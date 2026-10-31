"""SolSnipBot web server: REST API + WebSocket + static dashboard."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, fees, secrets, solana_rpc
from .bot import bot
from .clock import clock
from .dexscreener import dexscreener
from .gmgn import CHAIN, gmgn
from .paths import FRONTEND_DIR, SECRETS_FILE
from .pricefeed import price_feed
from .state import store
from .telegram_listener import get_listener
from .trader import get_sol_price, resolve_wallet, trader


def gmgn_module_min_gap() -> float:
    return gmgn.min_gap


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.LAST_DROPPED:
        store.log(
            "Paramètres invalides ignorés au chargement (valeurs par défaut "
            f"rétablies) : {', '.join(config.LAST_DROPPED)}",
            level="warn", action="settings",
        )
    store.runtime["telegram_phone"] = config.telegram_phone()
    # Say which mode the bot woke up in, once, loudly: "no real order is sent" is
    # the one thing that must never be a guess.
    _paper = bool(config.get("paper_trading"))
    _live = bool(config.get("live_trading"))
    store.runtime["trading_mode"] = config.mode_state()["key"]
    if _paper:
        store.log(
            "📝 MODE PAPIER actif — appels, analyses, prix et routes sont réels, "
            "mais aucun ordre n'est signé ni envoyé. Les résultats papier sont "
            "comptés séparément des résultats réels.",
            level="warn", action="settings",
        )
    with contextlib.suppress(Exception):
        await solana_rpc.start_background()
    with contextlib.suppress(Exception):
        await bot.start()
    yield
    with contextlib.suppress(Exception):
        await bot.stop()
    with contextlib.suppress(Exception):
        await dexscreener.close()
    with contextlib.suppress(Exception):
        await solana_rpc.stop_background()


app = FastAPI(title="SolSnipBot", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------- models
class SettingsPatch(BaseModel):
    model_config = {"extra": "allow"}


class SnipeRequest(BaseModel):
    address: str


class CloseRequest(BaseModel):
    percent: float = 100.0


class PhoneRequest(BaseModel):
    phone: str = ""


class CodeRequest(BaseModel):
    code: str


class PasswordRequest(BaseModel):
    password: str


class FeedTrackRequest(BaseModel):
    address: str = ""
    kind: str = "watch"        # watch | select
    action: str = "add"        # add | remove


class SecretRequest(BaseModel):
    key: str
    value: str = ""


class SecretTestRequest(BaseModel):
    key: str


class ModeRequest(BaseModel):
    mode: str                              # "paper" | "live"
    # Only ever set when the user has explicitly armed real trading; left unset,
    # the master switch is untouched, so flipping the switch can never, on its
    # own, put real money at risk.
    live_trading: bool | None = None


# ----------------------------------------------------------------------- core
async def _rpc_block() -> dict:
    """The node layer in one object: health, socket, and fee-cap adequacy."""
    return {
        **solana_rpc.rpc_health(),
        "priority_fee": solana_rpc.priority_fee_advice(
            await solana_rpc.cached_priority_fee_levels(), config.all()
        ),
    }


@app.get("/api/state")
async def api_state():
    snap = store.snapshot()
    settings = config.all()
    snap["settings"] = settings
    snap["feed"] = price_feed.status()
    # Live cost of a round trip, so the UI can say what the current amount and
    # priority fee really cost before a trade is attempted.
    snap["fees"] = fees.fee_gate(settings)
    snap["rpc"] = await _rpc_block()
    # The mode, stated by the server rather than inferred by the browser from two
    # booleans: the dashboard switch and the paper banner must name the same mode.
    snap["mode"] = config.mode_state(settings)
    return snap


@app.get("/api/health")
async def api_health():
    mode = config.mode_state()
    return {
        "ok": True,
        "gmgn_cli": gmgn.available,
        "clock": {"drift_seconds": round(clock.drift_seconds, 1), "measured": clock.measured},
        "telegram": store.runtime.get("telegram"),
        "wallet": store.runtime.get("wallet"),
        "gmgn": {
            "requests": gmgn.request_count,
            "cooling_down": gmgn.cooling_down,
            "cooldown_remaining_s": round(gmgn.cooldown_remaining(), 1),
            "min_gap_s": gmgn_module_min_gap(),
        },
        "queue": {"depth": bot.queue.qsize(), "processed": bot.processed, "skipped": bot.skipped},
        "price_feed": price_feed.counters(),
        # Which mode the bot is actually in, stated in one place so neither the UI
        # nor a check script has to infer it from two booleans.
        "trading": {
            "mode": mode["key"],
            "name": mode["name"],
            "paper": mode["paper"],
            "live": mode["live"],
            "auto_snipe": mode["auto_snipe"],
            "can_send_orders": mode["can_send_orders"],
        },
        "fees": fees.fee_gate(config.all()),
        # The node layer, stated in one place: which endpoint is configured (key
        # masked), how it is really behaving, whether the socket is live, and
        # whether the priority-fee cap is above or below the going rate.
        "rpc": await _rpc_block(),
    }


# ------------------------------------------------------------------- settings
def _announce_mode(updated: dict, *, announce: bool = True, origin: str = "") -> dict:
    """Publish the mode: the runtime badge, one log line, and the same answer.

    The three states are announced separately because two of them are easy to
    confuse when only the two booleans are read: turning "Mode papier" off does
    **not** make the bot trade unless the "Trading réel" master switch is on as
    well, and a line that said "RÉEL" at that point would be the most expensive
    kind of wrong. ``origin`` names where the change came from — never a secret.
    """
    mode = config.mode_state(updated)
    store.runtime["trading_mode"] = mode["key"]
    if not announce:
        return mode
    where = f" (demandé depuis {origin})" if origin else ""
    if mode["key"] == config.MODE_PAPER:
        message = ("📝 Mode PAPIER activé — aucun ordre réel ne sera envoyé (les "
                   "positions réelles déjà ouvertes restent, elles, vendues pour "
                   "de vrai).")
    elif mode["key"] == config.MODE_LIVE:
        message = ("Mode RÉEL activé — les snipes engagent à nouveau de vrais SOL. "
                   "Les positions papier restent virtuelles.")
    else:
        message = ("Mode OBSERVATION — « Mode papier » est éteint, mais « Trading "
                   "réel » l'est aussi : le bot analyse et suit les prix, aucun "
                   "ordre ne partira.")
    store.log(message + where, level="warn", action="settings")
    return mode


@app.get("/api/settings")
async def api_get_settings():
    return config.all()


@app.post("/api/settings")
async def api_set_settings(patch: dict):
    allowed = set(config.DEFAULT_SETTINGS.keys())
    clean = {k: v for k, v in patch.items() if k in allowed}
    if not clean:
        raise HTTPException(400, "Aucun paramètre valide fourni")
    try:
        updated = config.update(clean)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Switching the mode has to be visible everywhere at once, including in the
    # badge: a bot that trades must never be ambiguous about which mode it is in.
    _announce_mode(updated, announce="paper_trading" in clean or "live_trading" in clean)
    store.log("Configuration de trading mise à jour", level="info", action="settings")
    store.broadcast({"type": "settings", "settings": updated, "fees": fees.fee_gate(updated)})
    return updated


@app.post("/api/mode")
async def api_set_mode(change: ModeRequest, request: Request):
    """Switch between the paper book and real money, in one explicit act.

    A dedicated endpoint rather than one more field of the settings form, because
    this is the one setting where the gap between "saved" and "in force" is real
    money. It answers with the mode the server is actually in — including whether
    an order can leave at all — so the dashboard never has to infer it.
    """
    wanted = str(change.mode or "").strip().lower()
    if wanted not in {config.MODE_PAPER, config.MODE_LIVE}:
        raise HTTPException(400, "Mode inconnu : attendu « paper » ou « live » "
                                 f"(reçu «{change.mode}»).")
    patch: dict = {"paper_trading": wanted == config.MODE_PAPER}
    if change.live_trading is not None:
        patch["live_trading"] = bool(change.live_trading)
    try:
        updated = config.update(patch)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    mode = _announce_mode(updated, origin=_client_ip(request))
    notes: list[str] = []
    if mode["key"] == config.MODE_PAPER:
        notes.append("Les positions réelles déjà ouvertes continueront d'être "
                     "vendues pour de vrai.")
    elif mode["key"] == config.MODE_OBSERVE:
        notes.append("« Trading réel » est toujours désactivé : le bot analyse et "
                     "suit les prix, mais aucun ordre ne partira.")
    else:
        notes.append("Les prochains snipes engagent de vrais SOL.")
    balance = store.runtime.get("wallet_balance_sol")
    amount = float(config.get("trade_amount_sol") or 0.0)
    if balance is not None and not mode["paper"] and float(balance) < amount:
        notes.append(f"Solde du wallet : {float(balance):.4f} SOL — un achat de "
                     f"{amount:g} SOL serait refusé (fonds insuffisants).")
    store.broadcast({"type": "settings", "settings": updated,
                     "fees": fees.fee_gate(updated)})
    return {"mode": mode, "settings": updated, "notes": notes,
            "balance_sol": None if balance is None else float(balance),
            "trade_amount_sol": amount}


# --------------------------------------------------------------------- secrets
# The keys the bot runs on can be entered from the dashboard. Three rules hold:
# the stored value never leaves the server (only a mask is returned), a write is
# logged with its origin address, and the change takes effect at once — the
# wallet is re-derived and the RPC socket reconnected, so nobody has to restart
# the bot to know whether a new key works.
def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else ""


def _is_local(ip: str) -> bool:
    return ip in {"", "::1", "localhost"} or ip.startswith("127.")


def _guard_secret_write(request: Request) -> str:
    ip = _client_ip(request)
    if config.get("secrets_local_only") and not _is_local(ip):
        raise HTTPException(
            403,
            "L'enregistrement des secrets est verrouillé sur la machine locale "
            "(« secrets_local_only »). Désactivez ce verrou depuis le poste qui "
            "fait tourner le bot pour écrire depuis ailleurs.",
        )
    return ip


def _secrets_payload(request: Request | None = None) -> dict:
    ip = _client_ip(request) if request is not None else ""
    return {
        "items": secrets.status(),
        "file": str(SECRETS_FILE),
        "local_only": bool(config.get("secrets_local_only")),
        "client_local": _is_local(ip),
        "client_ip": ip,
        "helius_in_use": secrets.helius_in_use(),
        "inline_keys": [name for name, _ in secrets.clear_key_urls()],
    }


async def _write_helius_placeholder() -> list[str]:
    """Move an inline Helius key out of ``data/settings.json``.

    Saving a Helius key is pointless while the RPC URL carries another one in
    the clear: the URL wins. Rewriting it to the ``{api_key}`` placeholder puts
    the key in one place, and the URL stays readable in the settings view.
    """
    inline = secrets.clear_key_urls()
    if not inline:
        return []
    patch: dict[str, str] = {}
    for name, url in inline:
        patch[name] = secrets.placeholder_url(url)
    updated = config.update(patch)
    store.broadcast({"type": "settings", "settings": updated,
                     "fees": fees.fee_gate(updated)})
    return ["URL RPC réécrite pour lire la clé depuis le fichier des secrets au "
            "lieu de la garder en clair dans data/settings.json : "
            + ", ".join(patch)]


async def _apply_secret(key: str) -> list[str]:
    """Make a freshly stored secret take effect without restarting the bot."""
    notes: list[str] = []

    if key == "SOLANA_WALLET_PRIVATE_KEY":
        try:
            address, balance = await resolve_wallet()
            if address:
                notes.append(f"Wallet actif : {address} — {balance:.4f} SOL")
            else:
                notes.append("Aucune adresse n'a pu être dérivée : la clé est "
                             "peut-être invalide.")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Wallet non rechargé : {exc}")

    if key in {"HELIUS_API_KEY", "SOLANA_RPC_URL", "SOLANA_WS_URL"}:
        try:
            notes.extend(await _write_helius_placeholder())
            await solana_rpc.stop_background()
            await solana_rpc.start_background()
            notes.append("Connexion RPC relancée : le prochain appel utilise le "
                         "nouveau réglage.")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Connexion RPC non relancée : {exc}")

    if key.startswith("TELEGRAM_"):
        notes.append("Telegram : déconnectez puis reconnectez le listener "
                     "(bouton « Connecter Telegram ») pour appliquer ce réglage.")

    return notes


@app.get("/api/credentials")
async def api_credentials(request: Request):
    """Masked state of every managed secret — never a stored value."""
    return _secrets_payload(request)


@app.post("/api/credentials")
async def api_set_credential(req: SecretRequest, request: Request):
    ip = _guard_secret_write(request)
    key = (req.key or "").strip()
    spec = secrets.BY_KEY.get(key)
    if spec is None:
        raise HTTPException(400, f"Secret inconnu : {req.key}")
    try:
        result = secrets.set_secret(key, req.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    notes = await _apply_secret(key)
    # The label, never the value.
    store.log(
        f"🔑 {spec['label']} enregistré depuis le tableau de bord "
        f"({ip or 'origine inconnue'}) — {result.get('message', '')}".rstrip(" —"),
        level="warn", action="secret",
    )
    payload = _secrets_payload(request)
    store.broadcast({"type": "credentials", "credentials": payload})
    return {**result, "notes": notes, "credentials": payload}


@app.delete("/api/credentials/{key}")
async def api_clear_credential(key: str, request: Request):
    ip = _guard_secret_write(request)
    spec = secrets.BY_KEY.get(key)
    if spec is None:
        raise HTTPException(400, f"Secret inconnu : {key}")
    try:
        result = secrets.clear(key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    notes = await _apply_secret(key)
    store.log(
        f"🔑 {spec['label']} retiré du tableau de bord ({ip or 'origine inconnue'}) "
        "— la source précédente reprend la main.",
        level="warn", action="secret",
    )
    payload = _secrets_payload(request)
    store.broadcast({"type": "credentials", "credentials": payload})
    return {**result, "notes": notes, "credentials": payload}


@app.post("/api/credentials/test")
async def api_test_credential(req: SecretTestRequest):
    """Check the value in force for real, without spending anything."""
    if req.key not in secrets.BY_KEY:
        raise HTTPException(400, f"Secret inconnu : {req.key}")
    return await secrets.test(req.key)


@app.post("/api/trading/reset")
async def api_trading_reset():
    """Re-arm trading after an upgrade.

    Clears the PLAN_UPGRADE_REQUIRED block, then re-checks with a fund-free
    ``order quote`` probe so the UI shows the real plan state.
    """
    was_blocked = bool(store.runtime.get("trading_blocked"))
    store.runtime["trading_blocked"] = False
    store.runtime.pop("trading_blocked_reason", None)
    store.runtime.pop("trading_blocked_url", None)
    if was_blocked:
        store.log("Vérification du plan GMGN…", level="info", action="settings")
    probe = await trader.probe_trading_plan()
    if probe == "ok" and was_blocked:
        store.log("Trading réactivé — le plan GMGN autorise le trading via l'API",
                  level="success", action="settings")
    store.broadcast({"type": "status"})
    return {
        "ok": True,
        "was_blocked": was_blocked,
        "probe": probe,
        "trading_blocked": bool(store.runtime.get("trading_blocked")),
    }


# ------------------------------------------------------------------- portfolio
@app.get("/api/positions")
async def api_positions():
    return store.positions


@app.post("/api/positions/{position_id}/close")
async def api_close_position(position_id: str, req: CloseRequest):
    result = await bot.close_position(position_id, req.percent)
    if not result.get("ok"):
        store.broadcast({"type": "status"})
    return result


# --------------------------------------------------------------------- sniper
@app.post("/api/snipe")
async def api_snipe(req: SnipeRequest):
    address = (req.address or "").strip()
    if len(address) < 32:
        raise HTTPException(400, "Adresse de contrat invalide")
    report = await bot.force_snipe(address)
    return report


# --------------------------------------------------------------------- tokens
@app.get("/api/token/{address}")
async def api_token(address: str):
    try:
        info = await gmgn.token_info(address, chain=CHAIN)
    except Exception as exc:  # noqa: BLE001 - never 500 the dashboard
        info = {"error": str(exc)}
    dex = await dexscreener.token(address)
    if isinstance(info, dict):
        info.setdefault("market_cap", (dex or {}).get("market_cap"))
    return {"info": info, "dex": dex}


@app.get("/api/kline/{address}")
async def api_kline(address: str, resolution: str = "1m", limit: int = 300):
    rate_limited = False
    try:
        candles = await gmgn.kline(address, resolution=resolution, chain=CHAIN)
    except Exception:  # noqa: BLE001 - degrade gracefully while rate limited
        candles = []
        rate_limited = True
    if isinstance(candles, list) and limit and len(candles) > limit:
        candles = candles[-limit:]
    return {"resolution": resolution, "candles": candles or [], "rate_limited": rate_limited}


@app.get("/api/calls")
async def api_calls():
    return list(store.calls)


@app.get("/api/logs")
async def api_logs():
    return list(store.logs)


# ------------------------------------------------------------------- telegram
@app.get("/api/telegram/status")
async def api_telegram_status():
    listener = get_listener(bot.handle_call)
    return {
        "status": listener.status,
        "me": listener.me,
        "channels": config.get("channels", []),
        "resolved": listener.resolved_channels(),
        "phone": config.telegram_phone(),
    }


@app.post("/api/telegram/send_code")
async def api_telegram_send_code(req: PhoneRequest):
    listener = get_listener(bot.handle_call)
    phone = (req.phone or config.telegram_phone()).strip()
    if not phone:
        raise HTTPException(400, "Numéro de téléphone requis")
    return await listener.send_code(phone)


@app.post("/api/telegram/sign_in")
async def api_telegram_sign_in(req: CodeRequest):
    listener = get_listener(bot.handle_call)
    return await listener.sign_in_code(req.code.strip())


@app.post("/api/telegram/password")
async def api_telegram_password(req: PasswordRequest):
    listener = get_listener(bot.handle_call)
    return await listener.sign_in_password(req.password)


@app.post("/api/telegram/logout")
async def api_telegram_logout():
    listener = get_listener(bot.handle_call)
    await listener.stop()
    return {"ok": True}


# ------------------------------------------------------------------- refresh
@app.post("/api/refresh")
async def api_refresh():
    await clock.sync()
    store.runtime["clock_drift"] = round(clock.drift_seconds, 1)
    await trader.refresh_wallet()
    await get_sol_price()
    store.recompute_stats()
    store.broadcast({"type": "status"})
    return {"ok": True, "runtime": store.runtime}


# ------------------------------------------------------- realtime price feed
# A Solana address is base58 and 32-44 characters: anything else is a typo, and
# tracking it would just poll Dexscreener for a token that cannot exist.
_SOL_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@app.post("/api/feed/track")
async def api_feed_track(req: FeedTrackRequest):
    address = (req.address or "").strip()
    if not _SOL_ADDRESS.match(address):
        raise HTTPException(
            status_code=400,
            detail="Adresse Solana invalide (base58, 32 à 44 caractères).",
        )
    kind = req.kind if req.kind in ("watch", "select") else "watch"
    if req.action == "remove":
        price_feed.untrack(address, kind=kind)
    elif kind == "select":
        price_feed.select(address)
    else:
        symbol = ""
        for pos in store.positions:
            if pos.get("address") == address:
                symbol = pos.get("symbol") or ""
                break
        price_feed.track(address, kind="watch", symbol=symbol)
    store.broadcast({"type": "feed_status", "feed": price_feed.counters()})
    return {"ok": True, "feed": price_feed.counters()}


@app.get("/api/feed")
async def api_feed():
    return price_feed.status()


@app.get("/api/debug/tasks")
async def api_debug_tasks():
    """Diagnostic: where are the asyncio tasks currently blocked?"""
    import asyncio as _asyncio

    out = []
    for task in _asyncio.all_tasks():
        if task.done():
            continue
        stack = task.get_stack(limit=4)
        frames = []
        for frame in stack:
            frames.append(f"{frame.f_code.co_filename.split('\\\\')[-1]}:{frame.f_lineno} in {frame.f_code.co_name}")
        out.append({
            "name": task.get_name(),
            "coro": getattr(task.get_coro(), "__qualname__", str(task.get_coro())),
            "stack": frames,
        })
    return {"tasks": out, "gmgn_lock_locked": gmgn._lock.locked()}


# ------------------------------------------------------------------- websocket
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    def on_event(event: dict) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    store.subscribe(on_event)
    try:
        snap = store.snapshot()
        snap["settings"] = config.all()
        snap["feed"] = price_feed.status()
        snap["fees"] = fees.fee_gate(snap["settings"])
        snap["mode"] = config.mode_state(snap["settings"])
        await ws.send_json({"type": "snapshot", **snap})
        while True:
            event = await queue.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        store.unsubscribe(on_event)


# --------------------------------------------------------------------- static
class RevalidatingStatic(StaticFiles):
    """Serve the dashboard assets with ``Cache-Control: no-cache``.

    The ETag still saves bandwidth — an unchanged file answers ``304`` — but the
    browser has to ask every time. Without it the browser's heuristic cache kept
    serving a **stale** ``app.js`` for minutes after an edit: the change was on
    disk, the server served it, and the dashboard still showed the old text. That
    is the one failure mode that makes a UI untrustworthy, and it silently broke
    the ``?v=`` cache-buster (which only ever applied to ``index.html``).
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


if FRONTEND_DIR.exists():
    app.mount("/static", RevalidatingStatic(directory=str(FRONTEND_DIR)), name="static")


# Assets referenced by index.html, matched so a fingerprint can be tagged on.
_ASSET_RE = re.compile(r"/static/[\w\-./]+\.(?:js|css)")
# (path, mtime, size) -> fingerprint, so the hash is computed once per edit.
_FINGERPRINTS: dict[tuple[str, float, int], str] = {}


def _fingerprint(rel: str) -> str:
    """Short content hash of a file, so an edit always yields a new URL."""
    path = FRONTEND_DIR / rel
    try:
        stat = path.stat()
    except OSError:
        return "0"
    key = (str(path), stat.st_mtime, stat.st_size)
    cached = _FINGERPRINTS.get(key)
    if cached:
        return cached
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    except OSError:
        digest = "0"
    _FINGERPRINTS[key] = digest
    return digest


@app.get("/")
async def index():
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse({"error": "Frontend introuvable"}, status_code=500)
    html = index_file.read_text(encoding="utf-8")

    def stamp(match: re.Match[str]) -> str:
        url = match.group(0)
        rel = url.removeprefix("/static/")
        return f"{url}?v={_fingerprint(rel)}"

    # ``index.html`` is served ``no-cache``, and the assets it points at carry a
    # content hash: an edited ``app.js`` gets a brand-new URL, so the browser can
    # never keep running yesterday's dashboard behind a stale HTTP cache entry.
    # The ETag alone was not enough — a copy cached *before* ``no-cache`` was
    # added stayed "fresh" by heuristic and kept overriding the fix.
    return HTMLResponse(_ASSET_RE.sub(stamp, html), headers={"Cache-Control": "no-cache"})
