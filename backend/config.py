"""Persistent settings for SolSnipBot.

Settings live in ``data/settings.json`` and can be edited live from the UI.
Secrets (the wallet key, the GMGN pair, the Helius key, the Telegram
credentials) are entered from the dashboard and kept in
``~/.config/solsnipbot/secrets.env``, outside the project; see
:mod:`backend.secrets`. Historical ``.env`` files are still read, so an existing
setup keeps working untouched.
"""
from __future__ import annotations

import json
import pathlib
import threading
from typing import Any

from .paths import ROOT, SETTINGS_FILE

_lock = threading.RLock()


def _read_env_file(path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    text = path.read_text(encoding="utf-8", errors="ignore")
    # A BOM is invisible in an editor but becomes part of the first key name:
    # a file saved by Notepad or PowerShell 5.1 would otherwise lose its very
    # first secret (`\ufeffGMGN_API_KEY` is not `GMGN_API_KEY`).
    text = text.lstrip("\ufeff")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lstrip("\ufeff")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


from .paths import GMGN_ENV  # noqa: E402  (import after helper to keep file tidy)
from . import secrets as secret_store  # noqa: E402

# Env precedence: project .env -> global gmgn .env -> real environment.
_env: dict[str, str] = {}
_env.update(_read_env_file(GMGN_ENV))
_env.update(_read_env_file(ROOT / ".env"))


def get_env(key: str, default: str = "") -> str:
    """A credential, from the highest-priority layer that defines it.

    A real environment variable wins (an operator exporting one always keeps
    control), then the secret saved from the dashboard, then the historical
    ``.env`` files. :func:`backend.secrets.status` reports which layer answered,
    so a value that is present but overridden is never mistaken for the one in
    use.
    """
    import os

    return os.environ.get(key) or secret_store.stored(key) or _env.get(key) or default


# --------------------------------------------------------------------- schema
DEFAULT_SETTINGS: dict[str, Any] = {
    # --- Telegram channels to listen to -------------------------------------
    "channels": [
        "pumpdotfunalert",
        "gmgnsignals",
        "ai_agent_solana_0xbot",
    ],
    # --- Trading ------------------------------------------------------------
    "live_trading": True,          # master switch: submit real swaps
    # Paper mode: the whole pipeline runs for real — a real Telegram call, a real
    # GMGN analysis, real Dexscreener prices and a real Jupiter route that is
    # **quoted and simulated** — but no transaction is ever signed or broadcast.
    # While it is on, `live_trading` no longer decides anything: every trade is
    # virtual. Paper positions are kept apart from the real ones in every
    # statistic, so a test can never be read as a result.
    "paper_trading": False,
    "trade_amount_sol": 0.5,       # per-trade size in SOL
    "tp_percent": 50.0,            # take-profit, % gain
    "sl_percent": 20.0,            # stop-loss, % loss
    "trailing_percent": 10.0,      # trailing stop distance, %
    "trailing_enabled": True,
    "auto_snipe": True,            # buy automatically when all checks pass
    # One purchase per contract, for good. A token that was bought once is never
    # bought again, even after its trade closed: the channels happily re-publish
    # the same contract, and the 30-minute call de-duplication below is only a
    # memory — it is lost on restart. No source is exempt, a manual snipe
    # included: the rule protects the position list, and a manual click is
    # exactly where a second position on the same contract would be created
    # without being noticed. The only thing a manual snipe is spared is the
    # `auto_snipe` toggle.
    "snipe_once_per_token": True,
    "slippage": 30,                # swap slippage tolerance, %
    "priority_fee": 0.0001,        # SOL priority fee (cap per transaction)
    "anti_mev": True,
    "max_concurrent_positions": 5,
    # Fees are a fixed SOL amount per round trip (base fee x3 + priority fee x2),
    # so their share grows as the trade shrinks: measured 2,35-15,58 % of a
    # 0,001 SOL trade, median 10,88 %. A trade that pays more than this share of
    # itself in fees is refused; 0 disables the rule explicitly.
    "max_fee_percent": 3.0,
    # --- Execution engine (# jupiter needs no GMGN plan) --------------------
    "execution_backend": "jupiter",  # jupiter (signed locally) | gmgn (paid plan)
    "solana_rpc_url": "https://api.mainnet-beta.solana.com",
    # Used automatically when the primary node fails a read (free nodes often
    # reject getTokenAccountsByOwner, which would break every sell).
    "solana_rpc_fallback_url": "https://api.mainnet-beta.solana.com",
    # --- Snipe filters ------------------------------------------------------
    "max_market_cap": 40000.0,     # reject if the ATH market cap (USD) is above this
    "max_age_minutes": 15.0,       # reject if token older than this
    "max_top10_percent": 30.0,     # reject if top-10 holders own >= this %
    "require_mc_gt_volume": True,  # reject if market cap > 24h volume
    "require_more_buyers": True,   # reject if sellers > buyers
    "buyer_seller_window": "24h",  # 1m / 5m / 1h / 24h
    "require_website": True,
    "require_social": True,
    "require_description": False,
    "require_social_engagement": True,
    "max_social_dup": 25,          # reject if the social handle is reused by >N tokens
    "require_kol": True,
    "require_smart_wallet": True,
    "alert_prefilter": True,       # cheap rejection from the alert's own numbers
    # --- Realtime price tracking -------------------------------------------
    "price_poll_seconds": 5.0,     # exit-rule cadence (fed by the live quotes)
    # Continuous Dexscreener tracking: one batched poll shared by positions, the
    # selected token, the watchlist and the most recent calls.
    "price_feed_enabled": True,
    "price_feed_seconds": 2.0,     # how often Dexscreener is polled
    "price_feed_calls": 5,         # recent calls kept under live watch as well
    # --- Dashboard-managed secrets -----------------------------------------
    # Writing a secret (wallet key, API keys) is accepted from anywhere the
    # dashboard is reachable by default. Turning this on restricts it to the
    # machine running the bot, which is the safe answer the day the dashboard is
    # exposed beyond localhost.
    "secrets_local_only": False,
    "chain": "sol",
}

_settings: dict[str, Any] = dict(DEFAULT_SETTINGS)

# ------------------------------------------------------------------ validation
# A cleared number input in the UI arrives as `null` (parseFloat("") = NaN).
# Without validation such a value silently disabled a safety rule: an empty
# "Stop Loss" field turned the stop loss off. Every write is now checked.
NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "trade_amount_sol": (0.0, 1_000.0),
    "tp_percent": (0.0, 100_000.0),
    "sl_percent": (0.0, 99.0),
    "trailing_percent": (0.0, 99.0),
    "slippage": (0.0, 100.0),
    "priority_fee": (0.0, 1.0),
    "max_fee_percent": (0.0, 100.0),
    "max_concurrent_positions": (1, 100),
    "max_market_cap": (0.0, 1e12),
    "max_age_minutes": (0.0, 100_000.0),
    "max_top10_percent": (0.0, 100.0),
    "max_social_dup": (0.0, 1e9),
    "price_poll_seconds": (1.0, 600.0),
    "price_feed_seconds": (0.5, 60.0),
    "price_feed_calls": (0.0, 20.0),
}
_BOOLEAN_KEYS = {k for k, v in DEFAULT_SETTINGS.items() if isinstance(v, bool)}
SELECT_OPTIONS: dict[str, tuple[str, ...]] = {
    "execution_backend": ("jupiter", "gmgn"),
    # Exactly the windows GMGN's price block exposes (verified: buys_1m, buys_5m,
    # buys_1h, buys_6h, buys_24h). A window that does not exist would silently
    # fall back to the 24 h counters, i.e. a filter changing itself.
    "buyer_seller_window": ("1m", "5m", "1h", "6h", "24h"),
    "chain": ("sol",),
}

# Keys dropped because the stored value was invalid (reported in the log).
LAST_DROPPED: list[str] = []


def validate(patch: dict[str, Any]) -> dict[str, Any]:
    """Coerce and check a settings patch; raise ``ValueError`` with a UI message."""
    clean: dict[str, Any] = {}
    for key, value in (patch or {}).items():
        if key not in DEFAULT_SETTINGS:
            continue

        if key in _BOOLEAN_KEYS:
            if isinstance(value, str):
                # An unrecognised string (a typo from a script, an old client)
                # must never be read as "false": that silently turns a safety
                # filter off. Anything not clearly true/false is refused.
                token = value.strip().lower()
                if token in {"1", "true", "vrai", "oui", "yes", "on"}:
                    clean[key] = True
                elif token in {"0", "false", "faux", "non", "no", "off", ""}:
                    clean[key] = False
                else:
                    raise ValueError(f"« {key} » doit être vrai ou faux")
            elif isinstance(value, bool) or isinstance(value, (int, float)):
                clean[key] = bool(value)
            else:
                # `null` included: a cleared switch must not read as "off".
                raise ValueError(f"« {key} » doit être vrai ou faux")
            continue

        if key in NUMERIC_RANGES:
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValueError(f"« {key} » ne peut pas être vide")
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"« {key} » doit être un nombre") from None
            if number != number or number in (float("inf"), float("-inf")):
                raise ValueError(f"« {key} » doit être un nombre fini")
            low, high = NUMERIC_RANGES[key]
            if not low <= number <= high:
                raise ValueError(f"« {key} » doit être entre {low:g} et {high:g}")
            default = DEFAULT_SETTINGS[key]
            clean[key] = int(round(number)) if isinstance(default, int) else number
            continue

        if key == "channels":
            if isinstance(value, str):
                value = [part.strip() for part in value.split(",") if part.strip()]
            if not isinstance(value, list):
                raise ValueError("« channels » doit être une liste de canaux")
            channels = [str(item).strip().replace("https://t.me/", "").replace("t.me/", "")
                        for item in value if str(item).strip()]
            if not channels:
                raise ValueError("Au moins un canal Telegram est requis")
            clean[key] = channels
            continue

        if key in SELECT_OPTIONS:
            if value not in SELECT_OPTIONS[key]:
                raise ValueError(f"« {key} » doit être: {', '.join(SELECT_OPTIONS[key])}")
            clean[key] = value
            continue

        if key == "solana_rpc_url":
            url = str(value or "").strip()
            if not url.startswith(("http://", "https://")):
                raise ValueError("L'URL du RPC doit commencer par http:// ou https://")
            clean[key] = url
            continue

        if key == "solana_rpc_fallback_url":
            url = str(value or "").strip()
            if url and not url.startswith(("http://", "https://")):
                raise ValueError("L'URL du RPC de secours doit commencer par http:// ou https://")
            clean[key] = url
            continue

        clean[key] = value
    return clean


def load() -> dict[str, Any]:
    global _settings
    with _lock:
        data: dict[str, Any] = {}
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = {}
        merged = dict(DEFAULT_SETTINGS)
        dropped: list[str] = []
        for key, value in (data or {}).items():
            if key not in DEFAULT_SETTINGS:
                continue
            try:
                merged.update(validate({key: value}))
            except ValueError:
                # Never let a corrupt stored value silently disable a rule:
                # fall back to the default and report it.
                dropped.append(key)
        LAST_DROPPED.clear()
        LAST_DROPPED.extend(dropped)
        _settings = merged
        return dict(_settings)


def save() -> None:
    with _lock:
        SETTINGS_FILE.write_text(
            json.dumps(_settings, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def all() -> dict[str, Any]:
    with _lock:
        return dict(_settings)


def get(key: str, default: Any = None) -> Any:
    with _lock:
        return _settings.get(key, DEFAULT_SETTINGS.get(key, default))


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a validated patch. Raises ``ValueError`` on an invalid value."""
    clean = validate(patch)
    with _lock:
        _settings.update(clean)
        save()
        return dict(_settings)


# ----------------------------------------------------------------------- mode
# Whether an order can leave the machine is decided by two booleans, and reading
# them apart is precisely how a user comes to believe they are trading live when
# they are not. The mode is therefore named here, once, and every consumer — the
# dashboard switch, the settings panel, ``/api/mode`` — reads this same answer
# rather than re-deriving it.
MODE_PAPER = "paper"
MODE_LIVE = "live"
MODE_OBSERVE = "observation"

MODE_NAMES = {
    MODE_PAPER: "PAPIER",
    MODE_LIVE: "RÉEL",
    MODE_OBSERVE: "OBSERVATION",
}


def mode_state(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """The trading mode, in one place and in words.

    ``can_send_orders`` is the fact that matters: it is true only outside paper
    mode *and* with the ``live_trading`` master switch on. ``auto_orders`` narrows
    that further to automatic snipes, which ``auto_snipe`` gates — a manual snipe
    trades with ``live_trading`` alone. « Un seul achat par token » is not part of
    this distinction: it applies to both.
    """
    values = all() if settings is None else settings
    paper = bool(values.get("paper_trading", DEFAULT_SETTINGS["paper_trading"]))
    live = bool(values.get("live_trading", DEFAULT_SETTINGS["live_trading"]))
    auto = bool(values.get("auto_snipe", DEFAULT_SETTINGS["auto_snipe"]))
    if paper:
        key = MODE_PAPER
        label = "PAPIER — appels, analyses et prix réels, aucun ordre envoyé"
    elif live:
        key = MODE_LIVE
        label = ("RÉEL — snipes automatiques activés" if auto
                 else "RÉEL — sniper automatique désactivé")
    else:
        key = MODE_OBSERVE
        label = "OBSERVATION — aucun ordre automatique"
    return {
        "key": key,
        "name": MODE_NAMES[key],
        "label": label,
        "paper": paper,
        "live": live,
        "auto_snipe": auto,
        "can_send_orders": (not paper) and live,
        "auto_orders": (not paper) and live and auto,
    }


# ---------------------------------------------------------------- credentials
def gmgn_api_key() -> str:
    return get_env("GMGN_API_KEY")


def gmgn_private_key() -> str:
    return get_env("GMGN_PRIVATE_KEY")


def wallet_private_key() -> str:
    """Solana wallet signing key.

    In order: ``SOLANA_WALLET_PRIVATE_KEY`` (environment, or the secret saved
    from the dashboard), then the historical ``~/.config/solsnipbot/wallet.key``.
    """
    raw = (get_env("SOLANA_WALLET_PRIVATE_KEY") or "").strip()
    if raw:
        return raw
    key_file = pathlib.Path.home() / ".config" / "solsnipbot" / "wallet.key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    return ""


def telegram_api_id() -> int:
    """Telegram API ID, read from the environment or the local `.env`.

    The value is deliberately *not* in the source: a repository is a public
    artefact sooner or later, and an API pair identifies the application itself.
    Set ``TELEGRAM_API_ID`` in `.env` (see `.env.example`).
    """
    raw = get_env("TELEGRAM_API_ID", "")
    try:
        return int(raw)
    except ValueError:
        return 0


def telegram_api_hash() -> str:
    return get_env("TELEGRAM_API_HASH", "")


def telegram_phone() -> str:
    return get_env("TELEGRAM_PHONE", "")


load()
