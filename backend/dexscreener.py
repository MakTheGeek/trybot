"""Real-time price / market data from Dexscreener.

Dexscreener is used for continuous price tracking and PnL. When a token has no
Dexscreener pair yet (still on a launchpad bonding curve) the caller falls back
to GMGN prices.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

BASE = "https://api.dexscreener.com"


class DexScreener:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=12.0,
            headers={"User-Agent": "SolSnipBot/1.0", "Accept": "application/json"},
        )
        self._cache: dict[str, tuple[float, dict]] = {}
        self.last_error: str = ""
        # Number of transport failures (a request that never got an answer).
        # An answer that simply contains no pair for a token is not a failure.
        self.failures: int = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def token(self, address: str, *, ttl: float = 0.0) -> dict:
        """Return the best (deepest-liquidity) Solana pair for a token."""
        now = time.time()
        cached = self._cache.get(address)
        if cached and ttl and now - cached[0] < ttl:
            return cached[1]

        try:
            resp = await self._client.get(f"{BASE}/latest/dex/tokens/{address}")
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return cached[1] if cached else {}

        pairs = [p for p in (payload.get("pairs") or []) if p.get("chainId") == "solana"]
        if not pairs:
            # Keep stale data rather than reporting nothing.
            return cached[1] if cached else {}

        def liquidity(p: dict) -> float:
            try:
                return float((p.get("liquidity") or {}).get("usd") or 0)
            except (TypeError, ValueError):
                return 0.0

        best = max(pairs, key=liquidity)
        parsed = self._parse(best, address)
        self._cache[address] = (now, parsed)
        return parsed

    @staticmethod
    def _parse(pair: dict, address: str) -> dict:
        def num(value: Any) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        txns = pair.get("txns") or {}
        volume = pair.get("volume") or {}
        change = pair.get("priceChange") or {}
        liquidity = pair.get("liquidity") or {}
        created_ms = pair.get("pairCreatedAt")
        base = pair.get("baseToken") or {}

        # Aggregate buy/sell counts across the available windows.
        buys = {k: num((txns.get(k) or {}).get("buys")) for k in txns}
        sells = {k: num((txns.get(k) or {}).get("sells")) for k in txns}

        return {
            "address": address,
            "symbol": str(base.get("symbol") or ""),
            "name": str(base.get("name") or ""),
            "price_usd": num(pair.get("priceUsd")),
            "price_native": num(pair.get("priceNative")),
            "market_cap": num(pair.get("marketCap")) or num(pair.get("fdv")),
            "fdv": num(pair.get("fdv")),
            "liquidity_usd": num(liquidity.get("usd")),
            "volume_24h": num(volume.get("h24")),
            "volume_1h": num(volume.get("h1")),
            "volume_5m": num(volume.get("m5")),
            "change_m5": num(change.get("m5")),
            "change_h1": num(change.get("h1")),
            "change_h24": num(change.get("h24")),
            "buys": buys,
            "sells": sells,
            "dex_id": pair.get("dexId"),
            "pair_address": pair.get("pairAddress"),
            "created_at_ms": created_ms,
            "url": pair.get("url"),
            "updated_ms": int(time.time() * 1000),
        }

    async def prices_multi(self, addresses: list[str]) -> dict[str, dict]:
        """Batch price lookup for up to 30 tokens (Dexscreener limit)."""
        if not addresses:
            return {}
        result: dict[str, dict] = {}
        for i in range(0, len(addresses), 30):
            batch = addresses[i:i + 30]
            try:
                resp = await self._client.get(f"{BASE}/latest/dex/tokens/{','.join(batch)}")
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.failures += 1
                continue
            groups: dict[str, list[dict]] = {a: [] for a in batch}
            for pair in payload.get("pairs") or []:
                if pair.get("chainId") != "solana":
                    continue
                base = (pair.get("baseToken") or {}).get("address")
                if base in groups:
                    groups[base].append(pair)
            for addr, pairs in groups.items():
                if not pairs:
                    continue
                best = max(
                    pairs,
                    key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
                )
                result[addr] = self._parse(best, addr)
        return result


dexscreener = DexScreener()
