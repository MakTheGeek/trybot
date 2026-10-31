"""Jupiter swap API client.

The public ``lite-api.jup.ag`` endpoint needs no API key and no paid plan, so it
replaces GMGN's ``/v1/trade/swap`` (blocked by PLAN_UPGRADE_REQUIRED on Free).

Flow: ``quote`` -> ``swap_transaction`` (Jupiter builds an unsigned transaction)
-> :mod:`backend.solana_rpc` signs it locally and broadcasts it.
"""
from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import Any

from . import net

JUPITER_API = "https://lite-api.jup.ag/swap/v1"

# Jupiter's free endpoint rate-limits (HTTP 429) and occasionally answers 5xx. A
# call lost that way is a lost entry — and in paper mode a lost *virtual* fill,
# which would quietly bias the very test the paper book exists to run. Those
# failures are transient: the same call a moment later usually succeeds. A token
# that is simply not tradable, or a malformed request, is permanent and is raised
# at once rather than retried.
TRANSIENT_ATTEMPTS = 3
TRANSIENT_BACKOFF_S = 0.5

# Diagnostics: how many calls needed a second attempt to get through.
stats: dict[str, int] = {"transient_retries": 0, "transient_failures": 0}


class JupiterError(RuntimeError):
    pass


class TransientJupiterError(JupiterError):
    """A failure a later attempt can plausibly fix (429, 5xx, timeout, reset)."""


def _is_transient_status(code: int) -> bool:
    return code == 429 or 500 <= code < 600


def _request_sync(url: str, payload: dict | None, timeout: float) -> Any:
    """One Jupiter call over the shared keep-alive connection.

    The classification below is the part that matters: 429/5xx and transport
    failures are transient (the same call a moment later usually succeeds),
    while anything else is the API's final word and is raised at once. Using the
    pooled client only changes *how* the request travels — a reused connection
    instead of a fresh TLS handshake on every quote.
    """
    headers = {"Accept": "application/json"}
    try:
        if payload is None:
            response = net.client().get(url, timeout=timeout, headers=headers)
            if response.status_code >= 400:
                raise net.HttpError(response.status_code, response.text)
            body = response.text
            if not body.strip():
                raise net.EmptyResponse(f"{response.status_code} sans corps")
            return json.loads(body)
        return net.post_json(url, payload, timeout=timeout)
    except net.HttpError as exc:
        message = f"Jupiter HTTP {exc.status}: {exc.detail[:300]}"
        if _is_transient_status(exc.status):
            raise TransientJupiterError(message) from exc
        raise JupiterError(message) from exc
    except net.EmptyResponse as exc:
        # A 200 with nothing to read: transient by nature, retry it.
        raise TransientJupiterError(f"Jupiter: réponse vide ({exc})") from exc
    except Exception as exc:  # noqa: BLE001
        # Connection reset, timeout, truncated body: worth one more try.
        raise TransientJupiterError(f"Jupiter: {exc}") from exc


async def _with_transient_retry(factory: Any) -> Any:
    """Run ``factory`` up to ``TRANSIENT_ATTEMPTS`` times on a transient failure."""
    last: Exception | None = None
    for attempt in range(1, TRANSIENT_ATTEMPTS + 1):
        try:
            return await factory()
        except TransientJupiterError as exc:
            last = exc
            if attempt == TRANSIENT_ATTEMPTS:
                break
            stats["transient_retries"] += 1
            await asyncio.sleep(TRANSIENT_BACKOFF_S * (2 ** (attempt - 1)))
    stats["transient_failures"] += 1
    assert last is not None
    raise last


async def quote(
    *, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 300,
    timeout: float = 20.0,
) -> dict:
    """Price a swap. ``amount`` is in the input token's smallest unit."""
    query = urllib.parse.urlencode({
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": int(amount),
        "slippageBps": int(slippage_bps),
        "swapMode": "ExactIn",
        "restrictIntermediateTokens": "true",
    })
    data = await _with_transient_retry(
        lambda: asyncio.to_thread(_request_sync, f"{JUPITER_API}/quote?{query}", None, timeout)
    )
    if not isinstance(data, dict) or not data.get("outAmount"):
        raise JupiterError(f"Devis Jupiter sans route: {str(data)[:200]}")
    return data


async def swap_transaction(
    *, quote_response: dict, user_public_key: str, max_priority_lamports: int,
    timeout: float = 25.0,
) -> dict:
    """Ask Jupiter to build the unsigned swap transaction."""
    payload = {
        "quoteResponse": quote_response,
        "userPublicKey": user_public_key,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "maxLamports": int(max(1_000, max_priority_lamports)),
                "priorityLevel": "high",
            }
        },
    }
    data = await _with_transient_retry(
        lambda: asyncio.to_thread(_request_sync, f"{JUPITER_API}/swap", payload, timeout)
    )
    if not isinstance(data, dict) or not data.get("swapTransaction"):
        raise JupiterError(f"Jupiter n'a pas renvoyé de transaction: {str(data)[:200]}")
    return data
