"""Shared HTTPS plumbing: one trust store, one keep-alive connection pool.

**TLS.** Avast (and other TLS-scanning antivirus) re-signs HTTPS traffic with its
own root CA. Python's bundled trust store does not always validate that chain,
while Windows does — which is why ``curl`` reaches a host that Python refuses
with "certificate has expired". ``truststore`` makes Python use the OS trust
store, so **TLS verification stays ON** and behaves exactly like the browser. It
degrades silently to the default context when the package is unavailable.

**Connections.** Every caller shares one pooled client. This is not a detail: a
fresh HTTPS connection pays a full TLS handshake. Measured on this machine, same
node, same moment::

    connexion neuve      ~610 ms   (et ~2 800 ms au tout premier appel :
                                    DNS + magasin de certificats à froid)
    connexion réutilisée ~300 ms   (20 appels mesurés, aucun pic au-dessus d'1 s)

The bot makes several HTTP calls per trade (RPC reads, simulation, broadcast,
Jupiter quote + build), so for a long time the handshake was the single largest
avoidable cost in the entry path — and a sniper's entry is a race.
"""
from __future__ import annotations

import json
import ssl
import threading
import urllib.request
from typing import Any

import httpx

USING_OS_STORE = False

try:  # optional dependency
    import truststore

    _CTX: ssl.SSLContext = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    USING_OS_STORE = True
except Exception:  # noqa: BLE001
    _CTX = ssl.create_default_context()


def ssl_context() -> ssl.SSLContext:
    """TLS context backed by the OS trust store (verification always enabled)."""
    return _CTX


def open_url(request: urllib.request.Request, timeout: float):
    """``urlopen`` with the OS-backed trust store (one-shot connection)."""
    return urllib.request.urlopen(request, timeout=timeout, context=_CTX)


class HttpError(RuntimeError):
    """An HTTP error status, carrying the code so a caller can classify it."""

    def __init__(self, status: int, detail: str = ""):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail[:200]}")


class EmptyResponse(RuntimeError):
    """A 200 status with no usable body.

    This is what a rate-limited "free" endpoint answers when it has quietly
    stopped serving: ``HTTP 200`` and an empty string. Without naming it, every
    caller reports a generic parse error and nobody notices the node is dead.
    """


_lock = threading.Lock()
_client: httpx.Client | None = None

# A pool, not a single socket: RPC reads and the price feed can overlap, and a
# serialised queue behind one connection would add latency instead of saving it.
_LIMITS = httpx.Limits(max_connections=16, max_keepalive_connections=8,
                       keepalive_expiry=90.0)


def client() -> httpx.Client:
    """The shared keep-alive client (lazily created once, thread-safe)."""
    global _client
    with _lock:
        if _client is None:
            _client = httpx.Client(
                verify=_CTX,
                headers={"User-Agent": "SolSnipBot/1.0"},
                timeout=httpx.Timeout(30.0, connect=10.0),
                limits=_LIMITS,
                follow_redirects=False,
            )
        return _client


def close_client() -> None:
    """Close the pool (used at shutdown, and by tests)."""
    global _client
    with _lock:
        current, _client = _client, None
    if current is not None:
        try:
            current.close()
        except Exception:  # noqa: BLE001
            pass


def post_json(url: str, payload: Any, timeout: float | None = None) -> Any:
    """POST a JSON body over a pooled connection and decode the answer.

    Raises ``HttpError`` on a 4xx/5xx status, ``EmptyResponse`` on a 200 with an
    unreadable body, and lets transport failures (``httpx.TransportError``)
    through so the caller can decide whether they are worth retrying.
    """
    response = client().post(
        url,
        json=payload,
        timeout=timeout if timeout else 30.0,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if response.status_code >= 400:
        raise HttpError(response.status_code, response.text)
    body = response.text
    if not body or not body.strip():
        raise EmptyResponse(f"{response.status_code} sans corps")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise EmptyResponse(f"corps non-JSON: {body[:120]!r}") from exc
