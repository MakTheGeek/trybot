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

One trap is worth knowing about: gmgn-cli loads ``~/.config/gmgn/.env`` with
``override: true``, so a stale ``GMGN_TIME_OFFSET_MS`` written there silently
wins over the value this process passes to the subprocess. A stale offset is
exactly what produces ``AUTH_TIMESTAMP_EXPIRED`` on every call, so
:meth:`Clock.sync` rewrites that line whenever it no longer matches the fresh
measurement (and only when the key is already present).
"""
from __future__ import annotations

import datetime as _dt
import os
import pathlib
import re
import time

import httpx

GMGN_ENV_PATH = pathlib.Path(os.path.expanduser("~/.config/gmgn/.env"))
_OFFSET_LINE = re.compile(
    r"(?m)^([ \t]*GMGN_TIME_OFFSET_MS[ \t]*=[ \t]*)(-?\d+)[ \t]*\r?$"
)

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
        self.note: str = ""

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
                    self.mirror_into_gmgn_env()
                    return self.offset_ms
                except Exception as exc:  # noqa: BLE001 - try the next source
                    self.error = f"{type(exc).__name__}: {exc}"
                    continue
        self.measured = False
        return 0.0

    # ------------------------------------------------------ gmgn-cli offset file
    def mirror_into_gmgn_env(self, tolerance_ms: float = 2000.0) -> str:
        """Keep the offset stored in gmgn-cli's own ``.env`` in step.

        The CLI reads that file with ``override: true``: a stale value there
        beats the environment this process hands to the subprocess, and the
        signed timestamp then misses GMGN's +/-5 s window even though the bot
        measured the skew correctly. Only an existing line is rewritten — the
        file is the user's, so nothing is added to it.
        """
        self.note = ""
        if not self.measured:
            return ""
        try:
            # Bytes, not text mode: the file must come back byte-for-byte
            # identical apart from the number, line endings included.
            raw = GMGN_ENV_PATH.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        match = _OFFSET_LINE.search(text)
        if not match:
            return ""
        stored = int(match.group(2))
        wanted = int(round(self.offset_ms))
        if abs(stored - wanted) <= tolerance_ms:
            return ""
        fixed = _OFFSET_LINE.sub(
            lambda m: f"{m.group(1)}{wanted}", text, count=1
        )
        try:
            tmp = GMGN_ENV_PATH.with_suffix(".tmp")
            tmp.write_bytes(fixed.encode("utf-8"))
            tmp.replace(GMGN_ENV_PATH)
        except OSError as exc:
            self.note = (
                f"Décalage d'horloge stocké dans {GMGN_ENV_PATH} ({stored:+d} ms) "
                f"périmé et non corrigeable ({exc}) — les appels GMGN échoueront "
                f"tant que la ligne GMGN_TIME_OFFSET_MS y reste."
            )
            return self.note
        self.note = (
            f"Décalage d'horloge stocké dans {GMGN_ENV_PATH} remis à jour "
            f"({stored:+d} → {wanted:+d} ms) : une valeur périmée y prime sur "
            f"celle du bot et fait refuser les appels GMGN."
        )
        return self.note

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
