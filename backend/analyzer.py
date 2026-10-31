"""Snipe decision engine.

Takes a contract address and evaluates it against the configured buy filters
using live GMGN on-chain analytics. Every check is recorded with its value and
threshold so the UI can explain exactly why a token was sniped or rejected.
"""
from __future__ import annotations

import asyncio
from typing import Any

from . import config, fees
from .clock import clock
from .dexscreener import dexscreener
from .gmgn import CHAIN, gmgn


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _socials(link: dict) -> list[str]:
    out: list[str] = []
    for key in ("twitter_username", "telegram", "discord", "website", "youtube", "tiktok", "instagram", "reddit", "medium"):
        value = (link or {}).get(key)
        if value:
            out.append(key)
    return out


def _check(cid: str, label: str, passed: bool, detail: str, value: Any = None,
           threshold: Any = None) -> dict:
    return {
        "id": cid,
        "label": label,
        "passed": bool(passed),
        "detail": detail,
        "value": value,
        "threshold": threshold,
    }


async def analyze(address: str, *, source: str = "telegram", message: str = "", channel: str = "") -> dict:
    """Fetch live data for ``address`` and return a full snipe report."""
    settings = config.all()
    created = clock.now_ts()

    info, dex = await asyncio.gather(
        gmgn.token_info(address, chain=CHAIN),
        dexscreener.token(address),
    )

    # ---- price / supply / market cap --------------------------------------
    price_block = info.get("price") or {}
    price_usd = _f(price_block.get("price"))
    total_supply = _f(info.get("total_supply")) or _f(info.get("max_supply"))
    gmgn_mc = price_usd * total_supply

    market_cap = _f(dex.get("market_cap")) or gmgn_mc
    volume_24h = _f(dex.get("volume_24h")) or _f(price_block.get("volume_24h"))
    liquidity = _f(dex.get("liquidity_usd")) or _f(info.get("liquidity"))

    # All-time-high market cap. The user's ceiling is an ATH ceiling: a token
    # that already pumped above it and came back down must still be rejected, so
    # we compare the *peak* and not the current value. GMGN gives `ath_price`;
    # we value it with the same supply basis as the live market cap. When the
    # ATH is missing we fall back to the current market cap, which can only make
    # the check stricter than reality, never more permissive.
    supply = _f(info.get("circulating_supply")) or total_supply
    ath_price = _f(info.get("ath_price"))
    ath_market_cap = ath_price * supply if ath_price > 0 else 0.0
    ath_market_cap = max(ath_market_cap, market_cap)

    stat = info.get("stat") or {}
    tags = info.get("wallet_tags_stat") or {}
    link = info.get("link") or {}
    dev = info.get("dev") or {}

    # The security endpoint is only needed for the honeypot gate, which is
    # evaluated last, so it is not requested here.
    security: dict = {}

    raw_top10 = stat.get("top_10_holder_rate")
    if raw_top10 in (None, ""):
        # Rare: concentration missing from token info -> pay for security.
        security = await gmgn.token_security(address, chain=CHAIN)
        raw_top10 = security.get("top_10_holder_rate")
    top10 = _f(raw_top10)
    top10_pct = top10 * 100.0 if top10 <= 1.0 else top10

    # ---- age ---------------------------------------------------------------
    created_ts = int(_f(info.get("creation_timestamp")) or _f(info.get("open_timestamp")))
    age_seconds = max(created - created_ts, 0) if created_ts else 0
    age_minutes = age_seconds / 60.0

    # ---- buyers vs sellers -------------------------------------------------
    window = settings.get("buyer_seller_window", "24h")
    buys = _f(price_block.get(f"buys_{window}"))
    sells = _f(price_block.get(f"sells_{window}"))
    if buys == 0 and sells == 0:
        buys = _f(dex.get("buys", {}).get("h24")) if isinstance(dex.get("buys"), dict) else 0
        sells = _f(dex.get("sells", {}).get("h24")) if isinstance(dex.get("sells"), dict) else 0

    # ---- social / community ------------------------------------------------
    socials = _socials(link)
    website = (link.get("website") or "").strip()
    description = (link.get("description") or "").strip()
    twitter_dup = _f(stat.get("twitter_dup")) or _f(info.get("twitter_dup"))
    website_dup = _f(info.get("website_dup"))
    visiting = _f(info.get("visiting_count"))
    holder_count = _f(stat.get("holder_count")) or _f(info.get("holder_count"))

    kols = _f(tags.get("renowned_wallets"))
    smart_wallets = _f(tags.get("smart_wallets"))
    sniper_wallets = _f(tags.get("sniper_wallets"))
    rug_ratio = _f(info.get("rug_ratio")) or _f(stat.get("rug_ratio"))
    is_honeypot = 0.0

    # -------------------------------------------------------------- checks
    checks: list[dict] = []

    checks.append(_check(
        "top10",
        "Top 10 holders < 30%",
        top10_pct < float(settings["max_top10_percent"]),
        f"{top10_pct:.1f}% détenus par le top 10",
        round(top10_pct, 2),
        settings["max_top10_percent"],
    ))

    checks.append(_check(
        "mc_vs_volume",
        "MarketCap < Volume",
        (market_cap < volume_24h) if settings["require_mc_gt_volume"] else True,
        f"MC ${market_cap:,.0f} vs Vol ${volume_24h:,.0f}",
        round(market_cap, 2),
        round(volume_24h, 2),
    ))

    checks.append(_check(
        "buyers",
        "Acheteurs > Vendeurs",
        (buys > sells) if settings["require_more_buyers"] else True,
        f"{int(buys)} acheteurs / {int(sells)} vendeurs ({window})",
        int(buys),
        int(sells),
    ))

    links_ok = True
    if settings["require_website"] and not website:
        links_ok = False
    if settings["require_social"] and not socials:
        links_ok = False
    if settings["require_description"] and not description:
        links_ok = False
    checks.append(_check(
        "links",
        "Description / Site / Réseaux sociaux",
        links_ok,
        f"site={'oui' if website else 'non'} · réseaux={len(socials)} · "
        f"desc={'oui' if description else 'non'}",
        {"website": bool(website), "socials": len(socials), "description": bool(description)},
    ))

    dup = max(twitter_dup, website_dup)
    engagement = True
    if settings["require_social_engagement"]:
        engagement = bool(socials) and dup <= float(settings["max_social_dup"])
    checks.append(_check(
        "social_engagement",
        "Engagement social réel",
        engagement,
        f"duplication={int(dup)} | visiteurs={int(visiting)} | holders={int(holder_count)}",
        int(dup),
        settings["max_social_dup"],
    ))

    checks.append(_check(
        "kol",
        "KOLs présents",
        (kols > 0) if settings["require_kol"] else True,
        f"{int(kols)} KOL(s) détenteur(s)",
        int(kols),
        1,
    ))

    checks.append(_check(
        "smart",
        "Smart wallets présents",
        (smart_wallets > 0) if settings["require_smart_wallet"] else True,
        f"{int(smart_wallets)} smart wallet(s)",
        int(smart_wallets),
        1,
    ))

    checks.append(_check(
        "market_cap",
        f"MarketCap ATH ≤ ${float(settings['max_market_cap']):,.0f}",
        ath_market_cap <= float(settings["max_market_cap"]),
        f"ATH ${ath_market_cap:,.0f} (actuel ${market_cap:,.0f})",
        round(ath_market_cap, 2),
        settings["max_market_cap"],
    ))

    checks.append(_check(
        "age",
        f"Âge ≤ {settings['max_age_minutes']} min",
        age_minutes <= float(settings["max_age_minutes"]),
        f"âge {age_minutes:.1f} min",
        round(age_minutes, 2),
        settings["max_age_minutes"],
    ))

    # Fees are a fixed SOL cost, not a share: on a small trade they can exceed
    # anything the trade can make back. Measured on this bot's own round trips
    # (on-chain, per transaction): 2,35 % to 15,58 % of a 0,001 SOL trade, median
    # 10,88 %, because the configured priority fee alone is 10 % of that amount.
    checks.append(fees.fee_check(settings))

    failed = [c for c in checks if not c["passed"]]
    decision = "rejected" if failed else "sniped"

    # Honeypot is the last gate and the only reason to pay a second GMGN
    # request: on a token we would otherwise buy we never skip that check.
    if decision == "sniped" and not security:
        security = await gmgn.token_security(address, chain=CHAIN)
    is_honeypot = _f(security.get("is_honeypot"))
    if is_honeypot:
        checks.append(_check("honeypot", "Non-honeypot", False, "détecté comme honeypot", 1, 0))
        failed = [c for c in checks if not c["passed"]]
        decision = "rejected"

    return {
        "address": address,
        "chain": CHAIN,
        "name": info.get("name") or info.get("symbol") or "?",
        "symbol": info.get("symbol") or "?",
        "logo": info.get("logo") or "",
        "source": source,
        "channel": channel,
        "message": message[:400],
        "ts": created * 1000,
        "tx_ts": created,
        "decision": decision,
        "reasons": [c["detail"] for c in failed],
        "failed_ids": [c["id"] for c in failed],
        "checks": checks,
        "created_at": created_ts,
        # flattened metrics for the UI
        "market_cap": round(market_cap, 2),
        "ath_market_cap": round(ath_market_cap, 2),
        "ath_price": ath_price,
        "volume_24h": round(volume_24h, 2),
        "liquidity": round(liquidity, 2),
        "top10_pct": round(top10_pct, 2),
        "buyers": int(buys),
        "sellers": int(sells),
        "age_minutes": round(age_minutes, 2),
        "holders": int(holder_count),
        "kols": int(kols),
        "smart_wallets": int(smart_wallets),
        "sniper_wallets": int(sniper_wallets),
        "rug_ratio": round(rug_ratio, 4),
        "visiting_count": int(visiting),
        "price_usd": price_usd,
        "price_at_call": price_usd,
        "website": website,
        "socials": socials,
        "description": description,
        "twitter": (link.get("twitter_username") or ""),
        "telegram": (link.get("telegram") or ""),
        "has_website": bool(website),
        "has_social": bool(socials),
        "creator": dev.get("creator_address") or info.get("creator") or "",
        "launchpad": info.get("launchpad_platform") or info.get("launchpad") or "",
        "is_honeypot": bool(is_honeypot),
        "price_window": window,
    }
