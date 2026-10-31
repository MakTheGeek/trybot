"""Cheap pre-filter on the Telegram alert text.

The sniper channels publish machine-readable alerts that already carry the token
age, the top-10 concentration, the market cap and the holder count. Reading those
values lets an obvious non-candidate be rejected in microseconds instead of
spending a rate-limited GMGN request on it — which is what keeps the analysis
queue short enough to still catch a fresh token.

Two rules keep this safe:

* it can only ever **reject**, never accept;
* it only fires on a **clear** violation of a configured ceiling, with a margin,
  because the alert bot's numbers are rounded. Anything borderline is left to the
  full GMGN analysis, which stays the single source of truth for the buy.
"""
from __future__ import annotations

import re
import time
import uuid

# The alert bot rounds its numbers, so only reject well past the ceiling.
TOP10_MARGIN_PCT = 2.0      # alert top-10 must exceed the max by this much
MC_MARGIN = 1.05            # ... and market cap by this fraction

_AGE_RE = re.compile(
    r"open\s*[:\s]\s*(\d+(?:[.,]\d+)?)\s*"
    r"(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)"
    r"\s*ago",
    re.I,
)
_TOP10_RE = re.compile(r"top\s*10\s*[:\s]\s*([\d.,]+)\s*%", re.I)
_HOLDER_RE = re.compile(r"holders?\s*[:\s]\s*(\d+)", re.I)
_MC_RES = (
    re.compile(r"\bmcp\s*[:\s]\s*\$?\s*([\d.,]+)\s*([kmb])?", re.I),
    re.compile(r"\bmarket\s*cap\s*[:\s]\s*\$?\s*([\d.,]+)\s*([kmb])?", re.I),
)
_SYMBOL_RE = re.compile(r"\$+([^\n(]{1,40}?)\s*\(([^)\n]{0,60})\)")

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}
_MULTIPLIER = {"": 1.0, "k": 1e3, "m": 1e6, "b": 1e9}

_MAX_AGE_SANITY = 30 * 86400.0   # ignore anything more absurd than 30 days


def _number(raw: str, suffix: str = "") -> float:
    try:
        value = float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0
    return value * _MULTIPLIER.get((suffix or "").lower(), 1.0)


def _check(cid: str, label: str, detail: str, value=None, threshold=None) -> dict:
    return {
        "id": cid,
        "label": label,
        "passed": False,
        "detail": detail,
        "value": value,
        "threshold": threshold,
    }


def parse_alert(text: str) -> dict:
    """Extract the machine-readable hints an alert carries (all optional)."""
    text = text or ""
    hints: dict = {
        "age_seconds": None,
        "age_label": "",
        "top10_pct": None,
        "market_cap": None,
        "holders": None,
        "symbol": "",
        "name": "",
    }

    match = _AGE_RE.search(text)
    if match:
        seconds = _number(match.group(1)) * _UNIT_SECONDS.get(match.group(2).lower(), 0)
        if 0 <= seconds <= _MAX_AGE_SANITY:
            hints["age_seconds"] = seconds
            hints["age_label"] = match.group(0).strip()

    match = _TOP10_RE.search(text)
    if match:
        pct = _number(match.group(1))
        if 0.0 <= pct <= 100.0:
            hints["top10_pct"] = pct

    for pattern in _MC_RES:
        match = pattern.search(text)
        if match:
            value = _number(match.group(1), match.group(2) or "")
            if value > 0:
                hints["market_cap"] = value
                break

    match = _HOLDER_RE.search(text)
    if match:
        hints["holders"] = int(match.group(1))

    match = _SYMBOL_RE.search(text)
    if match:
        hints["symbol"] = match.group(1).strip()
        hints["name"] = match.group(2).strip()

    return hints


def prefilter(text: str, settings: dict, *, elapsed: float = 0.0) -> dict:
    """Evaluate the alert's own numbers against the configured ceilings.

    Returns the parsed hints plus the list of checks that *clearly* fail. An
    empty list means "let the full GMGN analysis decide".
    """
    hints = parse_alert(text)
    checks: list[dict] = []

    max_age = float(settings.get("max_age_minutes") or 0)
    if max_age > 0 and hints["age_seconds"] is not None:
        age_minutes = (hints["age_seconds"] + max(elapsed, 0.0)) / 60.0
        if age_minutes > max_age:
            checks.append(_check(
                "age",
                f"Âge ≤ {max_age:g} min",
                f"âge {age_minutes:.1f} min (alerte: {hints['age_label']})",
                round(age_minutes, 2),
                max_age,
            ))

    max_top10 = float(settings.get("max_top10_percent") or 0)
    if max_top10 > 0 and hints["top10_pct"] is not None:
        if hints["top10_pct"] >= max_top10 + TOP10_MARGIN_PCT:
            checks.append(_check(
                "top10",
                f"Top 10 holders < {max_top10:g}%",
                f"{hints['top10_pct']:.1f}% détenus par le top 10 (alerte)",
                round(hints["top10_pct"], 2),
                max_top10,
            ))

    max_mc = float(settings.get("max_market_cap") or 0)
    if max_mc > 0 and hints["market_cap"] is not None:
        # The ceiling applies to the ATH, which is by definition >= the current
        # market cap, so a current value already above it is a certain reject.
        if hints["market_cap"] > max_mc * MC_MARGIN:
            checks.append(_check(
                "market_cap",
                f"MarketCap ATH ≤ ${max_mc:,.0f}",
                f"MC ${hints['market_cap']:,.0f} (alerte, au-dessus du plafond ATH)",
                round(hints["market_cap"], 2),
                max_mc,
            ))

    return {
        "hints": hints,
        "checks": checks,
        "reject": bool(checks),
        "symbol": hints["symbol"],
        "name": hints["name"],
    }


def build_report(job: dict, result: dict, *, chain: str = "sol") -> dict:
    """Shape a pre-filter rejection like a normal analysis report."""
    hints = result.get("hints") or {}
    checks = result.get("checks") or []
    now_ms = int(time.time() * 1000)
    age_seconds = hints.get("age_seconds")
    return {
        "id": "call_" + uuid.uuid4().hex[:10],
        "address": job.get("address", ""),
        "chain": chain,
        "name": result.get("name") or result.get("symbol") or "?",
        "symbol": result.get("symbol") or "?",
        "logo": "",
        "source": job.get("source", "telegram"),
        "channel": job.get("channel", ""),
        "message": (job.get("message") or "")[:400],
        "ts": now_ms,
        "tx_ts": now_ms // 1000,
        "decision": "rejected",
        "reasons": [c["detail"] for c in checks],
        "failed_ids": [c["id"] for c in checks],
        "checks": checks,
        "created_at": 0,
        "market_cap": round(float(hints.get("market_cap") or 0.0), 2),
        "ath_market_cap": 0,
        "ath_price": 0,
        "volume_24h": 0,
        "liquidity": 0,
        "top10_pct": round(float(hints.get("top10_pct") or 0.0), 2),
        "buyers": 0,
        "sellers": 0,
        "age_minutes": round((age_seconds or 0.0) / 60.0, 2),
        "holders": int(hints.get("holders") or 0),
        "kols": 0,
        "smart_wallets": 0,
        "sniper_wallets": 0,
        "rug_ratio": 0,
        "visiting_count": 0,
        "price_usd": 0.0,
        "price_at_call": 0.0,
        "website": "",
        "socials": [],
        "description": "",
        "twitter": "",
        "telegram": "",
        "has_website": False,
        "has_social": False,
        "creator": "",
        "launchpad": "",
        "is_honeypot": False,
        "price_window": "24h",
        "prefiltered": True,
        "executed": False,
        "execution": {
            "executed": False,
            "reason": "Rejeté par le pré-filtre du message d'alerte (aucun appel GMGN consommé).",
        },
        "error": False,
    }
