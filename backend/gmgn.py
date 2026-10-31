"""Thin async wrapper around the ``gmgn-cli`` executable.

We deliberately shell out to the official CLI (same binary the GMGN skills use)
instead of re-implementing request signing, so authentication, rate-limit
handling and routing stay identical to GMGN's own tooling.

Because GMGN bans an IP for repeated rate-limit violations, every call is
funnelled through a single global gate:

* requests are serialised (one at a time),
* a minimum gap is enforced between request *starts*,
* the queue is served by priority, so a trade (or a snipe analysis) never waits
  behind dashboard traffic such as background chart polling,
* read results are cached for a short TTL,
* on ``RATE_LIMIT_BANNED`` the whole client cools down until the reset time.
"""
from __future__ import annotations

import asyncio
import heapq
import json
import os
import random
import re
import time
from typing import Any

from . import config
from .clock import clock
from .paths import GMGN_CLI_JS, NODE_EXE

CHAIN = "sol"
SOL_MINT = "So11111111111111111111111111111111111111112"
# Canonical quote target for the fund-free trading-plan probe (SOL -> USDC).
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

MIN_GAP_SECONDS = 1.5          # minimum spacing between GMGN request starts
MAX_COOLDOWN_WAIT = 240.0      # max seconds to wait out a rate-limit ban

# Scheduling priorities for the shared gate. Lower number = served first.
# A buy must never queue behind the dashboard's chart polling.
PRIORITY_TRADE = 0             # swap, quote
PRIORITY_CRITICAL = 1          # token info / security (a snipe is waiting on it)
PRIORITY_NORMAL = 2            # portfolio, holders, order lookups
PRIORITY_LOW = 3               # kline, trenches, tracking feeds (dashboard only)


class GMGNError(RuntimeError):
    pass


class GMGNRateLimited(GMGNError):
    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GMGNPlanRequired(GMGNError):
    """GMGN answered 403 ``PLAN_UPGRADE_REQUIRED``.

    The endpoint costs more rate-limit weight than the current plan's capacity,
    so it can never succeed on that plan: this error is deterministic, must not
    be retried, and is the signal that the account needs a paid GMGN plan.
    """

    def __init__(self, message: str, upgrade_url: str = "") -> None:
        super().__init__(message)
        self.upgrade_url = upgrade_url


def _base_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(clock.env())          # make the CLI's signer aware of clock skew
    api_key = config.gmgn_api_key()
    if api_key:
        env["GMGN_API_KEY"] = api_key
    private_key = config.gmgn_private_key()
    if private_key:
        env["GMGN_PRIVATE_KEY"] = private_key
    env["GMGN_ALLOW_AUTOMATED_TRADES"] = "1"
    env.setdefault("NO_COLOR", "1")
    return env


_RATE_RE = re.compile(r"~(\d+)s remaining")

PLAN_UPGRADE_URL = "https://gmgn.ai/ai?chain={chain}&tab=paid_plans"


def _is_rate_limit(message: str) -> bool:
    return "RATE_LIMIT" in message or "429" in message


def _is_plan_required(message: str) -> bool:
    return "PLAN_UPGRADE_REQUIRED" in (message or "")


def _plan_error(args: list[str], raw: str = "") -> GMGNPlanRequired:
    """Build the actionable error for a 403 PLAN_UPGRADE_REQUIRED.

    The CLI truncates its own stderr on exit, so the raw text may lose GMGN's
    ``upgrade_message``; we rebuild it from the error code instead.
    """
    try:
        chain = args[args.index("--chain") + 1]
    except (ValueError, IndexError):
        chain = CHAIN
    endpoint = " ".join(a for a in args[:2] if not a.startswith("-"))
    url = PLAN_UPGRADE_URL.format(chain=chain)
    return GMGNPlanRequired(
        f"Plan GMGN insuffisant: la route « {endpoint} » dépasse la capacité de votre plan "
        f"actuel (403 PLAN_UPGRADE_REQUIRED). Réessayer ne servira à rien: passez à un plan "
        f"payant GMGN (Plus ou Pro) pour activer le trading via l'API. {url}",
        upgrade_url=url,
    )


class GMGNCLI:
    def __init__(self) -> None:
        self.cli_js = str(GMGN_CLI_JS) if GMGN_CLI_JS else ""
        self.min_gap = MIN_GAP_SECONDS
        self._next_allowed = 0.0
        self._blocked_until = 0.0
        # Gate waiters: (priority, sequence, granted, finished). `granted` is
        # resolved when the slot is handed over; `finished` when the invocation
        # is over, which is what lets the next waiter start.
        self._waiters: list[tuple[int, int, asyncio.Future, asyncio.Future]] = []
        self._seq = 0
        self._dispatcher: asyncio.Task | None = None
        self._cache: dict[str, tuple[float, Any]] = {}
        self.last_error: str = ""
        self.request_count = 0

    @property
    def available(self) -> bool:
        return bool(self.cli_js and os.path.exists(self.cli_js))

    @property
    def cooling_down(self) -> bool:
        return time.monotonic() < self._blocked_until

    def cooldown_remaining(self) -> float:
        return max(0.0, self._blocked_until - time.monotonic())

    # ------------------------------------------------------------------ core
    async def run(
        self, args: list[str], *, timeout: float = 90.0, retries: int = 1,
        cache_key: str | None = None, cache_ttl: float = 0.0,
        max_wait: float = MAX_COOLDOWN_WAIT, priority: int = PRIORITY_NORMAL,
    ) -> Any:
        if not self.available:
            raise GMGNError("gmgn-cli introuvable. Installez-le avec: npm install -g gmgn-cli")

        if cache_key and cache_ttl:
            hit = self._cache.get(cache_key)
            if hit and hit[0] > time.monotonic():
                return hit[1]

        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            # Wait out an active rate-limit ban *without* holding the gate so
            # the dashboard (charts, token lookups) is never blocked for minutes.
            wait = self._blocked_until - time.monotonic()
            if wait > 0:
                if wait > max_wait:
                    raise GMGNRateLimited(
                        f"GMGN rate limit actif encore {wait:.0f}s", retry_after=wait
                    )
                await asyncio.sleep(wait)

            finished = await self._acquire(priority)
            try:
                result, error = await self._invoke(args, timeout)
            finally:
                if not finished.done():
                    finished.set_result(None)
            self.request_count += 1

            if error is None:
                if cache_key and cache_ttl:
                    self._cache[cache_key] = (time.monotonic() + cache_ttl, result)
                return result

            self.last_error = error
            if _is_plan_required(error):
                # Deterministic failure: never retry, and avoid burning the
                # swap error limiter with a repeat of the same business error.
                raise _plan_error(args, error)
            if _is_rate_limit(error):
                match = _RATE_RE.search(error)
                secs = float(match.group(1)) if match else 60.0
                self._blocked_until = time.monotonic() + secs + 2.0
                last_exc = GMGNRateLimited(f"GMGN rate limit: {secs:.0f}s", retry_after=secs)
                # The loop re-checks the cooldown at the top (outside the lock).
                if secs > max_wait:
                    break
                continue
            last_exc = GMGNError(error)
            if attempt < retries:
                await asyncio.sleep(0.5 + random.random() * 0.5)

        raise last_exc or GMGNError("Appel gmgn-cli échoué")

    # ------------------------------------------------------------ gate
    async def _acquire(self, priority: int) -> asyncio.Future:
        """Reserve the next gate slot (by priority) and return a future that the
        caller resolves when its invocation is over."""
        loop = asyncio.get_running_loop()
        granted: asyncio.Future = loop.create_future()
        finished: asyncio.Future = loop.create_future()
        self._seq += 1
        heapq.heappush(self._waiters, (priority, self._seq, granted, finished))
        if self._dispatcher is None or self._dispatcher.done():
            self._dispatcher = asyncio.create_task(self._dispatch())
        try:
            await granted
        except asyncio.CancelledError:
            # Never leave the dispatcher waiting on a caller that gave up.
            if not finished.done():
                finished.set_result(None)
            raise
        return finished

    async def _dispatch(self) -> None:
        """Hand the gate to one caller at a time, highest priority first.

        The minimum gap is measured between request *starts*: when a request
        itself takes longer than the gap the next one starts immediately, which
        keeps a two-call analysis from paying the gap twice.
        """
        try:
            while self._waiters:
                priority, seq, granted, finished = self._waiters[0]
                if granted.cancelled():
                    heapq.heappop(self._waiters)
                    continue
                wait = self._next_allowed - time.monotonic()
                if wait > 0:
                    # Short slices so a higher-priority waiter arriving during
                    # the gap is served first.
                    await asyncio.sleep(min(wait, 0.05))
                    continue
                heapq.heappop(self._waiters)
                self._next_allowed = time.monotonic() + self.min_gap
                if not granted.done():
                    granted.set_result(None)
                try:
                    await finished
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._dispatcher = None

    async def _invoke(self, args: list[str], timeout: float) -> tuple[Any, str | None]:
        proc = await asyncio.create_subprocess_exec(
            NODE_EXE, self.cli_js, *args, "--raw",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_base_env(),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return None, "timeout"

        raw = out.decode("utf-8", errors="ignore").strip()
        if proc.returncode == 0 and raw:
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("{") or line.startswith("["):
                    try:
                        return self._unwrap(json.loads(line)), None
                    except GMGNError as exc:
                        return None, str(exc)
                    except json.JSONDecodeError:
                        continue
            return None, f"Sortie non-JSON: {raw[:200]}"

        message = err.decode("utf-8", errors="ignore").strip() or raw
        return None, (message[:500] or f"exit={proc.returncode}")

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload and "code" in payload:
            if payload.get("code") not in (0, None) and not payload.get("data"):
                raise GMGNError(
                    str(payload.get("message") or payload.get("reason") or "erreur GMGN")
                )
            data = payload.get("data")
            return data if data is not None else payload
        return payload

    # ------------------------------------------------------------- read APIs
    async def token_info(self, address: str, *, chain: str = CHAIN, ttl: float = 20.0) -> dict:
        data = await self.run(
            ["token", "info", "--chain", chain, "--address", address],
            cache_key=f"info:{chain}:{address}", cache_ttl=ttl,
            priority=PRIORITY_CRITICAL,
        )
        return data if isinstance(data, dict) else {}

    async def token_security(self, address: str, *, chain: str = CHAIN, ttl: float = 60.0) -> dict:
        try:
            data = await self.run(
                ["token", "security", "--chain", chain, "--address", address],
                cache_key=f"sec:{chain}:{address}", cache_ttl=ttl,
                priority=PRIORITY_CRITICAL,
            )
            return data if isinstance(data, dict) else {}
        except GMGNError:
            return {}

    async def token_holders(self, address: str, *, limit: int = 20, chain: str = CHAIN) -> dict:
        try:
            data = await self.run(
                ["token", "holders", "--chain", chain, "--address", address, "--limit", str(limit)]
            )
            return data if isinstance(data, dict) else {}
        except GMGNError:
            return {}

    async def kline(
        self, address: str, *, resolution: str = "1m", frm: int | None = None, to: int | None = None,
        chain: str = CHAIN, ttl: float = 20.0,
    ) -> list:
        args = ["market", "kline", "--chain", chain, "--address", address, "--resolution", resolution]
        if frm is not None:
            args += ["--from", str(frm)]
        if to is not None:
            args += ["--to", str(to)]
        try:
            data = await self.run(args, cache_key=f"kline:{chain}:{address}:{resolution}", cache_ttl=ttl,
                                  priority=PRIORITY_LOW)
        except GMGNRateLimited:
            raise
        except GMGNError:
            return []
        if isinstance(data, dict):
            return data.get("list") or data.get("klines") or []
        return data if isinstance(data, list) else []

    async def trenches(self, *, type_: str = "new_creation", limit: int = 30, chain: str = CHAIN) -> list:
        data = await self.run(
            ["market", "trenches", "--chain", chain, "--type", type_, "--limit", str(limit)],
            priority=PRIORITY_LOW,
        )
        if isinstance(data, dict):
            return data.get(type_) or data.get("list") or []
        return data if isinstance(data, list) else []

    async def portfolio_info(self, *, ttl: float = 45.0) -> dict:
        data = await self.run(["portfolio", "info"], cache_key="portfolio:info", cache_ttl=ttl)
        return data if isinstance(data, dict) else {}

    async def portfolio_holdings(self, wallet: str, *, chain: str = CHAIN) -> dict:
        data = await self.run(["portfolio", "holdings", "--chain", chain, "--wallet", wallet])
        return data if isinstance(data, dict) else {}

    async def token_balance(self, wallet: str, token: str, *, chain: str = CHAIN) -> dict:
        data = await self.run(
            ["portfolio", "token-balance", "--chain", chain, "--wallet", wallet, "--token", token]
        )
        return data if isinstance(data, dict) else {}

    async def track_kol(self, *, limit: int = 50, chain: str = CHAIN) -> list:
        try:
            data = await self.run(["track", "kol", "--chain", chain, "--limit", str(limit)],
                                  priority=PRIORITY_LOW)
            if isinstance(data, dict):
                return data.get("list") or []
            return data if isinstance(data, list) else []
        except GMGNError:
            return []

    async def track_smartmoney(self, *, limit: int = 50, chain: str = CHAIN) -> list:
        try:
            data = await self.run(["track", "smartmoney", "--chain", chain, "--limit", str(limit)],
                                  priority=PRIORITY_LOW)
            if isinstance(data, dict):
                return data.get("list") or []
            return data if isinstance(data, list) else []
        except GMGNError:
            return []

    async def quote(self, *, wallet: str, input_token: str, output_token: str, amount: str,
                    slippage: int = 30, chain: str = CHAIN,
                    priority: int = PRIORITY_TRADE) -> dict:
        data = await self.run([
            "order", "quote", "--chain", chain, "--from", wallet,
            "--input-token", input_token, "--output-token", output_token,
            "--amount", amount, "--slippage", str(slippage),
        ], priority=priority)
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ write APIs
    async def swap(
        self, *, wallet: str, input_token: str, output_token: str, amount: str | None = None,
        percent: float | None = None, slippage: int = 30, auto_slippage: bool = True,
        priority_fee: float | None = None, anti_mev: bool = True, chain: str = CHAIN,
    ) -> dict:
        args = [
            "swap", "--chain", chain, "--from", wallet,
            "--input-token", input_token, "--output-token", output_token,
        ]
        if amount is not None:
            args += ["--amount", str(amount)]
        if percent is not None:
            args += ["--percent", str(percent)]
        if auto_slippage:
            args += ["--auto-slippage"]
        else:
            args += ["--slippage", str(slippage)]
        if priority_fee:
            args += ["--priority-fee", str(priority_fee)]
        if anti_mev:
            args += ["--anti-mev"]
        args += ["--yes"]
        data = await self.run(args, timeout=140.0, retries=0, priority=PRIORITY_TRADE)
        return data if isinstance(data, dict) else {}

    async def order_get(self, order_id: str, *, chain: str = CHAIN) -> dict:
        data = await self.run(["order", "get", "--chain", chain, "--order-id", order_id])
        return data if isinstance(data, dict) else {}


gmgn = GMGNCLI()
