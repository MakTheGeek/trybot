"""Minimal Solana RPC client + local transaction signing.

Lets the bot execute swaps **without** GMGN's trading API (which requires a paid
plan): Jupiter builds the transaction, ``solders`` signs it locally with the
wallet key, and the signed transaction is sent straight to an RPC node.

Nothing here is simulated: every value comes from the chain.

Three properties matter more than the rest, and each was bought with a measured
failure:

* **Keep-alive.** ``net.post_json`` reuses one connection; a fresh handshake costs
  ~610 ms against ~300 ms on a reused one (see ``net``).
* **A silent node must be visible.** A ``200`` with an empty body is how a
  rate-limited free endpoint dies, and for a while this bot only survived it
  because reads fell back to another node — sends did not. Endpoint health is
  now recorded and reported.
* **A signature must never be guessed.** Broadcasting is still single-endpoint
  and single-attempt; only *reads* fail over, and so does simulation (which
  broadcasts nothing).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import pathlib
import threading
import time
import urllib.parse
from typing import Any

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction, VersionedTransaction

import base58

from . import config, net

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
KEY_FILE = pathlib.Path.home() / ".config" / "solsnipbot" / "wallet.key"

LAMPORTS_PER_SOL = 1_000_000_000

# Compute units one of this bot's swaps actually reserves. Jupiter sets the limit
# itself (``dynamicComputeUnitLimit``): decoded from the ComputeBudget
# instructions of 9 real swaps, the limits were 83 435–115 665 CU (median
# 110 012), with a single 1 400 000 outlier. It only ever converts a priority-fee
# *price* — µlamports per CU, the unit the network and Helius both quote — into
# the *total* lamports a transaction pays, which is the unit the ``priority_fee``
# setting caps: ``total = price x limit / 1e6``.
DEFAULT_CU_BUDGET = 110_000


class SolanaError(RuntimeError):
    pass


# ------------------------------------------------------------------ json-rpc
def _expand_keys(url: str) -> str:
    """Resolve an API-key placeholder from the environment.

    Keeping the key out of ``data/settings.json`` means rotating it is an
    environment change, not a hunt through files: store
    ``https://mainnet.helius-rpc.com/?api-key={api_key}`` and set
    ``HELIUS_API_KEY``. A URL with the key already inline is used untouched, so
    nothing breaks for a setup that never adopts the placeholder.
    """
    if "{api_key}" not in url and "${HELIUS_API_KEY}" not in url:
        return url
    key = config.get_env("HELIUS_API_KEY").strip()
    if not key:
        return url
    return url.replace("${HELIUS_API_KEY}", key).replace("{api_key}", key)


def rpc_url() -> str:
    configured = config.get_env("SOLANA_RPC_URL") or config.get("solana_rpc_url") or ""
    return _expand_keys((configured or PUBLIC_RPC).strip())


def ws_url() -> str:
    """The node's WebSocket URL: explicit override, else derived from the RPC URL.

    Deriving it is the point — Helius serves both on the same host, so one
    configured URL gives the socket for free and nothing extra to mistype.
    """
    explicit = config.get_env("SOLANA_WS_URL").strip()
    if explicit:
        return _expand_keys(explicit)
    base = rpc_url()
    for scheme, ws_scheme in (("https://", "wss://"), ("http://", "ws://")):
        if base.startswith(scheme):
            return ws_scheme + base[len(scheme):]
    return ""


_SECRET_PARAMS = ("key", "token", "secret", "pass")


def _mask(url: str) -> str:
    """A URL safe to show in the UI or a log: any credential becomes ``***``.

    An API key is a billable secret; it must never be echoed back to a browser.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:  # noqa: BLE001
        return url
    if not parts.query:
        return url
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    masked = "&".join(
        f"{urllib.parse.quote(name, safe='')}="
        + ("***" if any(s in name.lower() for s in _SECRET_PARAMS)
           else urllib.parse.quote(value, safe=""))
        for name, value in pairs
    )
    return urllib.parse.urlunsplit(parts._replace(query=masked))


# ----------------------------------------------------------- endpoint health
# A node that answers `200` with an empty body looks healthy to a socket and
# useless to a bot. It went unnoticed here for a while because reads silently
# fell back while sends did not. Every RPC attempt is therefore recorded.
_health: dict[str, dict[str, Any]] = {}
_health_lock = threading.Lock()


def _note(url: str, ok: bool, detail: str = "", ms: float | None = None) -> None:
    host = _mask(url)
    with _health_lock:
        entry = _health.setdefault(
            host, {"ok": 0, "fail": 0, "last_error": "", "last_ms": None, "samples": []}
        )
        if ok:
            entry["ok"] += 1
            entry["last_error"] = ""
            if ms is not None:
                entry["last_ms"] = round(ms)
                entry["samples"] = (entry["samples"] + [ms])[-20:]
        else:
            entry["fail"] += 1
            entry["last_error"] = detail[:140]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def rpc_health() -> dict[str, Any]:
    """What the node layer is really doing, with every credential masked."""
    with _health_lock:
        endpoints: dict[str, dict[str, Any]] = {}
        for host, entry in _health.items():
            median = _median(entry["samples"])
            endpoints[host] = {
                "ok": entry["ok"],
                "fail": entry["fail"],
                "last_error": entry["last_error"],
                "last_ms": entry["last_ms"],
                "median_ms": round(median, 1) if median is not None else None,
            }
    alive = any(entry["ok"] for entry in endpoints.values())
    return {
        "primary": _mask(rpc_url()),
        "fallback": _mask(str(config.get("solana_rpc_fallback_url") or "")),
        "endpoints": endpoints,
        "alive": alive if endpoints else None,
        "websocket": _ws.status(),
    }


def _call_sync(url: str, method: str, params: list[Any], timeout: float) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    started = time.perf_counter()
    try:
        body = net.post_json(url, payload, timeout=timeout)
    except net.HttpError as exc:
        _note(url, False, f"HTTP {exc.status}")
        raise SolanaError(f"RPC {method}: HTTP {exc.status}: {exc.detail[:160]}") from exc
    except net.EmptyResponse as exc:
        # This is the "dead free node" signature: HTTP 200, nothing to read.
        _note(url, False, f"nœud muet ({exc})")
        raise SolanaError(f"RPC {method}: le nœud ne renvoie rien ({exc})") from exc
    except Exception as exc:  # noqa: BLE001
        _note(url, False, f"{type(exc).__name__}: {str(exc)[:80]}")
        raise SolanaError(f"RPC {method}: {exc}") from exc
    if isinstance(body, dict) and body.get("error"):
        error = body["error"]
        message = error.get("message") if isinstance(error, dict) else error
        _note(url, False, str(message)[:80])
        raise SolanaError(f"RPC {method}: {message}")
    _note(url, True, ms=(time.perf_counter() - started) * 1000)
    return body.get("result") if isinstance(body, dict) else None


def _batch_sync(url: str, body: list[dict], timeout: float) -> list[Any]:
    """One POST carrying several read calls; results come back in order.

    Measured: three reads in a single round trip cost what one costs (~300 ms
    either way), so anything that used to be several reads in a row is free to
    batch.
    A call that fails inside a batch answers ``None`` instead of sinking the
    whole set — partial information beats none.
    """
    started = time.perf_counter()
    try:
        answer = net.post_json(url, body, timeout=timeout)
    except net.EmptyResponse as exc:
        _note(url, False, f"nœud muet ({exc})")
        raise SolanaError(f"RPC lot: le nœud ne renvoie rien ({exc})") from exc
    except net.HttpError as exc:
        _note(url, False, f"HTTP {exc.status}")
        raise SolanaError(f"RPC lot: HTTP {exc.status}: {exc.detail[:160]}") from exc
    except Exception as exc:  # noqa: BLE001
        _note(url, False, f"{type(exc).__name__}: {str(exc)[:80]}")
        raise SolanaError(f"RPC lot: {exc}") from exc
    if not isinstance(answer, list):
        _note(url, False, "réponse en lot illisible")
        raise SolanaError("RPC lot: réponse illisible")
    _note(url, True, ms=(time.perf_counter() - started) * 1000)
    by_id: dict[Any, dict] = {}
    for item in answer:
        if isinstance(item, dict) and "id" in item:
            by_id[item["id"]] = item
    results: list[Any] = []
    for request in body:
        item = by_id.get(request["id"])
        if item is None or item.get("error"):
            results.append(None)
        else:
            results.append(item.get("result"))
    return results


# Broadcasting must never be replayed on another node: a transaction the first
# node accepted would simply be sent twice. Every other call may fail over —
# including ``simulateTransaction``, which is a *read*: it validates and returns,
# it never broadcasts. Excluding it meant that on the day the primary node died,
# the free pre-flight that protects a trade from an unexecutable route silently
# stopped working while reads carried on falling back.
_NO_FALLBACK = {"sendTransaction"}
_last_fallback_warning = 0.0


def _endpoints(method: str) -> list[str]:
    primary = rpc_url()
    if method in _NO_FALLBACK:
        return [primary]
    fallback = str(config.get("solana_rpc_fallback_url") or "").strip()
    if not fallback or fallback == primary:
        return [primary]
    return [primary, fallback]


def _warn_fallback(method: str, exc: Exception) -> None:
    """Tell the user once a minute that the primary node is not answering."""
    global _last_fallback_warning
    now = time.monotonic()
    if now - _last_fallback_warning < 60:
        return
    _last_fallback_warning = now
    try:  # imported lazily: solana_rpc must stay importable on its own
        from .state import store

        store.log(
            f"RPC principal en échec ({method}: {str(exc)[:90]}) — bascule sur le "
            "RPC de secours.",
            level="warn", action="rpc",
        )
    except Exception:  # noqa: BLE001
        pass


async def _with_fallback(method: str, run) -> Any:
    """Run ``run(url)`` on the primary, then on the fallback if it is allowed."""
    endpoints = _endpoints(method)
    last: Exception | None = None
    for index, url in enumerate(endpoints):
        try:
            return await asyncio.to_thread(run, url)
        except SolanaError as exc:
            last = exc
            if index + 1 < len(endpoints):
                _warn_fallback(method, exc)
    raise last or SolanaError(f"RPC {method}: aucun endpoint disponible")


async def rpc(method: str, params: list[Any], timeout: float = 20.0) -> Any:
    """Run a JSON-RPC call off the event loop, with a fallback node on failure."""
    return await _with_fallback(
        method, lambda url: _call_sync(url, method, params, timeout)
    )


async def rpc_batch(calls: list[tuple[str, list[Any]]], timeout: float = 20.0) -> list[Any]:
    """Several **read** methods in one round trip; results follow the input order.

    A broadcast is refused outright: batching it would make it replayable on the
    fallback, which is the one thing this module promises not to do.
    """
    if not calls:
        return []
    forbidden = sorted({method for method, _ in calls if method in _NO_FALLBACK})
    if forbidden:
        raise SolanaError(
            f"RPC en lot refusé pour {', '.join(forbidden)}: une diffusion ne se rejoue pas"
        )
    body = [
        {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
        for index, (method, params) in enumerate(calls, start=1)
    ]
    label = "lot:" + ",".join(method for method, _ in calls)
    return await _with_fallback(label, lambda url: _batch_sync(url, body, timeout))


async def priority_fee_levels(account: str = "") -> dict[str, float] | None:
    """The going priority-fee *price* per level, in µlamports per compute unit.

    This is a Helius extension; any other node answers "method not found", which
    is not an error — it only means the diagnostic is unavailable, so ``None``.
    """
    params: dict[str, Any] = {"options": {"includeAllPriorityFeeLevels": True}}
    if account:
        params["accountKeys"] = [account]
    try:
        result = await rpc("getPriorityFeeEstimate", [params], timeout=10.0)
    except Exception:  # noqa: BLE001
        return None
    levels = (result or {}).get("priorityFeeLevels") if isinstance(result, dict) else None
    return levels if isinstance(levels, dict) else None


_fee_cache: dict[str, Any] = {"ts": 0.0, "levels": None}


async def cached_priority_fee_levels(ttl: float = 30.0) -> dict[str, float] | None:
    """The levels above, refreshed at most every ``ttl`` seconds.

    The dashboard polls the health endpoint, so asking the node on every poll
    would turn a read-only indicator into traffic — and traffic on a rate-limited
    plan is exactly what the rest of this module exists to ration.
    """
    now = time.monotonic()
    if now - float(_fee_cache["ts"] or 0.0) < ttl:
        return _fee_cache["levels"]
    levels = await priority_fee_levels()
    _fee_cache.update(ts=now, levels=levels)
    return levels


def priority_fee_advice(levels: dict[str, float] | None,
                        settings: dict[str, Any]) -> dict[str, Any]:
    """Is the configured priority-fee cap above or below the going rate?

    The two numbers are set in different units and comparing them raw would be
    comparing a distance with a speed. ``priority_fee`` is a **total** in SOL —
    what Jupiter's ``maxLamports`` and the fee model both cap — while the levels
    are a **price** in µlamports per CU. The bridge is
    ``total = price x CU budget / 1e6``, with the CU budget measured from this
    bot's own swaps (see ``DEFAULT_CU_BUDGET``).
    """
    cap_sol = max(float(settings.get("priority_fee") or 0.0), 0.0)
    cap_lamports = int(round(cap_sol * LAMPORTS_PER_SOL))
    implied: dict[str, int] = {}
    for name in ("medium", "high", "veryHigh"):
        price = (levels or {}).get(name)
        if isinstance(price, (int, float)) and price > 0:
            implied[name] = int(price * DEFAULT_CU_BUDGET / 1_000_000)
    advice: dict[str, Any] = {
        "cap_sol": cap_sol,
        "cap_lamports": cap_lamports,
        # What price the cap allows, in the unit the network quotes.
        "cap_price_microlamports_per_cu": (
            int(cap_lamports * 1_000_000 / DEFAULT_CU_BUDGET) if cap_lamports else 0
        ),
        "levels_microlamports_per_cu": levels,
        "implied_lamports": implied,
        "cu_budget": DEFAULT_CU_BUDGET,
    }
    high = implied.get("high")
    if high is None:
        advice["state"] = "indisponible"
    elif cap_lamports >= high:
        advice["state"] = "au-dessus"
    else:
        advice["state"] = "en-dessous"
        advice["shortfall_lamports"] = high - cap_lamports
    return advice


# ------------------------------------------------------------------- wallet
def wallet_keypair() -> Keypair:
    raw = (config.get_env("SOLANA_WALLET_PRIVATE_KEY") or "").strip()
    if not raw and KEY_FILE.exists():
        raw = KEY_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        raise SolanaError(
            "Clé privée du wallet absente "
            "(SOLANA_WALLET_PRIVATE_KEY ou ~/.config/solsnipbot/wallet.key)."
        )
    try:
        if raw.startswith("["):
            data = bytes(json.loads(raw))
        else:
            data = base58.b58decode(raw)
    except Exception as exc:  # noqa: BLE001
        raise SolanaError(f"Clé privée illisible: {exc}") from exc

    try:
        if len(data) == 64:
            try:
                return Keypair.from_bytes(data)
            except Exception:  # noqa: BLE001
                # Solana secret keys are seed(32) + pubkey(32); the trailing
                # half is redundant for signing, so tolerate a typo there as
                # long as the seed still derives a valid keypair.
                return Keypair.from_seed(data[:32])
        if len(data) == 32:
            return Keypair.from_seed(data)
    except Exception as exc:  # noqa: BLE001
        raise SolanaError(f"Clé privée invalide: {exc}") from exc
    raise SolanaError(f"Clé privée de longueur inattendue ({len(data)} octets).")


def wallet_address() -> str:
    return str(wallet_keypair().pubkey())


# --------------------------------------------------------- sign / send / wait
def _signed_payload(tx: VersionedTransaction) -> bytes:
    """Exact bytes an on-chain signature must cover.

    ``bytes(MessageV0)`` omits the version prefix, but the serialized versioned
    transaction writes it back (``0x80 | version``), so signing the raw message
    bytes produces a signature that verifies as invalid.
    """
    payload = bytes(tx.message)
    version = tx.version()
    if version is not None:
        payload = bytes([0x80 | int(version)]) + payload
    return payload


def decode_and_check(b64_tx: str) -> tuple[VersionedTransaction, Keypair]:
    """Decode a built transaction and refuse anything we must not sign."""
    keypair = wallet_keypair()
    try:
        raw = base64.b64decode(b64_tx)
    except Exception as exc:  # noqa: BLE001
        raise SolanaError("Transaction Jupiter illisible (base64).") from exc
    try:
        tx = VersionedTransaction.from_bytes(raw)
    except Exception as exc:  # noqa: BLE001
        raise SolanaError(f"Transaction Jupiter invalide: {exc}") from exc

    payer = tx.message.account_keys[0]
    if payer != keypair.pubkey():
        raise SolanaError(
            f"Le payeur de la transaction ({payer}) n'est pas le wallet local "
            f"({keypair.pubkey()}) — signature refusée."
        )
    if len(tx.signatures) != 1:
        raise SolanaError(
            f"Transaction à {len(tx.signatures)} signatures: une seule est signable "
            "localement (celle du wallet)."
        )
    return tx, keypair


def sign_only(b64_tx: str) -> str:
    """Sign locally without broadcasting: returns the signed transaction (base64)."""
    tx, keypair = decode_and_check(b64_tx)
    signed = VersionedTransaction.populate(tx.message, [keypair.sign_message(_signed_payload(tx))])
    return base64.b64encode(bytes(signed)).decode("ascii")


async def sign_and_send(b64_tx: str, *, skip_preflight: bool = True) -> str:
    """Sign Jupiter's serialized transaction locally and broadcast it."""
    tx, keypair = decode_and_check(b64_tx)
    local_signature = keypair.sign_message(_signed_payload(tx))
    signed = VersionedTransaction.populate(tx.message, [local_signature])
    encoded = base64.b64encode(bytes(signed)).decode("ascii")
    broadcast = await rpc(
        "sendTransaction",
        [encoded, {
            "encoding": "base64",
            "skipPreflight": skip_preflight,
            "maxRetries": 3,
            "preflightCommitment": "confirmed",
        }],
        timeout=30.0,
    )
    if not broadcast:
        raise SolanaError("Le RPC n'a renvoyé aucune signature.")
    returned = str(broadcast)
    if returned != str(local_signature):
        # The node echoed a different signature: the transaction we signed is
        # not the one it accepted. Never report a hash we cannot vouch for.
        raise SolanaError(
            "Signature renvoyée par le RPC différente de la signature locale "
            f"({returned[:12]}… vs {str(local_signature)[:12]}…)."
        )
    return returned


async def simulate_signed(signed_b64: str, *, sig_verify: bool = True) -> dict:
    """Ask the node to simulate a signed transaction (free, validates the signature)."""
    result = await rpc(
        "simulateTransaction",
        [signed_b64, {
            "encoding": "base64",
            "sigVerify": sig_verify,
            "replaceRecentBlockhash": False,
            "commitment": "processed",
        }],
        timeout=30.0,
    )
    return (result or {}).get("value") or {}


async def _signature_status(signature: str, *, history: bool) -> dict | None:
    result = await rpc(
        "getSignatureStatuses", [[signature], {"searchTransactionHistory": history}]
    )
    return ((result or {}).get("value") or [None])[0]


class _SignatureSocket:
    """One long-lived WebSocket, used to learn a fill the moment the node does.

    Polling ``getSignatureStatuses`` once a second learns a fill up to a second
    late — and a take-profit detected a second late exits at the wrong price,
    which is exactly the overshoot measured on this bot's own paper exits. A
    ``signatureSubscribe`` notification arrives as the node processes the
    transaction. The socket also carries the slot stream, so the dashboard can
    show a live proof that the connection is real rather than assumed.

    Every failure here is soft by design: socket down, silent, or answering
    something unexpected all make ``wait`` return ``None``, and the caller falls
    back to the polling it has always used. The WebSocket can make confirmation
    faster; it can never make it fail.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._socket: Any | None = None
        self._futures: dict[str, asyncio.Future] = {}
        self._request_to_sig: dict[int, str] = {}
        self._sub_to_sig: dict[int, str] = {}
        self._sig_to_sub: dict[str, int] = {}
        self._next_id = 1
        self._connected = False
        self._last_slot: int | None = None
        self._last_message = 0.0
        self._reconnects = 0
        self._notifications = 0
        self._error = ""

    # -- lifecycle ---------------------------------------------------------
    def ensure_started(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._run())
        except RuntimeError:  # no running loop: polling still works
            self._task = None

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        # The task is cancelled on purpose, so its CancelledError is expected —
        # and it is a BaseException, which ``suppress(Exception)`` would not
        # catch, so shutdown used to end in a spurious traceback.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def status(self) -> dict[str, Any]:
        idle = None
        if self._last_message:
            idle = int((time.monotonic() - self._last_message) * 1000)
        return {
            "connected": self._connected,
            "url": _mask(ws_url()),
            "last_slot": self._last_slot,
            "idle_ms": idle,
            "reconnects": self._reconnects,
            "notifications": self._notifications,
            "error": self._error,
        }

    # -- the loop ----------------------------------------------------------
    async def _run(self) -> None:
        try:
            import websockets
        except Exception as exc:  # noqa: BLE001
            self._error = f"websockets indisponible: {exc}"
            return
        backoff = 1.0
        while True:
            url = ws_url()
            if not url:
                self._error = "aucune URL WebSocket"
                return
            try:
                async with websockets.connect(
                    url, open_timeout=15, ping_interval=20, close_timeout=5,
                    max_size=2 ** 21,
                ) as socket:
                    self._socket = socket
                    self._connected = True
                    self._error = ""
                    backoff = 1.0
                    await socket.send(json.dumps(
                        {"jsonrpc": "2.0", "id": 0, "method": "slotSubscribe"}
                    ))
                    async for raw in socket:
                        self._last_message = time.monotonic()
                        with contextlib.suppress(Exception):
                            self._handle(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._error = f"{type(exc).__name__}: {str(exc)[:90]}"
                self._reconnects += 1
            finally:
                self._socket = None
                self._connected = False
                # A dropped socket must never leave a caller waiting: every
                # pending wait is released so polling takes over immediately.
                for future in list(self._futures.values()):
                    if not future.done():
                        future.set_result(None)
                self._futures.clear()
                self._request_to_sig.clear()
                self._sub_to_sig.clear()
                self._sig_to_sub.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _handle(self, message: dict) -> None:
        method = message.get("method")
        if method == "slotNotification":
            with contextlib.suppress(Exception):
                self._last_slot = int(message["params"]["result"]["slot"])
            return
        if method == "signatureNotification":
            self._notifications += 1
            with contextlib.suppress(Exception):
                signature = self._sub_to_sig.pop(int(message["params"]["subscription"]), None)
                if signature:
                    self._sig_to_sub.pop(signature, None)
                    future = self._futures.pop(signature, None)
                    if future is not None and not future.done():
                        future.set_result(message["params"].get("result"))
            return
        request_id = message.get("id")
        if request_id in self._request_to_sig:
            signature = self._request_to_sig.pop(request_id)
            subscription = message.get("result")
            future = self._futures.get(signature)
            if isinstance(subscription, int):
                self._sub_to_sig[subscription] = signature
                self._sig_to_sub[signature] = subscription
            elif future is not None and not future.done():
                # The node refused the subscription: release the caller so it
                # polls instead of waiting out the timeout.
                self._futures.pop(signature, None)
                future.set_result(None)

    # -- the one thing callers use ----------------------------------------
    async def wait(self, signature: str, timeout: float) -> dict | None:
        """Wait for the node's verdict on ``signature``; ``None`` if it can't give one."""
        self.ensure_started()
        socket = self._socket
        if not self._connected or socket is None:
            return None
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        request_id = self._next_id
        self._next_id += 1
        self._futures[signature] = future
        self._request_to_sig[request_id] = signature
        try:
            await socket.send(json.dumps({
                "jsonrpc": "2.0", "id": request_id, "method": "signatureSubscribe",
                "params": [signature, {"commitment": "confirmed"}],
            }))
            result = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return None
        finally:
            self._futures.pop(signature, None)
            self._request_to_sig.pop(request_id, None)
            subscription = self._sig_to_sub.pop(signature, None)
            if subscription is not None:
                self._sub_to_sig.pop(subscription, None)
                with contextlib.suppress(Exception):
                    await socket.send(json.dumps({
                        "jsonrpc": "2.0", "id": request_id + 1_000_000,
                        "method": "signatureUnsubscribe", "params": [subscription],
                    }))
        if isinstance(result, dict):
            return result["value"] if isinstance(result.get("value"), dict) else result
        return None


_ws = _SignatureSocket()


async def start_background() -> None:
    """Open the node socket at boot (so its state is visible before it is needed)."""
    _ws.ensure_started()


async def stop_background() -> None:
    await _ws.stop()
    net.close_client()


async def confirm(signature: str, *, timeout: float = 45.0) -> dict:
    """Confirm a transaction as fast as the node can tell us.

    Three stages, cheapest first:

    1. one status read — a transaction that landed while we were still in the
       previous call is already visible, and this is the common case;
    2. the WebSocket notification — no polling, the verdict arrives as it forms;
    3. polling — the fallback that always works, kept exactly as it was.

    Stage 2 is an optimisation. If the socket is down, silent or refuses the
    subscription, this behaves as it always did.
    """
    deadline = time.monotonic() + timeout
    value = await _signature_status(signature, history=False)
    error = (value or {}).get("err")
    if error:
        raise SolanaError(f"Transaction rejetée on-chain: {error}")
    if value and value.get("confirmationStatus") in ("confirmed", "finalized"):
        return value

    remaining = deadline - time.monotonic()
    if remaining > 0:
        notified = await _ws.wait(signature, timeout=min(remaining, 20.0))
        if notified is not None:
            error = notified.get("err")
            if error:
                raise SolanaError(f"Transaction rejetée on-chain: {error}")
            # The node only fires this at the commitment we asked for.
            return {"err": None, "confirmationStatus": "confirmed", "via": "websocket"}

    while time.monotonic() < deadline:
        value = await _signature_status(signature, history=False)
        if value:
            if value.get("err"):
                raise SolanaError(f"Transaction rejetée on-chain: {value['err']}")
            if value.get("confirmationStatus") in ("confirmed", "finalized"):
                return value
        await asyncio.sleep(1.0)
    # Last chance: the recent-status cache only holds ~150 slots, so a genuinely
    # landed transaction can fall out of it before we ever see it.
    value = await _signature_status(signature, history=True)
    if value and not value.get("err"):
        return value
    raise SolanaError(f"Transaction non confirmée après {timeout:.0f}s ({signature[:12]}…)")


async def transaction_effects(signature: str, wallet: str) -> dict[str, float | None]:
    """What a settled transaction really did: ``{"delta", "fee"}`` (SOL).

    ``delta`` is the net SOL change for ``wallet``; ``fee`` is ``meta.fee`` —
    base fee **plus** priority fee — i.e. every lamport the transaction cost the
    payer, which is what makes a fee-inclusive PnL possible. ``None`` when the
    transaction is unknown or the field is absent.
    """
    result = await rpc(
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                     "commitment": "confirmed"}],
        timeout=25.0,
    )
    if not isinstance(result, dict):
        return {"delta": None, "fee": None}
    meta = result.get("meta") or {}
    message = ((result.get("transaction") or {}).get("message") or {})
    keys = message.get("accountKeys") or []
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    delta: float | None = None
    for index, entry in enumerate(keys):
        pubkey = entry.get("pubkey") if isinstance(entry, dict) else entry
        if pubkey == wallet and index < len(pre) and index < len(post):
            delta = (float(post[index]) - float(pre[index])) / LAMPORTS_PER_SOL
            break
    fee: float | None = None
    if meta.get("fee") is not None:
        try:
            fee = float(meta["fee"]) / LAMPORTS_PER_SOL
        except (TypeError, ValueError):
            fee = None
    return {"delta": delta, "fee": fee}


async def balance_delta(signature: str, wallet: str) -> float | None:
    """Net SOL change for ``wallet`` in a settled transaction (None if unknown).

    This is the *real* cash effect — fees and any token-account rent refund
    included — read from the chain instead of estimated from a quote.
    """
    return (await transaction_effects(signature, wallet))["delta"]


async def transaction_fee(signature: str) -> float | None:
    """Fee (base + priority) a transaction cost, in SOL; ``None`` if unknown."""
    if not signature:
        return None
    try:
        # The fee payer is the wallet we trade with, so its ledger entry works.
        effects = await transaction_effects(signature, wallet_address())
    except Exception:  # noqa: BLE001
        return None
    return effects.get("fee")


# ------------------------------------------------------------------- balances
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
# pump.fun mints are Token-2022: both programs must be swept to miss nothing.
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")

# CloseAccount instruction of the SPL token program (single u8 discriminant).
_CLOSE_ACCOUNT = bytes([9])


async def list_token_balances(wallet: str) -> list[dict[str, Any]]:
    """Every non-empty token account of the wallet, both token programs.

    Returns ``[{mint, amount, raw, program, address}]`` sorted by descending size.
    Used to catch tokens held without a position record (an orphaned buy).
    """
    found: dict[str, dict[str, Any]] = {}
    # Both token programs in one round trip: a batch of reads costs what one
    # read costs (measured: a batch of three reads costs what one read costs,
    # ~300 ms), and a sweep that misses the
    # Token-2022 program would miss every pump.fun token there is.
    programs = (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID)
    try:
        answers = await rpc_batch([
            ("getTokenAccountsByOwner",
             [wallet, {"programId": str(program)},
              {"encoding": "jsonParsed", "commitment": "confirmed"}])
            for program in programs
        ])
    except Exception:  # noqa: BLE001
        return []
    for program, result in zip(programs, answers):
        for account in (result or {}).get("value") or []:
            try:
                info = account["account"]["data"]["parsed"]["info"]
                amount = info["tokenAmount"]
                raw = int(amount.get("amount") or 0)
                mint = info["mint"]
            except Exception:  # noqa: BLE001
                continue
            if raw <= 0:
                continue
            previous = found.get(mint)
            if previous and int(previous["raw"]) >= raw:
                continue
            found[mint] = {
                "mint": mint,
                "amount": float(amount.get("uiAmountString") or amount.get("uiAmount") or 0.0),
                "raw": raw,
                "decimals": int(amount.get("decimals") or 0),
                "program": str(account["account"].get("owner") or program),
                "address": account["pubkey"],
            }
    return sorted(found.values(), key=lambda item: item["raw"], reverse=True)


async def token_account(wallet: str, mint: str) -> dict[str, Any] | None:
    """Largest token account holding ``mint``: {address, program, ui, decimals, raw}.

    ``program`` is the owning token program — pump.fun tokens now use
    **Token-2022**, not the legacy SPL token program, so it must be read from the
    account instead of assumed.
    """
    result = await rpc(
        "getTokenAccountsByOwner",
        [wallet, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )
    best: dict[str, Any] | None = None
    for account in (result or {}).get("value") or []:
        try:
            info = account["account"]["data"]["parsed"]["info"]["tokenAmount"]
            ui = float(info.get("uiAmountString") or info.get("uiAmount") or 0.0)
            decimals = int(info.get("decimals") or 0)
            raw = int(info.get("amount") or 0)
        except Exception:  # noqa: BLE001
            continue
        if best is None or raw > int(best["raw"]):
            best = {"address": account["pubkey"],
                    "program": account["account"].get("owner") or str(TOKEN_PROGRAM_ID),
                    "ui": ui, "decimals": decimals, "raw": raw}
    return best


async def token_ui_balance(wallet: str, mint: str) -> tuple[float, int, int]:
    """On-chain SPL balance for one mint: (ui_amount, decimals, raw_amount).

    ``raw_amount`` is the exact integer amount in the mint's smallest unit, which
    is what a sell must quote (float maths would lose precision).
    """
    account = await token_account(wallet, mint)
    if not account:
        return (0.0, 0, 0)
    return (float(account["ui"]), int(account["decimals"]), int(account["raw"]))


async def mint_decimals(mint: str) -> int:
    """Decimals of a mint, both token programs (pump.fun mints are Token-2022).

    Paper mode turns a Jupiter quote's raw ``outAmount`` into a token amount
    without ever owning the token, so there is no token account to read the
    decimals from — the mint itself is asked. Two encodings are queried in the
    same round trip, so whichever one the node accepts is already there.
    """
    try:
        info, supply = await rpc_batch([
            ("getAccountInfo", [mint, {"encoding": "jsonParsed"}]),
            ("getTokenSupply", [mint]),
        ], timeout=15.0)
    except Exception:  # noqa: BLE001
        info = supply = None
    parsed = ((info or {}).get("value") or {}).get("data") or {}
    decimals = ((parsed.get("parsed") or {}).get("info") or {}).get("decimals")
    if decimals is None:
        decimals = ((supply or {}).get("value") or {}).get("decimals")
    if decimals is not None:
        return int(decimals)
    raise SolanaError(f"Décimales du token illisibles ({mint[:8]}…)")


async def close_token_account(mint: str) -> dict[str, float]:
    """Close the now-empty token account for ``mint`` and get its rent back.

    Every first buy of a token creates an associated token account whose rent
    (~0.0015–0.002 SOL) stays locked until the account is closed — more than a
    small trade. Nothing is closed unless the balance is **exactly zero**, and
    the instruction itself refuses a non-empty account, so a partially sold
    position can never be touched.

    Returns the measured numbers: ``rent`` (lamports the account held), ``fee``
    (what the closing transaction cost) and ``net`` (the real wallet change, so
    ``net = rent - fee``) — all ``0.0`` when there was nothing to close.
    """
    empty = {"rent": 0.0, "fee": 0.0, "net": 0.0}
    owner = wallet_keypair()
    owner_pubkey = owner.pubkey()
    account = await token_account(str(owner_pubkey), mint)
    if not account or int(account["raw"]) != 0:
        return empty

    # The account's rent and the blockhash the closing transaction needs are
    # independent: one round trip instead of two.
    info, blockhash = await rpc_batch([
        ("getAccountInfo", [account["address"],
                            {"encoding": "jsonParsed", "commitment": "confirmed"}]),
        ("getLatestBlockhash", [{"commitment": "confirmed"}]),
    ])
    lamports = int((((info or {}).get("value") or {}).get("lamports")) or 0)
    if lamports <= 0:
        return empty

    instruction = Instruction(
        program_id=Pubkey.from_string(account["program"]),
        data=_CLOSE_ACCOUNT,
        accounts=[
            AccountMeta(Pubkey.from_string(account["address"]), False, True),
            AccountMeta(owner_pubkey, False, True),   # rent refunded here
            AccountMeta(owner_pubkey, True, False),   # owner must sign
        ],
    )
    recent = Hash.from_string(((blockhash or {}).get("value") or {})["blockhash"])
    unsigned = Transaction.new_unsigned(
        Message.new_with_blockhash([instruction], owner_pubkey, recent)
    )
    unsigned.sign([owner], recent)
    encoded = base64.b64encode(bytes(unsigned)).decode("ascii")

    signature = await rpc(
        "sendTransaction",
        [encoded, {"encoding": "base64", "skipPreflight": False,
                   "maxRetries": 3, "preflightCommitment": "confirmed"}],
        timeout=30.0,
    )
    if not signature:
        raise SolanaError("Le RPC n'a renvoyé aucune signature pour la fermeture.")
    await confirm(str(signature), timeout=30.0)

    # Read what the closing transaction really cost: its fee is part of the
    # position's PnL, and the net delta nets it against the refunded rent.
    rent = lamports / LAMPORTS_PER_SOL
    fee = 0.0
    try:
        effects = await transaction_effects(str(signature), str(owner_pubkey))
        if effects.get("fee") is not None:
            fee = float(effects["fee"])
        if effects.get("delta") is not None:
            return {"rent": rent, "fee": fee, "net": float(effects["delta"])}
    except Exception:  # noqa: BLE001
        pass
    # Fall back to the gross rent: honest about the refund, silent about the fee.
    return {"rent": rent, "fee": fee, "net": rent}


async def sol_balance(wallet: str) -> float:
    result = await rpc("getBalance", [wallet])
    # getBalance answers with a context wrapper: {"context": ..., "value": N}
    if isinstance(result, dict):
        result = result.get("value")
    return float(result or 0) / LAMPORTS_PER_SOL
