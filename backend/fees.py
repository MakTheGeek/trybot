"""What a round trip really costs, measured on this bot's own trades.

Measured on the 9 real round trips in ``data/positions.json`` (on-chain fees read
per transaction with ``getTransaction``):

    buy          9 648 – 105 000 lamports
    sell        13 828 –  49 017 lamports  (sell + close account)
    total       2,35 % – 15,58 % of a 0,001 SOL trade, median 10,88 %

The base fee is 5 000 lamports per signature. The priority fee is what the
``priority_fee`` setting caps; when the cap is 0,0001 SOL the buy regularly paid
the full 100 000 lamports, i.e. **10 % of a 0,001 SOL trade on the buy alone**.

Fees are a *fixed* SOL amount: their share of the trade grows as the trade
shrinks. That is the whole point of this module — state the cost so the bot can
refuse a trade that cannot pay for itself, which is what the measured history
shows it kept doing.
"""
from __future__ import annotations

from typing import Any

LAMPORTS = 1_000_000_000
BASE_FEE_LAMPORTS = 5_000  # one signature


def _priority_sol(settings: dict[str, Any]) -> float:
    return max(float(settings.get("priority_fee") or 0.0), 0.0)


def buy_fee_sol(settings: dict[str, Any]) -> float:
    """Fee of the buy transaction: one signature + the priority fee."""
    return (BASE_FEE_LAMPORTS / LAMPORTS) + _priority_sol(settings)


def sell_fee_sol(settings: dict[str, Any]) -> float:
    """Fee of the sell transaction: one signature + the priority fee."""
    return (BASE_FEE_LAMPORTS / LAMPORTS) + _priority_sol(settings)


def close_fee_sol(settings: dict[str, Any]) -> float:
    """Fee of closing the emptied token account (no priority fee needed)."""
    return BASE_FEE_LAMPORTS / LAMPORTS


def estimated_round_trip_sol(settings: dict[str, Any]) -> float:
    """Upper bound of the fees of a full buy -> sell -> close round trip.

    Three transactions pay the base fee (buy, sell and the close of the token
    account) and the two swaps may each pay the priority fee in full, which is
    the honest upper bound: the measured buys did reach it. Token-account rent
    is *excluded* — it is refunded when the account is closed.

    Paper mode books its costs with these very three functions, so a paper P&L
    and a real one are computed on the same scale and can be compared directly.
    """
    return buy_fee_sol(settings) + sell_fee_sol(settings) + close_fee_sol(settings)


def fee_share_percent(settings: dict[str, Any]) -> float:
    """The same cost as a percentage of the configured trade size."""
    amount = float(settings.get("trade_amount_sol") or 0.0)
    if amount <= 0:
        return 0.0
    return estimated_round_trip_sol(settings) / amount * 100.0


def fee_gate(settings: dict[str, Any]) -> dict[str, Any]:
    """Verdict of the fee rule. ``cap <= 0`` disables it explicitly."""
    cap = float(settings.get("max_fee_percent") or 0.0)
    share = fee_share_percent(settings)
    amount = float(settings.get("trade_amount_sol") or 0.0)
    return {
        "estimated_sol": round(estimated_round_trip_sol(settings), 8),
        "share_percent": round(share, 2),
        "cap_percent": cap,
        "enabled": cap > 0,
        "passed": cap <= 0 or share <= cap,
        "amount_sol": amount,
    }


def fee_check(settings: dict[str, Any]) -> dict[str, Any]:
    """The call-card check for the fee rule, same shape as the other criteria.

    It is about the trade rather than the token — but a call card that explains a
    rejection has to include it, otherwise a refused snipe would look unfounded.
    """
    gate = fee_gate(settings)
    if gate["enabled"]:
        label = f"Frais ≤ {gate['cap_percent']:g} % du trade"
        detail = (f"frais estimés {gate['estimated_sol']:.6f} SOL = "
                  f"{gate['share_percent']:.1f} % d'un trade de {gate['amount_sol']:g} SOL")
    else:
        label = "Frais (règle désactivée)"
        detail = (f"frais estimés {gate['estimated_sol']:.6f} SOL = "
                  f"{gate['share_percent']:.1f} % du trade")
    return {
        "id": "fees",
        "label": label,
        "passed": bool(gate["passed"]),
        "detail": detail,
        "value": gate["share_percent"],
        "threshold": gate["cap_percent"],
    }
