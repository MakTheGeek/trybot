"""Continuous real-time price tracking with Dexscreener.

Dexscreener is a polling API (there is no socket), so "real time" here means one
fast, batched poll loop shared by everything that needs a live price:

* open positions — they drive Take Profit / Stop Loss / Trailing Stop;
* the token selected in the dashboard — its price, metrics and live candle;
* a watchlist — any contract the user chooses to follow;
* the most recent calls, so the dashboard stays alive between trades.

Each poll stores one tick per token and broadcasts it over the WebSocket, so the
dashboard moves tick by tick instead of being re-rendered on a timer.

Nothing is ever invented: a request that fails keeps the last known tick, marks
it stale and exposes its age, so an empty or frozen panel can never be mistaken
for a calm market.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque

from . import config
from .dexscreener import dexscreener
from .state import store

# Dexscreener accepts 30 token addresses per request: one batch per poll keeps
# the API load at ~30 req/min on the 2 s default, far under any rate limit.
BATCH = 30
MAX_TRACKED = 30
# In-memory tick history per token: a sparkline, not an archive.
TICKS = 400
SPARK_POINTS = 40
MAX_BACKOFF = 30.0
KINDS = ("position", "select", "watch", "call")


def _now_ms() -> int:
    return int(time.time() * 1000)


class PriceFeed:
    """The application's single Dexscreener poll loop."""

    def __init__(self) -> None:
        self._tracked: dict[str, dict] = {}
        self._selected: str = ""
        self._call_addresses: list[str] = []
        self._task: asyncio.Task | None = None
        self._running = False
        self.requests = 0
        self.errors = 0
        self.backoff = 0.0
        self.last_poll_ms = 0
        self.last_ok_ms = 0
        self.last_error = ""
        self.latency_ms = 0
        self._warned_over = False

    # --------------------------------------------------------------- tracking
    def track(self, address: str, *, kind: str = "watch", symbol: str = "") -> bool:
        address = (address or "").strip()
        if not address:
            return False
        entry = self._tracked.get(address)
        if entry is None:
            entry = {
                "address": address,
                "kinds": set(),
                "symbol": symbol,
                "price_usd": 0.0,
                "change_m5": 0.0,
                "change_h1": 0.0,
                "liquidity_usd": 0.0,
                "market_cap": 0.0,
                "volume_24h": 0.0,
                "ts": 0,
                "errors": 0,
                "ticks": deque(maxlen=TICKS),
            }
            self._tracked[address] = entry
        entry["kinds"].add(kind if kind in KINDS else "watch")
        if symbol:
            entry["symbol"] = symbol
        return True

    def untrack(self, address: str, *, kind: str = "watch") -> bool:
        address = (address or "").strip()
        entry = self._tracked.get(address)
        if entry is None:
            return False
        entry["kinds"].discard(kind)
        if not entry["kinds"]:
            self._tracked.pop(address, None)
        # Only dropping the *selection* clears the selection: removing a token
        # from the watchlist must not also stop following what is on screen.
        if kind == "select" and address == self._selected:
            self._selected = ""
        return True

    def select(self, address: str) -> None:
        """Follow the token currently shown in the dashboard (replaces the old one)."""
        address = (address or "").strip()
        previous, self._selected = self._selected, address
        if previous and previous != address:
            self.untrack(previous, kind="select")
        if address:
            self.track(address, kind="select")

    def watched(self) -> list[dict]:
        """Compact view of the watchlist (kind ``watch`` only)."""
        return [
            {"address": e["address"], "symbol": e.get("symbol") or ""}
            for e in self._tracked.values() if "watch" in e["kinds"]
        ]

    def quote(self, address: str) -> tuple[dict, int]:
        """Last tick for ``address`` plus its age in ms (``{}, 0`` if unknown).

        The position manager reads its exit prices from here, so the whole
        application makes a single price request per interval instead of one per
        subsystem.
        """
        entry = self._tracked.get((address or "").strip())
        if not entry or not entry.get("ts"):
            return {}, 0
        return {
            "address": entry["address"],
            "symbol": entry.get("symbol") or "",
            "price_usd": entry.get("price_usd") or 0.0,
            "market_cap": entry.get("market_cap") or 0.0,
            "liquidity_usd": entry.get("liquidity_usd") or 0.0,
            "volume_24h": entry.get("volume_24h") or 0.0,
            "change_m5": entry.get("change_m5") or 0.0,
        }, _now_ms() - int(entry["ts"])

    # ----------------------------------------------------------------- polling
    def _recent_calls(self) -> list[str]:
        try:
            limit = int(float(config.get("price_feed_calls") or 0))
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return []
        out: list[str] = []
        for call in store.calls:
            address = (call.get("address") or "").strip()
            if address and address not in out:
                out.append(address)
                if len(out) >= limit:
                    break
        return out

    def _wanted(self) -> list[str]:
        """Everything worth polling right now, in display order, capped.

        Also re-derives each entry's ``kinds``: this is what makes a token drop
        out of the feed on its own once it is neither held, selected, watched
        nor recent — its ticks are released with it.
        """
        if not config.get("price_feed_enabled", True):
            return []
        positions = [p for p in store.open_positions() if p.get("address")]
        held = {p["address"] for p in positions}
        for pos in positions:
            self.track(pos["address"], kind="position", symbol=pos.get("symbol") or "")
        self._call_addresses = self._recent_calls()
        calls = set(self._call_addresses)

        # Refresh the derived kinds first — this is what releases a token (and
        # its tick history) once nothing points at it any more.
        for address, entry in list(self._tracked.items()):
            kinds = entry["kinds"]
            if address not in held:
                kinds.discard("position")
            if address in calls:
                kinds.add("call")
            else:
                kinds.discard("call")
            if "select" in kinds and address != self._selected:
                kinds.discard("select")
            if not kinds:
                self._tracked.pop(address, None)
        # Then admit whatever is new (a token can enter the feed through a call
        # alone, without ever having been held or opened by hand).
        for address in self._call_addresses:
            self.track(address, kind="call")
        if self._selected:
            self.track(self._selected, kind="select")

        ordered: list[str] = []
        watched = [a for a, e in self._tracked.items() if "watch" in e["kinds"]]
        groups = (
            [p["address"] for p in positions],
            [self._selected] if self._selected else [],
            watched,
            self._call_addresses,
        )
        for group in groups:
            for address in group:
                if address and address not in ordered:
                    ordered.append(address)
        if len(ordered) > MAX_TRACKED:
            if not self._warned_over:
                self._warned_over = True
                store.log(
                    f"Suivi de prix limité à {MAX_TRACKED} tokens (Dexscreener : "
                    f"{BATCH} par requête) — {len(ordered) - MAX_TRACKED} ignoré(s).",
                    level="warn", action="monitor",
                )
            ordered = ordered[:MAX_TRACKED]
        return ordered

    async def _poll_once(self) -> None:
        addresses = self._wanted()
        if not addresses:
            self.last_poll_ms = _now_ms()
            return

        started = time.time()
        before_failures = dexscreener.failures
        prices = await dexscreener.prices_multi(addresses)
        self.latency_ms = int((time.time() - started) * 1000)
        self.requests += 1
        self.last_poll_ms = _now_ms()
        if dexscreener.failures != before_failures:
            # Nothing came back at all: transport failure, so back off.
            raise RuntimeError(dexscreener.last_error or "réponse vide")

        now = _now_ms()
        ticks: dict[str, dict] = {}
        for address in addresses:
            entry = self._tracked.get(address)
            if entry is None:
                continue
            quote = prices.get(address) or {}
            price = float(quote.get("price_usd") or 0.0)
            if price <= 0:
                # No indexed pair yet (still on a bonding curve): keep the last
                # tick and count the miss rather than publish a zero price.
                entry["errors"] = int(entry.get("errors") or 0) + 1
                continue
            entry.update({
                "price_usd": price,
                "change_m5": float(quote.get("change_m5") or 0.0),
                "change_h1": float(quote.get("change_h1") or 0.0),
                "liquidity_usd": float(quote.get("liquidity_usd") or 0.0),
                "market_cap": float(quote.get("market_cap") or 0.0),
                "volume_24h": float(quote.get("volume_24h") or 0.0),
                "symbol": quote.get("symbol") or entry.get("symbol") or "",
                "ts": now,
                "errors": 0,
            })
            entry["ticks"].append((now, price))
            ticks[address] = {
                "address": address,
                "symbol": entry["symbol"],
                "price_usd": price,
                "change_m5": entry["change_m5"],
                "liquidity_usd": entry["liquidity_usd"],
                "market_cap": entry["market_cap"],
                "volume_24h": entry["volume_24h"],
                "ts": now,
            }

        if not ticks:
            # A valid answer that simply holds no pair for any watched token
            # (all of them still on a bonding curve). Not an error: the request
            # worked, the misses stay counted per row, nothing is invented.
            self.last_ok_ms = now
            self.errors = 0
            store.runtime["dexscreener"] = "ok"
            store.broadcast({"type": "prices", "ts": now, "data": {}, "feed": self.counters()})
            return

        self.last_ok_ms = now
        self.errors = 0
        store.runtime["dexscreener"] = "ok"
        store.broadcast({
            "type": "prices", "ts": now, "data": ticks, "feed": self.counters(),
        })

    async def _loop(self) -> None:
        while self._running:
            interval = float(config.get("price_feed_seconds") or 2.0)
            try:
                await self._poll_once()
                if self.backoff:
                    store.log(
                        f"Suivi de prix rétabli (Dexscreener) après {self.backoff:.0f} s.",
                        level="success", action="monitor",
                    )
                self.backoff = 0.0
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                first = not self.backoff
                # Never hammer a failing endpoint: widen the gap, then come back.
                self.backoff = min(max(self.backoff * 2, interval * 2), MAX_BACKOFF)
                store.runtime["dexscreener"] = "error"
                if first:
                    store.log(
                        f"Suivi de prix en échec (Dexscreener): {self.last_error} — "
                        f"nouvel essai dans {self.backoff:.0f} s",
                        level="warn", action="monitor",
                    )
            await asyncio.sleep(max(0.5, self.backoff or interval))

    # ------------------------------------------------------------------ status
    def counters(self) -> dict:
        return {
            "enabled": bool(config.get("price_feed_enabled", True)),
            "interval_s": float(config.get("price_feed_seconds") or 2.0),
            "calls_tracked": int(float(config.get("price_feed_calls") or 0)),
            "tracked": len(self._tracked),
            "requests": self.requests,
            "errors": self.errors,
            "backoff_s": round(self.backoff, 1),
            "latency_ms": self.latency_ms,
            "last_ok_ms": self.last_ok_ms,
            "last_poll_ms": self.last_poll_ms,
            "last_error": self.last_error,
            "source": "dexscreener",
        }

    def status(self) -> dict:
        """Counters + one row per tracked token (with its tick history)."""
        now = _now_ms()
        rows = []
        for entry in sorted(self._tracked.values(), key=lambda e: -float(e.get("ts") or 0)):
            ticks = list(entry["ticks"])
            rows.append({
                "address": entry["address"],
                "symbol": entry.get("symbol") or "",
                "kinds": sorted(entry["kinds"]),
                "price_usd": entry.get("price_usd") or 0.0,
                "change_m5": entry.get("change_m5") or 0.0,
                "change_h1": entry.get("change_h1") or 0.0,
                "liquidity_usd": entry.get("liquidity_usd") or 0.0,
                "market_cap": entry.get("market_cap") or 0.0,
                "volume_24h": entry.get("volume_24h") or 0.0,
                "ts": entry.get("ts") or 0,
                "age_ms": (now - int(entry["ts"])) if entry.get("ts") else 0,
                "errors": entry.get("errors") or 0,
                "spark": [p for _, p in ticks][-SPARK_POINTS:],
            })
        return {**self.counters(), "rows": rows}

    # ------------------------------------------------------------ life cycle
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        store.log(
            f"Suivi de prix continu activé (Dexscreener, "
            f"{float(config.get('price_feed_seconds') or 2.0):.1f} s).",
            level="info", action="monitor",
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None


price_feed = PriceFeed()
