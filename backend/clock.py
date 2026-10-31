"""Wall-clock skew detection.

GMGN's OpenAPI validates the signed request timestamp within +/-5 s of the
server clock. Some machines (VMs, dual-boot PCs, offline boxes) drift far
outside that window, which makes every gmgn-cli call fail with
``AUTH_TIMESTAMP_EXPIRED``.

We therefore measure the offset against an authoritative HTTP ``Date`` header
once at start-up and expose:

* ``offset_ms``  - server_time - local_time, in milliseconds
* ``now_ts()``   - a corrected Unix timestamp (seconds)
* ``now()``      - a corrected ``datetime`` (UTC)
* ``env()``      - an environment dict that tells gmgn-cli how to correct itself
"""
from __future__ import annotations

import datetime as _dt
import time

import httpx

# Hosts that reliably answer a HEAD request with a plain ``Date`` header.
_TIME_SOURCES = [
    "https://www.google.com/generate_204",
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://www.microsoft.com",
]


class Clock:
    def __init__(self) -> None:
        self.offset_ms: float = 0.0
        self.measured: bool = False
        self.source: str = ""
        self.error: str = ""

    # ------------------------------------------------------------------ sync
    async def sync(self) -> float:
        """Measure the local clock offset against an external time source."""
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            for url in _TIME_SOURCES:
                try:
                    before = time.time()
                    resp = await client.head(url)
                    after = time.time()
                    date_header = resp.headers.get("date")
                    if not date_header:
                        # Fall back to a GET for hosts that omit Date on HEAD.
                        resp = await client.get(url)
                        after = time.time()
                        date_header = resp.headers.get("date")
                    if not date_header:
                        continue
                    server_dt = _dt.datetime.strptime(
                        date_header, "%a, %d %b %Y %H:%M:%S %Z"
                    ).replace(tzinfo=_dt.timezone.utc)
                    midpoint = (before + after) / 2.0
                    self.offset_ms = (server_dt.timestamp() - midpoint) * 1000.0
                    self.measured = True
                    self.source = url
                    self.error = ""
                    return self.offset_ms
                except Exception as exc:  # noqa: BLE001 - try the next source
                    self.error = f"{type(exc).__name__}: {exc}"
                    continue
        self.measured = False
        return 0.0

    # ---------------------------------------------------------------- access
    def now_ts(self) -> int:
        return int((time.time() * 1000 + self.offset_ms) / 1000)

    def now(self) -> _dt.datetime:
        return _dt.datetime.fromtimestamp(self.now_ts(), tz=_dt.timezone.utc)

    @property
    def drift_seconds(self) -> float:
        return self.offset_ms / 1000.0

    def is_healthy(self, tolerance_s: float = 4.0) -> bool:
        return self.measured and abs(self.drift_seconds) <= tolerance_s

    def env(self) -> dict[str, str]:
        """Environment overrides understood by the patched gmgn-cli signer."""
        return {"GMGN_TIME_OFFSET_MS": str(int(round(self.offset_ms)))}


clock = Clock()
