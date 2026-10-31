"""Trade execution and open-position management (TP / SL / trailing stop).

Two execution backends, selected by the ``execution_backend`` setting:

* ``jupiter`` — Jupiter quotes and builds the swap, :mod:`backend.solana_rpc`
  signs it locally with the wallet key and broadcasts it to an RPC node.
  No GMGN plan is involved.
* ``gmgn`` — ``gmgn-cli swap`` (identical routing to the GMGN terminal), which
  requires a **paid** GMGN plan for the trading routes.

Exits (take-profit, stop-loss, trailing stop) are supervised here in real time so
the trailing behaviour stays fully configurable.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from . import config
from . import fees as fee_model
from . import jupiter
from . import solana_rpc as solana
from .dexscreener import dexscreener
from .gmgn import SOL_MINT, USDC_MINT, GMGNPlanRequired, PRIORITY_LOW, gmgn
from .pricefeed import price_feed
from .state import store

LAMPORTS = 1_000_000_000

# How many fresh quotes a swap may try when the simulation refuses the route
# Jupiter proposed (see `_jupiter_swap`).
ROUTE_ATTEMPTS = 3


async def get_sol_price() -> float:
    """SOL/USD spot price from Dexscreener (USDC pair preferred)."""
    data = await dexscreener.token(SOL_MINT)
    price = float(data.get("price_usd") or 0.0)
    if price:
        store.runtime["sol_price"] = price
    return price


async def resolve_wallet() -> tuple[str, float]:
    """Return the Solana wallet we trade with, and its SOL balance.

    With the ``jupiter`` backend the local signing key is authoritative — the
    address comes from the key and the balance straight from the chain, so the
    money path never depends on a GMGN read. The ``gmgn`` backend keeps using
    the wallet bound to the API key.
    """
    if str(config.get("execution_backend") or "gmgn") == "jupiter":
        try:
            address = solana.wallet_address()
            balance = await solana.sol_balance(address)
            store.runtime["wallet"] = address
            store.runtime["wallet_balance_sol"] = balance
            return address, balance
        except Exception as exc:  # noqa: BLE001
            store.log(f"Wallet local indisponible: {exc}", level="warn", action="wallet")
    try:
        info = await gmgn.portfolio_info()
    except Exception as exc:  # noqa: BLE001
        store.log(f"Impossible de récupérer le wallet GMGN: {exc}", level="warn", action="wallet")
        return "", 0.0
    for wallet in info.get("wallets") or []:
        if wallet.get("chain") == "sol":
            address = wallet.get("address") or ""
            balance = 0.0
            for bal in wallet.get("balances") or []:
                if bal.get("symbol") == "SOL":
                    try:
                        balance = float(bal.get("balance") or 0.0)
                    except (TypeError, ValueError):
                        balance = 0.0
            store.runtime["wallet"] = address
            store.runtime["wallet_balance_sol"] = balance
            return address, balance
    return "", 0.0


async def token_balance(wallet: str, token: str) -> float:
    try:
        data = await gmgn.token_balance(wallet, token)
        for key in ("balance", "amount", "amount_cur", "token_balance"):
            if data.get(key) not in (None, ""):
                return float(data[key])
        # Some responses nest the balance.
        for value in data.values():
            if isinstance(value, dict) and value.get("balance"):
                return float(value["balance"])
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def sell_amount_units(raw: int, percent: float) -> int:
    """Units to sell for ``percent`` of ``raw``.

    Returns 0 when the request rounds below one unit (so the caller can refuse
    instead of silently dumping the whole bag). A full 100% always returns the
    exact balance.
    """
    if raw <= 0:
        return 0
    if percent >= 100:
        return raw
    return min(int(raw * (percent / 100.0)), raw)


# An exit decided on the display price is re-checked against the price a sale
# would really get when the two differ by more than this fraction.
_EXIT_PRICE_TOLERANCE = 0.25


# A drained pool has no sell route at all: Jupiter answers "No routes found" for
# every quote. A paper position then cannot be sold, while its exit rule keeps
# firing on every tick — measured on SOOTS: 34 identical failed sales in a few
# minutes. After this many consecutive *no-route* failures the paper position is
# closed at the last Dexscreener price and the record says the price was not
# executable. Only paper positions: a real bag is never written off at a price
# nobody paid — it keeps failing loudly until a market exists again.
UNPRICED_GIVE_UP = 4
_UNPRICED_NOTE = "route de vente inexistante — prix non exécutable"
_UNPRICED_PARTIAL_NOTE = (
    "Aucune route de vente : ce pool ne paie plus rien, donc une vente partielle "
    "n'a pas de prix. Fermez la totalité pour solder la position au dernier prix "
    "connu (étiqueté non exécutable)."
)
_TRANSIENT_NOTE = (
    "Jupiter n'a pas répondu (limite de débit ou réseau) : la position est restée "
    "ouverte, réessayez dans un instant."
)


def is_no_route_error(exc: BaseException) -> bool:
    """True when Jupiter says there is no route at all (not a transient failure).

    A 429, a timeout or a node error must never count towards the give-up: only
    a pool that pays nothing is a reason to stop asking. Matched on our own
    wording and Jupiter's, lower-cased, so the check survives message changes.
    """
    text = str(exc).lower()
    return ("no route" in text or "aucune route" in text
            or "route de sortie" in text or "devis de vente vide" in text)


# What a *temporary* refusal looks like. Kept next to the no-route test because
# the two must never be confused: one means "the market has nothing to pay", the
# other means "ask again in a moment". Text-matched as well as class-matched so a
# raised or wrapped error (a caller's own RuntimeError, say) is still recognised.
_TRANSIENT_HINTS = ("http 429", "too many requests", "http 500", "http 502",
                    "http 503", "http 504", "timeout", "timed out", "timedout",
                    "connection reset", "connection aborted", "connection refused",
                    "connection closed", "temporarily", "réponse vide",
                    "max retries", "service unavailable", "gateway")


def is_transient_error(exc: BaseException) -> bool:
    if isinstance(exc, jupiter.TransientJupiterError):
        return True
    text = str(exc).lower()
    return any(hint in text for hint in _TRANSIENT_HINTS)


# A refused simulation has two very different meanings: the *route* cannot
# execute (a real refusal — the swap would burn its fees for nothing) or the
# *wallet* cannot pay for it (nothing to do with the route; the price it quotes
# is still the price the market offers). Matched on explicit wording only, so a
# program's own error code is never mistaken for a balance problem.
_FUNDING_MARKERS = (
    "insufficient lamports",
    "insufficient funds",
    "insufficient balance",
    "resultwithnegativelamports",
)


def simulation_refusal_is_funding(sim: dict) -> bool:
    """True when a simulation failed only because the wallet cannot pay."""
    if not sim:
        return False
    blob = json.dumps(sim.get("err") or {})
    for line in sim.get("logs") or []:
        blob += " " + str(line)
    blob = blob.lower()
    return any(marker in blob for marker in _FUNDING_MARKERS)


def exit_decision(*, price: float, peak: float, pnl_pct: float, tp_percent: float,
                  sl_percent: float, trailing_percent: float,
                  trailing_active: bool) -> str:
    """Which exit rule fires, in priority order (``""`` = keep holding).

    A threshold of 0 disables its rule instead of firing immediately, so a
    position opened without a stored threshold can never close on the next tick.
    """
    if tp_percent > 0 and pnl_pct >= tp_percent:
        return "take_profit"
    if sl_percent > 0 and pnl_pct <= -sl_percent:
        return "stop_loss"
    if (trailing_active and trailing_percent > 0
            and peak > 0 and price <= peak * (1 - trailing_percent / 100.0)):
        return "trailing_stop"
    return ""


def realized_pnl(*, proceeds_sol: float, sell_fees: float, committed_sol: float,
                 buy_fee: float, close_fee: float, sol_price: float,
                 cost_usd: float, tokens: float = 0.0) -> dict:
    """Realised P&L of a closed position, **every fee included**.

    ``proceeds_sol`` is the wallet's net change for the sell transaction(s) — the
    sell fees are already deducted — so they are added back to get the gross
    proceeds, and the whole fee drag is then subtracted explicitly::

        net = (proceeds + sell_fees) - committed - (buy_fee + sell_fees + close_fee)
            =  proceeds - committed - buy_fee - close_fee

    Every lamport the trade cost is therefore counted: base **and** priority fees
    of the buy, of the sell and of the closing transaction. The token-account rent
    is deliberately excluded — it is refunded, not spent — and reported on its own
    (``rent_recovered``). ``cost_usd`` is the capital committed to the swap, so the
    percentage is the return on what was really traded.
    """
    fees = max(0.0, buy_fee) + max(0.0, sell_fees) + max(0.0, close_fee)
    gross_sol = proceeds_sol + max(0.0, sell_fees)
    net_sol = gross_sol - max(0.0, committed_sol) - fees
    pnl_usd = net_sol * sol_price
    return {
        "fees_sol": fees,
        "gross_sol": gross_sol,
        "net_sol": net_sol,
        "pnl_usd": pnl_usd,
        "pnl_percent": (pnl_usd / cost_usd * 100.0) if cost_usd else 0.0,
        # Price the tokens really went for, fees kept out so it is comparable
        # with the entry price (which is also a rate, before the buy fee).
        "exit_price": (gross_sol * sol_price / tokens) if tokens else 0.0,
    }


class Trader:
    def __init__(self) -> None:
        self.wallet: str = ""
        self.sol_balance: float = 0.0
        self._task: asyncio.Task | None = None
        self._running = False
        self._bg: set[asyncio.Task] = set()
        # Positions being closed: their token account is already empty, so the
        # manager must not try to sell them again while the final PnL is built.
        self._closing: set[str] = set()
        # Consecutive « aucune route de vente » failures, per position id. Only
        # this specific failure counts: a 429 or a timeout is transient and must
        # never push a paper position towards a made-up liquidation.
        self._no_route: dict[str, int] = {}

    def _spawn(self, coro) -> None:
        """Run a coroutine in the background, keeping a strong reference."""
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # -------------------------------------------------------------- wallet
    async def refresh_wallet(self) -> None:
        address, balance = await resolve_wallet()
        if address:
            self.wallet = address
            self.sol_balance = balance
            store.runtime["wallet_balance_sol"] = balance
        store.broadcast({"type": "status"})

    # --------------------------------------------------- trading-plan probe
    async def probe_trading_plan(self) -> str:
        """Check whether the GMGN plan allows the trading routes.

        ``order quote`` is fund-free and sits on the same plan-gated trading
        family as ``swap``: GMGN answers 403 PLAN_UPGRADE_REQUIRED before
        validating anything when the endpoint's weight exceeds the plan's
        rate-limit capacity (observed: weight 11 vs capacity 5 on Free). A plan
        hit here therefore proves swaps can never execute either, so we can warn
        the user at start-up instead of wasting a swap on the next real snipe.

        Any other error is inconclusive and never blocks trading: only a
        PLAN_UPGRADE_REQUIRED answer does.
        """
        if not self.wallet:
            await self.refresh_wallet()
        if not self.wallet:
            return "unknown"
        if str(config.get("execution_backend") or "gmgn") != "gmgn":
            # Jupiter signs locally: the GMGN plan is irrelevant, clear any
            # stale block so the UI does not warn about an unused route.
            store.runtime["trading_blocked"] = False
            store.runtime.pop("trading_blocked_reason", None)
            store.runtime.pop("trading_blocked_url", None)
            return "skip"
        try:
            await gmgn.quote(
                wallet=self.wallet, input_token=SOL_MINT, output_token=USDC_MINT,
                amount=str(int(0.001 * LAMPORTS)), slippage=30,
                priority=PRIORITY_LOW,
            )
        except GMGNPlanRequired as exc:
            store.runtime["trading_blocked"] = True
            store.runtime["trading_blocked_reason"] = str(exc)
            store.runtime["trading_blocked_url"] = getattr(exc, "upgrade_url", "")
            store.log(
                "⛔ Le plan GMGN actuel ne permet pas le trading via l'API "
                "(403 PLAN_UPGRADE_REQUIRED) — aucun ordre ne sera envoyé tant "
                "que le plan n'est pas mis à niveau (Plus ou Pro).",
                level="warn", action="gmgn",
            )
            store.broadcast({"type": "status"})
            return "blocked"
        except Exception:  # noqa: BLE001
            return "unknown"
        # The trading route answered: the plan can trade.
        store.runtime["trading_blocked"] = False
        store.runtime.pop("trading_blocked_reason", None)
        store.runtime.pop("trading_blocked_url", None)
        store.broadcast({"type": "status"})
        return "ok"

    # ------------------------------------------------------------------ buy
    async def buy(self, report: dict) -> dict:
        """Execute a snipe for a token that passed every filter."""
        settings = config.all()
        address = report["address"]
        amount_sol = float(settings["trade_amount_sol"])
        result = {
            "executed": False,
            "reason": "",
            "position_id": "",
            "amount_sol": amount_sol,
        }

        if bool(settings.get("paper_trading")):
            # Paper mode: no wallet, no plan and no balance is involved, because
            # no transaction is ever built into an order.
            return await self._paper_buy(report)

        if not self.wallet:
            await self.refresh_wallet()
        if not self.wallet:
            result["reason"] = "Aucun wallet Solana lié à la clé API GMGN."
            return result

        backend = str(settings.get("execution_backend") or "gmgn")

        if backend == "gmgn":
            if not config.gmgn_private_key():
                result["reason"] = "GMGN_PRIVATE_KEY manquante: les ordres signés sont impossibles."
                return result
            # A 403 PLAN_UPGRADE_REQUIRED is deterministic and can never succeed,
            # so once we've seen it we stop submitting doomed swaps (which would
            # also trip GMGN's repeated-business-error limiter) until re-armed.
            if store.runtime.get("trading_blocked"):
                result["reason"] = str(store.runtime.get("trading_blocked_reason") or
                                        "Trading désactivé: plan GMGN insuffisant.")
                return result
        else:
            # Jupiter signs locally: the key must control the wallet we track,
            # otherwise the swap would move funds from a different account.
            try:
                local = solana.wallet_address()
            except Exception as exc:  # noqa: BLE001
                result["reason"] = f"Wallet local indisponible: {exc}"
                store.log(f"Achat impossible — {result['reason']}", level="error",
                          token=address, action="buy")
                return result
            if local != self.wallet:
                result["reason"] = (
                    f"Clé privée locale = {local[:6]}…{local[-4:]} mais wallet suivi = "
                    f"{self.wallet[:6]}…{self.wallet[-4:]}: envoi refusé."
                )
                store.log(f"Achat impossible — {result['reason']}", level="error",
                          token=address, action="buy")
                return result

        # A Jupiter swap also pays the network fee and any new token account's
        # rent out of the same balance, so keep a small reserve. The comparison is
        # made in whole lamports — the unit a wallet actually holds — because
        # `0.1 + 0.003` in binary floating point is 0.10300000000000001: comparing
        # those floats refused a wallet holding exactly the 103 000 000 lamports
        # the rule asks for, and told it "0.1030 disponible pour 0.1 (+0.0030)".
        reserve = 0.003 if backend == "jupiter" else 0.0
        needed_lamports = int(round((amount_sol + reserve) * LAMPORTS))
        held_lamports = int(round(float(self.sol_balance or 0.0) * LAMPORTS))
        if self.sol_balance and needed_lamports > held_lamports:
            result["reason"] = (
                f"Solde insuffisant: {self.sol_balance:.4f} SOL disponible "
                f"pour un achat de {amount_sol} SOL"
                + (f" (+{reserve:.4f} SOL réservés aux frais/loyer)." if reserve else ".")
            )
            return result

        lamports = int(round(amount_sol * LAMPORTS))
        store.log(
            f"Achat {report['symbol']} — {amount_sol} SOL ({lamports} lamports)",
            level="info", token=address, action="buy",
        )
        try:
            if backend == "jupiter":
                resp = await self._jupiter_swap(
                    input_mint=SOL_MINT, output_mint=address, amount=lamports, side="buy",
                )
            else:
                resp = await gmgn.swap(
                    wallet=self.wallet,
                    input_token=SOL_MINT,
                    output_token=address,
                    amount=str(lamports),
                    slippage=int(settings["slippage"]),
                    auto_slippage=True,
                    priority_fee=float(settings["priority_fee"]),
                    anti_mev=bool(settings["anti_mev"]),
                )
        except GMGNPlanRequired as exc:
            # Blocking, actionable condition: surface it once, loudly, and stop
            # re-submitting swaps that cannot succeed on this plan.
            store.runtime["trading_blocked"] = True
            store.runtime["trading_blocked_reason"] = str(exc)
            store.runtime["trading_blocked_url"] = getattr(exc, "upgrade_url", "")
            result["reason"] = str(exc)
            store.log(
                f"⛔ Trading impossible — plan GMGN insuffisant (403 PLAN_UPGRADE_REQUIRED). "
                f"Abonnement payant requis (Plus ou Pro).",
                level="error", token=address, action="buy",
            )
            return result
        except Exception as exc:  # noqa: BLE001
            result["reason"] = f"Échec du swap: {exc}"
            store.log(f"Échec de l'achat {report['symbol']}: {exc}", level="error",
                      token=address, action="buy")
            return result

        order_id = resp.get("order_id") or resp.get("orderId") or resp.get("id") or ""
        tx_hash = resp.get("tx_hash") or resp.get("txHash") or ""
        # Jupiter gives us the exact filled balance (read back from the chain),
        # so the position is complete immediately; GMGN needs a deferred read.
        tokens = float(resp.get("_tokens") or 0.0)
        result["executed"] = True
        result["order_id"] = order_id
        result["tx_hash"] = tx_hash
        result["raw"] = {k: v for k, v in resp.items() if k in ("code", "message", "reason", "status")}

        entry_price = float(report.get("price_usd") or 0.0)

        # If the swap response told us the fill price, prefer it.
        for key in ("price", "fill_price", "avg_price"):
            if resp.get(key):
                try:
                    entry_price = float(resp[key])
                    break
                except (TypeError, ValueError):
                    pass

        sol_price = store.runtime.get("sol_price") or await get_sol_price()
        # Real fill price: the swap told us exactly how many tokens we received
        # and what those SOL cost, so the entry is *measured*, not assumed. Every
        # exit rule (TP / SL / trailing) keys off it, and slippage can be large.
        if tokens and amount_sol and sol_price:
            entry_price = (amount_sol * sol_price) / tokens
        position = {
            "id": store.new_position_id(),
            "address": address,
            "chain": "sol",
            "symbol": report.get("symbol", "?"),
            "name": report.get("name", "?"),
            "logo": report.get("logo", ""),
            "status": "open",
            "opened_at": int(time.time() * 1000),
            "amount_sol": amount_sol,
            # Capital committed to the swap, kept whole even after a partial
            # sell so the final PnL covers every fraction that was sold.
            "sol_committed": amount_sol,
            "proceeds_sol": 0.0,
            "sell_fees_sol": 0.0,
            "sol_price_entry": sol_price,
            # ``cost_usd`` and ``tokens`` follow what is *left* after a partial
            # sell (so the live P&L describes the remaining bag), while these two
            # keep the original amount, which is the only correct denominator for
            # the final percentage and the only correct divisor for the average
            # exit price.
            "cost_usd_total": round(amount_sol * sol_price, 4),
            "tokens_bought": tokens,
            "cost_usd": round(amount_sol * sol_price, 4),
            "tokens": tokens,
            "entry_price": entry_price,
            "current_price": entry_price,
            "peak_price": entry_price,
            "trailing_active": False,
            "tp_percent": float(settings["tp_percent"]),
            "sl_percent": float(settings["sl_percent"]),
            "trailing_percent": float(settings["trailing_percent"]),
            "trailing_enabled": bool(settings["trailing_enabled"]),
            "pnl_usd": 0.0,
            "pnl_percent": 0.0,
            "order_id": order_id,
            "tx_buy": tx_hash,
            "exit_reason": "",
            "manual": False,
            "wallet": self.wallet,
        }
        store.add_position(position)
        result["position_id"] = position["id"]
        store.log(
            f"Position ouverte: {position['symbol']} à ${entry_price:.8f}",
            level="success", token=address, action="buy",
        )
        # Read the filled token balance in the background: blocking here would
        # hold the analysis worker (and the gate) for ~3 s after the decision.
        if not tokens:
            self._spawn(self._finalize_buy(position["id"], address))
        store.recompute_stats()
        store.broadcast({"type": "status"})
        return result

    async def _finalize_buy(self, position_id: str, address: str) -> None:
        """Fill in the token balance a moment after the swap has settled.

        Retried a few times: a node that is briefly unable to list token accounts
        used to leave the position with ``tokens = 0`` — an untracked bag.
        """
        last_error = ""
        for attempt in range(1, 5):
            if float((store.get_position(position_id) or {}).get("tokens") or 0) > 0:
                return
            try:
                await asyncio.sleep(3.0)
                position = store.get_position(position_id) or {}
                tokens = await token_balance(self.wallet, address)
                if not tokens:
                    last_error = "aucun token renvoyé"
                    continue
                patch: dict = {"tokens": tokens, "tokens_bought": tokens}
                # The fill price was unknown at creation time (the balance was
                # still 0). Now that the real amount is known, correct the entry
                # so TP/SL/trailing key off the true average price.
                cost = float(position.get("cost_usd") or 0)
                if cost > 0:
                    patch["entry_price"] = cost / tokens
                store.update_position(position_id, patch)
                updated = store.get_position(position_id)
                if updated:
                    store.push_position_update(updated)
                store.recompute_stats()
                store.broadcast({"type": "status"})
                return
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
        store.log(
            f"Solde de tokens inconnu après 4 essais ({last_error}) — la vente relira "
            "la chaîne avant de vendre.",
            level="warn", token=address, action="buy",
        )

    # ------------------------------------------------------------------ paper
    async def _paper_buy(self, report: dict) -> dict:
        """A snipe with everything real except the transaction.

        The route, the price and the price impact come from the live pool: the
        quote is Jupiter's, for the exact size that would have been traded, and
        the route is really **simulated** — so a swap that could not have executed
        fails here too, instead of flattering the strategy. Fees are the same
        upper bound the "fees" criterion uses, which is what makes a paper P&L
        directly comparable with a real one.
        """
        settings = config.all()
        address = report["address"]
        amount_sol = float(settings["trade_amount_sol"])
        result = {"executed": False, "reason": "", "position_id": "",
                  "amount_sol": amount_sol, "paper": True}
        symbol = report.get("symbol", "?")
        lamports = int(round(amount_sol * LAMPORTS))

        def refuse(why: str) -> dict:
            result["reason"] = f"Achat papier impossible — {why}"
            store.log(f"📝 Achat PAPIER refusé {symbol} — {why}", level="warn",
                      token=address, action="buy")
            return result

        try:
            fill = await self._jupiter_swap(
                input_mint=SOL_MINT, output_mint=address, amount=lamports,
                side="buy", dry_run=True,
            )
        except Exception as exc:  # noqa: BLE001
            return refuse(f"aucune route exécutable ({exc})")

        out_raw = int(fill.get("_out_amount") or 0)
        if out_raw <= 0:
            return refuse("devis vide")
        try:
            decimals = await solana.mint_decimals(address)
        except Exception as exc:  # noqa: BLE001
            return refuse(str(exc))
        tokens = out_raw / (10 ** decimals)
        if tokens <= 0:
            return refuse("quantité nulle")

        sol_price = float(store.runtime.get("sol_price") or 0.0) or await get_sol_price()
        # Same formula as the real path: the entry is what the committed SOL really
        # bought, so the quoted price impact is part of it — not the display price.
        entry_price = (((amount_sol * sol_price) / tokens) if sol_price
                       else float(report.get("price_usd") or 0.0))
        position = {
            "id": store.new_position_id(),
            "address": address,
            "chain": "sol",
            "symbol": symbol,
            "name": report.get("name", "?"),
            "logo": report.get("logo", ""),
            "status": "open",
            "paper": True,
            "opened_at": int(time.time() * 1000),
            "amount_sol": amount_sol,
            # Kept whole across partial sells so the final PnL covers every slice.
            "sol_committed": amount_sol,
            "proceeds_sol": 0.0,
            "sell_fees_sol": 0.0,
            "buy_fee_sol": fee_model.buy_fee_sol(settings),
            "sol_price_entry": sol_price,
            "cost_usd_total": round(amount_sol * sol_price, 4),
            "tokens_bought": tokens,
            "cost_usd": round(amount_sol * sol_price, 4),
            "tokens": tokens,
            "decimals": decimals,
            "entry_price": entry_price,
            "current_price": entry_price,
            "peak_price": entry_price,
            "trailing_active": False,
            "tp_percent": float(settings["tp_percent"]),
            "sl_percent": float(settings["sl_percent"]),
            "trailing_percent": float(settings["trailing_percent"]),
            "trailing_enabled": bool(settings["trailing_enabled"]),
            "pnl_usd": 0.0,
            "pnl_percent": 0.0,
            "order_id": "",
            "tx_buy": "",
            "exit_reason": "",
            "manual": False,
            "wallet": self.wallet,
            # Provenance of the virtual fill, so the UI can be explicit about it.
            "paper_route": fill.get("_route") or "",
            "paper_price_impact": float(fill.get("_price_impact") or 0.0),
            "paper_simulated": bool(fill.get("_simulated")),
            # The route was quotable but the simulation could not confirm it because
            # the wallet cannot pay. Kept visible so a virtual result is never read
            # as a route that provably executes.
            "paper_funding_only": bool(fill.get("_funding_only")),
        }
        store.add_position(position)
        result["executed"] = True
        result["position_id"] = position["id"]
        result["route"] = position["paper_route"]
        result["price_impact"] = position["paper_price_impact"]
        result["simulated"] = position["paper_simulated"]
        note = ("" if position["paper_simulated"]
                else " · route non validée par la simulation")
        store.log(
            f"📝 ACHAT PAPIER {symbol} — {amount_sol:g} SOL → {tokens:,.2f} tokens "
            f"à ${entry_price:.8f} · route {position['paper_route']} · impact "
            f"{position['paper_price_impact'] * 100:.2f} %{note} · aucun ordre envoyé",
            level="info", token=address, action="buy",
        )
        store.recompute_stats()
        store.broadcast({"type": "status"})
        return result

    async def _paper_sell(self, position: dict, *, percent: float, reason: str,
                          manual: bool = False) -> dict:
        """Close a paper position at the price a real sale would really get.

        The sale is quoted on the live pool for the exact amount being sold, so a
        token nobody can buy any more fails to sell here too — which is exactly
        the risk this mode exists to measure.
        """
        settings = config.all()
        address = position["address"]
        symbol = position.get("symbol", "?")
        result = {"executed": False, "reason": "", "paper": True}
        decimals = int(position.get("decimals") or 0)
        raw_total = int(round(float(position.get("tokens") or 0) * (10 ** decimals)))
        amount = sell_amount_units(raw_total, percent)
        if amount <= 0:
            result["reason"] = (f"Montant à vendre nul: {raw_total} unité(s) × "
                                f"{percent:.1f}% arrondit à 0.")
            store.log(f"Vente PAPIER de {symbol} refusée — {result['reason']}",
                      level="warn", token=address, action="sell")
            return result

        try:
            fill = await self._jupiter_swap(
                input_mint=address, output_mint=SOL_MINT, amount=amount,
                side="sell", dry_run=True, verify_route=False,
            )
        except Exception as exc:  # noqa: BLE001
            if is_no_route_error(exc):
                # The pool is drained: asking again on the next tick would give
                # the same answer for ever (measured: 34 failed sales on SOOTS).
                if await self._sell_without_route(position, reason, manual=manual,
                                                  percent=percent):
                    result["executed"] = True
                    result["liquidation"] = "unpriced"
                    result["reason"] = _UNPRICED_NOTE
                else:
                    result["reason"] = (_UNPRICED_PARTIAL_NOTE if manual
                                        else f"Aucune route de sortie: {exc}")
                return result
            if manual and is_transient_error(exc):
                # A rate limit or a network hiccup is not the market's verdict:
                # nothing is counted and nothing is written off. Say so plainly,
                # otherwise a refused click reads like a broken close button.
                result["reason"] = _TRANSIENT_NOTE
                store.log(f"⛔ Vente PAPIER de {symbol} non aboutie — {exc} "
                          "(panne passagère : la position reste ouverte, "
                          "réessayez dans un instant)", level="warn",
                          token=address, action="sell")
                return result
            result["reason"] = f"Aucune route de sortie: {exc}"
            store.log(f"⛔ Vente PAPIER impossible {symbol} — {exc} "
                      "(la position reste ouverte)", level="error",
                      token=address, action="sell")
            return result

        gross_sol = int(fill.get("_out_amount") or 0) / LAMPORTS
        if gross_sol <= 0:
            if await self._sell_without_route(position, reason, manual=manual,
                                              percent=percent):
                result["executed"] = True
                result["liquidation"] = "unpriced"
                result["reason"] = _UNPRICED_NOTE
            else:
                result["reason"] = (_UNPRICED_PARTIAL_NOTE if manual
                                    else "Devis de vente vide.")
            return result

        # A route exists again: forget the earlier failures, and clear the
        # counter on the record so the row stops advertising them.
        self._no_route.pop(position["id"], None)
        if position.get("no_route_failures"):
            store.update_position(position["id"], {"no_route_failures": 0})

        sell_fee = fee_model.sell_fee_sol(settings)
        # The real path reports the wallet's *net* change for the sell and adds the
        # fee back afterwards, so do the same here: both P&Ls then have one shape.
        sol_received = gross_sol - sell_fee
        route = fill.get("_route") or ""
        result["executed"] = True
        result["gross_sol"] = gross_sol
        result["sol_received"] = sol_received
        result["route"] = route
        store.log(
            f"📝 VENTE PAPIER {percent:.0f}% de {symbol} ({reason}) — "
            f"{gross_sol:.9f} SOL reçus · route {route} "
            "(cotée, non simulable: les tokens sont virtuels) · aucun ordre envoyé",
            level="info", token=address, action="sell",
        )

        remaining_raw = max(raw_total - amount, 0)
        if percent >= 99.9 or remaining_raw <= 0:
            await self._close_position(position, reason, sol_received=sol_received,
                                       sell_fee=sell_fee)
        else:
            sold = min(max(percent / 100.0, 0.0), 1.0)
            patch: dict = {"tokens": remaining_raw / (10 ** decimals)}
            if position.get("cost_usd"):
                patch["cost_usd"] = round(float(position["cost_usd"]) * (1 - sold), 6)
            if position.get("amount_sol"):
                patch["amount_sol"] = round(float(position["amount_sol"]) * (1 - sold), 9)
            patch["proceeds_sol"] = round(
                float(position.get("proceeds_sol") or 0.0) + sol_received, 9)
            patch["sell_fees_sol"] = round(
                float(position.get("sell_fees_sol") or 0.0) + sell_fee, 9)
            store.update_position(position["id"], patch)
            store.push_position_update(store.get_position(position["id"]) or position)
        store.recompute_stats()
        store.broadcast({"type": "status"})
        return result

    async def _sell_without_route(self, position: dict, reason: str, *,
                                  manual: bool, percent: float) -> bool:
        """Decide what « aucune route de vente » means for this position.

        Returns True when the position was closed (booked out at the last price).

        * An **automatic** exit rule (SL, trailing) fires every few seconds, so it
          counts its attempts and only writes the bag off after ``UNPRICED_GIVE_UP``
          identical refusals — that is the anti-spin rule measured on SOOTS.
        * An **explicit click** is a decision, not a retry: the user asked once, and
          making them click four times would look like a broken button. The record
          is the same one — labelled, non-executable, no sale.
        * A **partial** sale cannot be booked that way (only the whole bag may be
          written off), so it is refused with the instruction that closes it.
        """
        symbol = position.get("symbol", "?")
        if manual and percent >= 99.9:
            await self._liquidate_unpriced(position, reason, attempts=1, manual=True)
            return True
        if manual:
            store.log(
                f"⛔ Vente PAPIER partielle de {symbol} refusée — aucune route de "
                "vente (seule la fermeture totale peut solder au dernier prix connu)",
                level="error", token=position["address"], action="sell",
            )
            return False
        return await self._count_no_route(position, reason)

    async def _count_no_route(self, position: dict, reason: str) -> bool:
        """Record one « aucune route de vente » failure; True if it liquidated.

        Only this failure is counted — a 429 or a timeout takes another branch in
        `_paper_sell` — so a temporary API problem can never push a position
        towards a made-up price. The count is written on the position itself so a
        restart does not restart the wait from zero.
        """
        pos_id = position["id"]
        # `store.update_position` aliases the stored dict, so the field and the
        # in-memory count can already agree: take the larger, never the sum.
        previous = max(int(position.get("no_route_failures") or 0),
                       self._no_route.get(pos_id, 0))
        fails = previous + 1
        self._no_route[pos_id] = fails
        symbol = position.get("symbol", "?")
        if fails < UNPRICED_GIVE_UP:
            store.log(
                f"⛔ Vente PAPIER impossible {symbol} — aucune route de vente "
                f"({fails}/{UNPRICED_GIVE_UP}) · la position reste ouverte",
                level="error", token=position["address"], action="sell",
            )
            store.update_position(pos_id, {"no_route_failures": fails})
            return False
        self._no_route.pop(pos_id, None)
        await self._liquidate_unpriced(position, reason, attempts=fails)
        return True

    async def _liquidate_unpriced(self, position: dict, reason: str, *,
                                  attempts: int, manual: bool = False) -> dict:
        """Close a paper position no market exists for, at the last price seen.

        This is not a sale: with no route there is no executable price to measure
        at all. The position is booked out at the last Dexscreener price so the
        book stops carrying a bag that can never be sold — and the record says in
        as many words that the price was not executable, so nobody can read the
        PnL as money that was actually received.
        """
        address = position["address"]
        symbol = position.get("symbol", "?")
        # One price for the whole act: the record written below and the journal
        # line must not disagree about what the bag was booked out at.
        price = (await self._price_for(address)
                 or float(position.get("current_price") or 0.0))
        tokens = float(position.get("tokens") or 0.0)
        sol_price = float(store.runtime.get("sol_price") or 0.0) or await get_sol_price()
        sell_fee = fee_model.sell_fee_sol(config.all())
        gross = (tokens * price / sol_price) if (price > 0 and sol_price > 0) else 0.0
        sol_received = max(gross - sell_fee, 0.0)
        updated = await self._close_position(
            position, reason, sol_received=sol_received, sell_fee=sell_fee,
            unpriced=True, attempts=attempts, price=price,
        )
        cause = "à votre demande" if manual else f"après {attempts} tentatives"
        store.log(
            f"⚠️ {symbol} : aucune route de vente {cause} — "
            f"position papier liquidée au dernier prix connu ${price:.10f} "
            "(Dexscreener, NON exécutable) · aucune vente n'a eu lieu",
            level="warn", token=address, action="sell",
        )
        return updated

    # -------------------------------------------------------------- jupiter
    async def _jupiter_swap(self, *, input_mint: str, output_mint: str, amount: int,
                            side: str, dry_run: bool = False,
                            verify_route: bool = True) -> dict:
        """Quote -> Jupiter builds -> simulation -> local signature -> RPC send -> confirm.

        ``dry_run`` stops right after the simulation: the route is real, the
        quoted output is real and, when the wallet can pay for it, the simulation
        is real too — but nothing is signed and nothing is broadcast. That is what
        paper mode trades on: the same route a real order would have taken, at the
        same price and the same price impact, minus the transaction.

        ``verify_route=False`` skips both the build and the simulation and keeps
        only the quote. A sale of *virtual* units cannot be simulated: the wallet
        does not hold the tokens, so the node refuses it whatever the route is —
        the simulation would say nothing about the market. The quote still proves
        a route exists and gives the price a sale would really get.
        """
        settings = config.all()
        slippage_bps = max(1, int(round(float(settings["slippage"]) * 100)))
        max_priority = max(1_000, int(round(float(settings["priority_fee"]) * LAMPORTS)))
        token_addr = output_mint if side == "buy" else input_mint
        local = solana.wallet_address()
        sim: dict = {}
        quote: dict = {}

        # A route Jupiter returns can be unexecutable at that instant. Measured
        # on this machine: a "Whirlpool > Manifest" route failed simulation with
        # InstructionError [6, Custom 1] while three other routes for the very
        # same 0,001 SOL swap succeeded — on the configured node *and* on a
        # second one, so it is the program's verdict, not a flaky node. Sending
        # that transaction would pay the base fee and the priority fee (up to
        # 105 000 lamports, 10 % of a 0,001 SOL trade) for nothing, and the buy
        # would simply not happen. One free simulation avoids it; a refused route
        # is replaced by a fresh quote before anything is broadcast.
        built: dict = {}
        for attempt in range(1, ROUTE_ATTEMPTS + 1):
            quote = await jupiter.quote(
                input_mint=input_mint,
                output_mint=output_mint,
                amount=int(amount),
                slippage_bps=slippage_bps,
            )
            if not verify_route:
                # Priced, not simulated: see the docstring. `sim` stays empty so
                # the caller reports the route as unvalidated rather than passed.
                sim = {}
                break
            built = await jupiter.swap_transaction(
                quote_response=quote,
                user_public_key=local,
                max_priority_lamports=max_priority,
            )
            sim = {}
            try:
                sim = await solana.simulate_signed(
                    solana.sign_only(built["swapTransaction"])
                )
            except Exception as exc:  # noqa: BLE001
                # An unreadable simulation is not a refusal: Jupiter's own
                # pre-flight has already passed the quote, so send it.
                store.log(
                    f"Simulation indisponible ({str(exc)[:60]}) — envoi direct.",
                    level="warn", token=token_addr, action=side,
                )
                break
            if simulation_refusal_is_funding(sim):
                # Not the route's fault — the wallet cannot pay. A fresh route would
                # change nothing, so a real order is refused with the real cause
                # instead of three useless retries. Paper mode carries on: the
                # quoted price is the price the market offers, whatever our balance,
                # and the fill is flagged as unvalidated so the report stays honest.
                if dry_run:
                    # A note on the trade that was just opened — never a second
                    # one. This used to be logged with `action="buy"` too, so a
                    # single purchase showed up twice in the journal and read as
                    # the bot sniping the same token twice.
                    store.log(
                        "↳ même achat : simulation impossible faute de SOL — prix "
                        "coté conservé, exécution non validée.",
                        level="warn", token=token_addr, action="note",
                    )
                    break
                raise solana.SolanaError(
                    "Fonds insuffisants (SOL ou token) pour cette transaction — "
                    "rien n'a été envoyé."
                )
            if not sim.get("err"):
                break
            store.log(
                f"Route refusée par la simulation ({sim.get('err')}) — nouvelle route "
                f"({attempt}/{ROUTE_ATTEMPTS}).",
                level="warn", token=token_addr, action=side,
            )
            if attempt == ROUTE_ATTEMPTS:
                raise solana.SolanaError(
                    f"Toutes les routes ont été refusées par la simulation "
                    f"({sim.get('err')}) — transaction non envoyée, aucun frais payé."
                )
            await asyncio.sleep(0.35)

        if dry_run:
            # Paper mode stops here: the route is the one a real order would have
            # taken and the quoted output is what it would have paid. Whether the
            # simulation validated it is reported instead of assumed — a route
            # that could not be checked is flagged `_quoted_only`/`_funding_only`
            # rather than being called a pass.
            return {
                "_paper": True,
                "_out_amount": int(quote.get("outAmount") or 0),
                "_in_amount": int(quote.get("inAmount") or 0),
                "_price_impact": float(quote.get("priceImpactPct") or 0.0),
                "_route": " > ".join((step.get("swapInfo") or {}).get("label", "?")
                                     for step in (quote.get("routePlan") or [])) or "?",
                # True only when the node really validated this route. A simulation
                # that only failed on the wallet's balance proves nothing about the
                # route, so it is reported separately instead of being called a pass.
                "_simulated": bool(sim) and not sim.get("err")
                              and not simulation_refusal_is_funding(sim),
                "_funding_only": simulation_refusal_is_funding(sim),
                "_quoted_only": not verify_route,
            }

        signature = await solana.sign_and_send(built["swapTransaction"])
        store.log(
            f"Swap Jupiter ({side}) envoyé — {signature[:12]}…",
            level="info", token=token_addr, action=side,
        )

        confirmed = True
        try:
            await solana.confirm(signature)
        except solana.SolanaError as exc:
            # A confirmation we could not observe is not proof of failure: the
            # chain decides below.
            confirmed = False
            store.log(f"Confirmation non observée ({exc}) — vérification on-chain…",
                      level="warn", token=token_addr, action=side)

        # Always read the chain afterwards. A missed confirmation must never
        # leave real tokens untracked (a bag nobody would ever sell).
        mint = output_mint if side == "buy" else input_mint
        held, held_raw = 0.0, 0
        read_failed = False
        try:
            held, _decimals, held_raw = await solana.token_ui_balance(local, mint)
        except Exception as exc:  # noqa: BLE001
            # A flaky node must never turn a completed swap into a "failed buy":
            # that is how a bag ends up held with no position record at all.
            read_failed = True
            store.log(
                f"Solde de tokens illisible ({str(exc)[:80]}) — la position sera "
                "complétée en tâche de fond.",
                level="warn", token=token_addr, action=side,
            )
        result = {
            "tx_hash": signature,
            "status": "confirmed" if confirmed else "unconfirmed",
        }
        if side == "buy":
            if held_raw <= 0 and not confirmed and not read_failed:
                raise solana.SolanaError(
                    f"Achat non confirmé et aucun token reçu ({signature[:12]}…)"
                )
            result["_tokens"] = held
            return result

        # Sell: report what is really left, and the real proceeds (net of fees).
        result["_remaining_tokens"] = held
        # Reading the ledger must never turn a completed swap into a "failed
        # sell": the tokens are gone either way, so a hiccup only costs us the
        # exact figures (the caller then falls back on the quote).
        try:
            effects = await solana.transaction_effects(signature, local)
        except Exception as exc:  # noqa: BLE001
            store.log(
                f"Effets de la vente illisibles ({str(exc)[:70]}) — PnL estimé sans "
                "les frais exacts.", level="warn", token=token_addr, action=side,
            )
            effects = {}
        delta = effects.get("delta")
        if delta is not None and delta > 0:
            result["_sol_received"] = delta
        if effects.get("fee") is not None:
            result["_sell_fee"] = float(effects["fee"])
        return result

    # ----------------------------------------------------------------- sell
    async def sell(self, position: dict, *, percent: float = 100.0, reason: str = "manual",
                   manual: bool = False) -> dict:
        address = position["address"]
        symbol = position.get("symbol", "?")
        result = {"executed": False, "reason": ""}

        # Percentages arrive from the UI and from the exit rules: clamp them so a
        # stray 0 / negative / >100 can never turn into an accidental full dump.
        try:
            percent = float(percent)
        except (TypeError, ValueError):
            percent = 100.0
        percent = min(max(percent, 0.0), 100.0)
        if percent <= 0:
            result["reason"] = "Pourcentage de vente nul ou négatif."
            return result

        if position.get("paper"):
            # A paper position owns no tokens on the chain, so it is always closed
            # virtually — even if paper mode was switched off in the meantime, and
            # even with no wallet configured at all. The converse holds too: a real
            # position is always sold for real, whichever mode is on, so no bag is
            # ever silently abandoned.
            return await self._paper_sell(position, percent=percent, reason=reason,
                                          manual=manual)

        if not self.wallet:
            await self.refresh_wallet()
        if not self.wallet:
            result["reason"] = "Aucun wallet configuré."
            return result

        store.log(f"Vente {percent:.0f}% de {symbol} ({reason})", level="info",
                  token=address, action="sell")
        backend = str(config.get("execution_backend") or "gmgn")
        try:
            if backend == "jupiter":
                _ui, _decimals, raw = await solana.token_ui_balance(self.wallet, address)
                if raw <= 0:
                    result["reason"] = "Solde de tokens nul sur la chaîne: vente impossible."
                    store.log(f"Vente impossible {symbol}: aucun token détenu.",
                              level="warn", token=address, action="sell")
                    return result
                amount = sell_amount_units(raw, percent)
                if amount <= 0:
                    result["reason"] = (
                        f"Montant à vendre nul: {raw} unité(s) × {percent:.1f}% "
                        "arrondit à 0 — augmentez le pourcentage."
                    )
                    store.log(f"Vente de {symbol} refusée — {result['reason']}",
                              level="warn", token=address, action="sell")
                    return result
                resp = await self._jupiter_swap(
                    input_mint=address, output_mint=SOL_MINT, amount=amount, side="sell",
                )
            else:
                resp = await gmgn.swap(
                    wallet=self.wallet,
                    input_token=address,
                    output_token=SOL_MINT,
                    percent=percent,
                    slippage=int(config.get("slippage")),
                    auto_slippage=True,
                    priority_fee=float(config.get("priority_fee")),
                    anti_mev=bool(config.get("anti_mev")),
                )
        except GMGNPlanRequired as exc:
            store.runtime["trading_blocked"] = True
            store.runtime["trading_blocked_reason"] = str(exc)
            store.runtime["trading_blocked_url"] = getattr(exc, "upgrade_url", "")
            result["reason"] = str(exc)
            store.log(f"⛔ Vente impossible — plan GMGN insuffisant: {exc}", level="error",
                      token=address, action="sell")
            return result
        except Exception as exc:  # noqa: BLE001
            result["reason"] = f"Échec de la vente: {exc}"
            store.log(f"Échec de la vente {symbol}: {exc}", level="error",
                      token=address, action="sell")
            return result

        result["executed"] = True
        result["tx_hash"] = resp.get("tx_hash") or resp.get("txHash") or ""

        sol_received = float(resp.get("_sol_received") or 0.0)
        sell_fee = float(resp.get("_sell_fee") or 0.0)
        if percent >= 99.9:
            await self._close_position(position, reason, sol_received=sol_received,
                                       sell_fee=sell_fee)
        else:
            # Prefer the balance the chain actually reports over a subtraction,
            # and keep the cost basis proportional so the remaining PnL has meaning.
            remaining = resp.get("_remaining_tokens")
            sold = percent / 100.0
            if remaining is None:
                remaining = max(float(position.get("tokens") or 0) * (1 - sold), 0)
            patch: dict = {"tokens": float(remaining)}
            if position.get("cost_usd"):
                patch["cost_usd"] = round(float(position["cost_usd"]) * (1 - sold), 6)
            if position.get("amount_sol"):
                patch["amount_sol"] = round(float(position["amount_sol"]) * (1 - sold), 9)
            # Accumulate what the partial sale really brought in (fees included)
            # so the PnL written at the final close covers *every* sale.
            if sol_received > 0:
                patch["proceeds_sol"] = round(
                    float(position.get("proceeds_sol") or 0.0) + sol_received, 9)
            if sell_fee > 0:
                patch["sell_fees_sol"] = round(
                    float(position.get("sell_fees_sol") or 0.0) + sell_fee, 9)
            store.update_position(position["id"], patch)
            store.push_position_update(store.get_position(position["id"]) or position)
        store.recompute_stats()
        store.broadcast({"type": "status"})
        return result

    async def _close_position(self, position: dict, reason: str,
                              sol_received: float = 0.0, sell_fee: float = 0.0,
                              *, unpriced: bool = False, attempts: int = 0,
                              price: float | None = None) -> dict:
        pos_id = position["id"]
        self._closing.add(pos_id)
        try:
            paper = bool(position.get("paper"))
            if paper:
                # Nothing was ever paid on the chain: there is no token account to
                # close and no transaction to look up. The same modelled fees the
                # "fees" criterion uses are booked instead, so a paper PnL and a
                # real one are directly comparable (and slightly pessimistic:
                # the priority fee is rarely consumed in full).
                rent, close_fee = 0.0, fee_model.close_fee_sol(config.all())
                buy_fee = float(position.get("buy_fee_sol")
                                or fee_model.buy_fee_sol(config.all()))
            else:
                # Close the now-empty token account first: that transaction is the
                # last lamport this position will ever cost, so every fee is known
                # before the PnL is written and the figure is final in one go.
                rent, close_fee = await self._recover_rent(position)
                buy_fee = await self._tx_fee(position.get("tx_buy") or "")

            price = price if price is not None else (
                await self._price_for(position["address"])
                or float(position.get("current_price") or 0))
            tokens = float(position.get("tokens") or 0)
            entry = float(position.get("entry_price") or 0)
            cost = float(position.get("cost_usd") or 0)
            committed = float(position.get("sol_committed") or position.get("amount_sol") or 0)
            # A partial sell prorates ``cost_usd``/``tokens`` to describe what is
            # still held, but the final return and the average exit price must be
            # measured against the *whole* position — otherwise selling half the
            # bag makes the loss look twice as big and the exit price twice as high.
            cost_total = float(position.get("cost_usd_total") or cost)
            tokens_bought = float(position.get("tokens_bought") or tokens)
            # Every sale of the position, not only the last one.
            proceeds = float(position.get("proceeds_sol") or 0.0) + sol_received
            sell_fees_total = sell_fee + float(position.get("sell_fees_sol") or 0.0)
            fees = buy_fee + sell_fees_total + close_fee

            value = tokens * price
            pnl_usd = value - cost if cost else 0.0
            pnl_pct = ((price / entry - 1) * 100.0) if entry else 0.0
            exit_price = 0.0
            if proceeds > 0 and cost_total > 0:
                sol_price = float(store.runtime.get("sol_price") or 0.0) or await get_sol_price()
                realised = realized_pnl(
                    proceeds_sol=proceeds,
                    sell_fees=sell_fees_total,
                    committed_sol=committed,
                    buy_fee=buy_fee,
                    close_fee=close_fee,
                    sol_price=sol_price,
                    cost_usd=cost_total,
                    tokens=tokens_bought,
                )
                pnl_usd = realised["pnl_usd"]
                pnl_pct = realised["pnl_percent"]
                exit_price = realised["exit_price"]
                fees = realised["fees_sol"]
            patch = {
                "status": "closed",
                "closed_at": int(time.time() * 1000),
                "current_price": price,
                "pnl_usd": round(pnl_usd, 4),
                "pnl_percent": round(pnl_pct, 2),
                "exit_reason": reason,
                "tokens": 0.0,
                # The last sale's net, exactly as the swap that just ran reported it.
                "sol_received": round(sol_received, 9),
                # …and the running totals across *every* sale, so a record closed
                # after a partial exit stays coherent with the PnL it carries:
                # proceeds + sell fees = what the position really took in.
                "proceeds_sol": round(proceeds, 9),
                "sell_fees_sol": round(sell_fees_total, 9),
                # Total transaction fees the position cost (network + priority,
                # buy + sells + close), so the PnL can be read as fee-inclusive.
                "fees_sol": round(fees, 9),
            }
            if exit_price > 0:
                patch["exit_price"] = exit_price
            if rent:
                patch["rent_recovered"] = round(rent, 9)
            if unpriced:
                # The price is the last Dexscreener print, not something the
                # market would have paid: say it on the record itself, so the
                # dashboard, the history and any later export all repeat it.
                patch["exit_unpriced"] = True
                patch["exit_price_source"] = "dexscreener"
                patch["exit_note"] = _UNPRICED_NOTE
                patch["no_route_failures"] = attempts
            store.update_position(pos_id, patch)
            updated = store.get_position(pos_id) or {**position, **patch}
            store.push_position_update(updated)
            note = (f" · {_UNPRICED_NOTE} — aucun ordre n'a eu lieu" if unpriced else "")
            store.log(
                f"{'Position papier fermée' if paper else 'Position fermée'}: "
                f"{position.get('symbol')} — PnL ${pnl_usd:+.4f} "
                f"({pnl_pct:+.1f}%) [{reason}] · frais {fees:.9f} SOL inclus{note}",
                level="warn" if (unpriced or pnl_usd < 0) else "success",
                token=position["address"], action="sell",
            )
            if not paper:
                # A virtual close moves no lamport, so there is nothing to re-read.
                await self.refresh_wallet_balance()
            return updated
        finally:
            self._closing.discard(pos_id)

    async def _recover_rent(self, position: dict) -> tuple[float, float]:
        """Close the empty token account: returns ``(rent_refunded, close_fee)``.

        The rent is a *refund*, not trading proceeds, so it stays out of the PnL
        and is reported on its own (``rent_recovered``). The closing transaction's
        fee, on the other hand, is a real cost: it is handed back to the caller so
        the PnL can include it.
        """
        try:
            result = await solana.close_token_account(position["address"])
        except Exception as exc:  # noqa: BLE001
            store.log(f"Loyer non récupéré ({position.get('symbol')}): {exc}",
                      level="warn", token=position["address"], action="sell")
            return 0.0, 0.0
        rent = float((result or {}).get("rent") or 0.0)
        fee = float((result or {}).get("fee") or 0.0)
        if rent > 0:
            store.log(
                f"Loyer récupéré: {rent:.9f} SOL ({position.get('symbol')})",
                level="success", token=position["address"], action="sell",
            )
        return rent, fee

    async def _tx_fee(self, signature: str) -> float:
        """Fee a transaction really cost (base **and** priority); 0.0 if unknown.

        Reading it at close time keeps the buy path free of any extra round-trip:
        latency on a snipe matters, the fee does not need to be known before it is
        spent.
        """
        if not signature:
            return 0.0
        try:
            fee = await solana.transaction_fee(signature)
        except Exception:  # noqa: BLE001
            return 0.0
        return float(fee or 0.0)

    # ------------------------------------------------------- orphan recovery
    async def adopt_orphans(self) -> int:
        """Take over tokens held with no position record.

        A swap that lands while the bookkeeping fails (node error, crash,
        restart) leaves a real bag that no rule would ever sell. Such tokens are
        adopted at their current price so TP/SL/trailing apply again, and flagged
        ``recovered`` so their origin stays visible.
        """
        if not self.wallet:
            await self.refresh_wallet()
        if not self.wallet:
            return 0
        try:
            held = await solana.list_token_balances(self.wallet)
        except Exception as exc:  # noqa: BLE001
            store.log(f"Balayage des tokens impossible: {str(exc)[:120]}",
                      level="warn", action="recover")
            return 0

        known = {p["address"] for p in store.open_positions()}
        adopted = 0
        for item in held:
            mint = item["mint"]
            if mint in known:
                continue
            market = await dexscreener.token(mint, ttl=1.0)
            price = float(market.get("price_usd") or 0.0)
            if not price:
                store.log(
                    f"Token non suivi détecté ({mint[:8]}…, {item['amount']:.4f} unités) "
                    "mais aucun prix disponible — non adopté.",
                    level="warn", token=mint, action="recover",
                )
                continue
            position = {
                "id": f"rec-{int(time.time() * 1000)}-{mint[:6]}",
                "address": mint,
                "symbol": market.get("symbol") or mint[:6],
                "name": market.get("name") or "",
                "logo": "",
                "wallet": self.wallet,
                "status": "open",
                "manual": False,
                "recovered": True,
                "opened_at": int(time.time() * 1000),
                "entry_price": price,
                "current_price": price,
                "peak_price": price,
                "trailing_active": False,
                "trailing_enabled": bool(config.get("trailing_enabled")),
                "tp_percent": float(config.get("tp_percent")),
                "sl_percent": float(config.get("sl_percent")),
                "trailing_percent": float(config.get("trailing_percent")),
                "tokens": float(item["amount"]),
                "tokens_bought": float(item["amount"]),
                # Unknown by definition: kept at 0 so no invented PnL is shown.
                "cost_usd": 0.0,
                "cost_usd_total": 0.0,
                "amount_sol": 0.0,
                "sol_price_entry": float(store.runtime.get("sol_price") or 0.0),
                "pnl_usd": 0.0,
                "pnl_percent": 0.0,
                "exit_reason": "",
            }
            store.add_position(position)
            store.log(
                f"Position récupérée: {position['symbol']} — {item['amount']:.4f} unités "
                "détenues sans suivi, adoptées au prix courant (sortie désormais "
                "gérée, PnL d'entrée inconnu).",
                level="warn", token=mint, action="recover",
            )
            adopted += 1
        if adopted:
            store.recompute_stats()
            store.broadcast({"type": "status"})
        return adopted

    # ------------------------------------------------- executable price check
    async def _executable_price(self, mint: str, *, raw: int | None = None,
                               decimals: int | None = None) -> float:
        """USD price a sale of the held amount would really get, right now.

        The Dexscreener quote is a *display* price: for a brand-new pump.fun
        token its pool is often not indexed at all (``liquidity: 0``) and the
        reported price can sit far below the market — that fired a stop loss on a
        price no buyer was offering. A Jupiter quote is the price a sale would
        genuinely get, so it is used to double-check an exit. ``0.0`` when no
        quote could be obtained (the caller then keeps the original decision).

        ``raw``/``decimals`` price a position that has no token account on the
        chain — a paper position. Without them the real on-chain balance is read.
        """
        if raw is None or decimals is None:
            try:
                account = await solana.token_account(self.wallet, mint)
            except Exception:  # noqa: BLE001
                return 0.0
            if not account or int(account.get("raw") or 0) <= 0:
                return 0.0
            raw = int(account["raw"])
            decimals = int(account.get("decimals") or 0)
        if raw <= 0:
            return 0.0
        tokens = raw / float(10 ** int(decimals or 0))
        try:
            settings = config.all()
            quote = await jupiter.quote(
                input_mint=mint,
                output_mint=SOL_MINT,
                amount=raw,
                slippage_bps=max(1, int(round(float(settings["slippage"]) * 100))),
            )
            out = int(quote.get("outAmount") or 0)
        except Exception as exc:  # noqa: BLE001
            store.log(
                f"Devis de contrôle indisponible ({str(exc)[:70]}) — la sortie décidée "
                "est maintenue.", level="warn", token=mint, action="exit_check",
            )
            return 0.0
        sol_price = float(store.runtime.get("sol_price") or 0.0)
        if out <= 0 or tokens <= 0 or sol_price <= 0:
            return 0.0
        return (out / LAMPORTS) * sol_price / tokens

    async def _confirm_exit(self, pos: dict, reason: str, price: float,
                            peak: float, entry: float) -> str:
        """Re-check an exit against the price a sale would really get.

        Dexscreener's price is a *display* price, and for a brand-new pump.fun
        token whose only pair has ``liquidity: 0`` it can sit far from the real
        market (measured on ZUCK: display $3.547e-06, executable $2.947e-06). A
        take-profit decided on that price realised a **loss** while still being
        labelled ``take_profit``. The two rules are therefore treated differently:

        * a **take-profit** is only honoured when the executable price is itself
          at the take-profit level — a profit nobody is paying is not a profit,
          so the exit is cancelled (the position keeps running);
        * a **protective** exit (stop-loss / trailing stop) is never weakened:
          it fires as decided when the two prices agree closely, and wherever the
          executable price confirms a breach; a large disagreement is recomputed
          so a display crash cannot trigger a stop no buyer was offering.

        Returns the reason to honour (``""`` cancels the exit). An unavailable
        quote keeps the original decision, so protection is never silently
        disabled.
        """
        if pos.get("paper"):
            # A paper position has no token account: its own recorded units are the
            # amount a sale would move, so the check stays meaningful (and keeps
            # catching a display price the market is not offering).
            raw = int(round(float(pos.get("tokens") or 0) * (10 ** int(pos.get("decimals") or 0))))
            real = await self._executable_price(pos["address"], raw=raw,
                                                decimals=int(pos.get("decimals") or 0))
        else:
            real = await self._executable_price(pos["address"])
        if real <= 0 or price <= 0:
            return reason

        gap = abs(real - price) / price

        # A protective rule the display and the executable price agree on is
        # taken as decided. A take-profit is *always* re-checked below.
        if reason != "take_profit" and gap < _EXIT_PRICE_TOLERANCE:
            return reason

        trailing_active = bool(pos.get("trailing_active"))
        if pos.get("trailing_enabled") and entry and real > entry:
            trailing_active = True
        real_reason = exit_decision(
            price=real,
            peak=max(peak, real),
            pnl_pct=((real / entry - 1) * 100.0) if entry else 0.0,
            tp_percent=float(pos.get("tp_percent") or 0),
            sl_percent=float(pos.get("sl_percent") or 0),
            trailing_percent=float(pos.get("trailing_percent") or 0),
            trailing_active=trailing_active,
        )
        if real_reason == reason:
            return reason
        self._log_exit_recheck(pos["address"], reason, price, real, real_reason)
        # Kept for the dashboard: the row shows the *displayed* PnL, so without
        # this the screen claims a take-profit was reached while the market never
        # paid it, and nothing on the row explains the exit that did not happen
        # (that is exactly how a working rule looked broken). `_revalue` copies it
        # onto the position and clears it on the next tick with no disagreement.
        self._exit_check[pos["address"]] = {
            "reason": reason,
            "display": price,
            "exec": real,
            "real": real_reason or "",
            "at": time.time(),
        }
        return real_reason

    _last_exit_log: dict[str, float] = {}
    # Last exit whose executable price disagreed with the display, per token.
    _exit_check: dict[str, dict] = {}

    def _log_exit_recheck(self, mint: str, reason: str, price: float,
                          real: float, real_reason: str) -> None:
        """Report a recalculated exit, at most once a minute per token.

        A display price that stays wrong would otherwise write the same warning
        on every 5-second tick.
        """
        now = time.monotonic()
        if now - self._last_exit_log.get(mint, 0.0) < 60.0:
            return
        self._last_exit_log[mint] = now
        store.log(
            f"Sortie « {reason} » recalculée: prix Dexscreener ${price:.10f} ≠ prix "
            f"exécutable ${real:.10f} -> règle appliquée: "
            + (f"« {real_reason} »" if real_reason else "aucune (sortie annulée)"),
            level="warn", token=mint, action="exit_check",
        )

    # --------------------------------------------------------- price helpers
    async def _price_for(self, address: str) -> float:
        data = await dexscreener.token(address, ttl=1.0)
        return float(data.get("price_usd") or 0.0)

    async def refresh_wallet_balance(self) -> None:
        address, balance = await resolve_wallet()
        if address:
            self.wallet = address
            self.sol_balance = balance
            store.runtime["wallet_balance_sol"] = balance

    # ------------------------------------------------------ position manager
    async def _manage_loop(self) -> None:
        counter = 0
        while self._running:
            try:
                positions = store.open_positions()
                # The live feed already polls these tokens (continuously, in one
                # batch); exits read its freshest tick. A direct read happens
                # only when that tick is too old — feed paused, disabled or in
                # backoff — so protection never depends on a stalled tracker.
                stale_ms = max(3000.0, float(config.get("price_feed_seconds") or 2.0) * 3000.0)
                for pos in positions:
                    quote, age = price_feed.quote(pos["address"])
                    if not quote or age > stale_ms:
                        quote = await dexscreener.token(pos["address"], ttl=1.0)
                    await self._revalue(pos, quote)
                counter += 1
                if counter % 12 == 0:
                    await self.refresh_wallet_balance()
                    await get_sol_price()
                    store.broadcast({"type": "status"})
            except Exception as exc:  # noqa: BLE001
                store.log(f"Erreur du suivi de positions: {exc}", level="error", action="monitor")
            await asyncio.sleep(float(config.get("price_poll_seconds") or 5.0))

    async def _revalue(self, pos: dict, quote: dict) -> None:
        if pos.get("id") in self._closing:
            # Being closed right now: the token account is already empty, so a
            # fresh exit decision would only try to sell a bag that is gone.
            return
        if pos.get("status") == "closed":
            # A closed trade is finished: its realised PnL is frozen and no later
            # quote for that token may reopen it. `_manage_loop` only ever reads
            # open positions, so this changes nothing today — it states the
            # invariant at the source instead of trusting every caller with it.
            return
        price = float(quote.get("price_usd") or 0.0)
        if not price:
            price = float(pos.get("current_price") or 0.0)
        if not price:
            return

        entry = float(pos.get("entry_price") or 0.0)
        tokens = float(pos.get("tokens") or 0.0)
        # The whole committed cost, not the prorated one: the cash a partial sell
        # already banked is added back below, so the two must be read together.
        cost = float(pos.get("cost_usd_total") or pos.get("cost_usd") or 0.0)
        peak = max(float(pos.get("peak_price") or entry), price)

        banked = (float(pos.get("proceeds_sol") or 0.0)
                  * float(store.runtime.get("sol_price") or pos.get("sol_price_entry") or 0.0))
        pnl_pct = ((price / entry - 1) * 100.0) if entry else 0.0
        pnl_usd = (tokens * price + banked - cost) if cost else 0.0
        if not tokens and cost:
            # Fall back to a proportional estimate when the token balance is unknown.
            pnl_usd = cost * pnl_pct / 100.0 + banked

        # Trailing stop: armed as soon as the trade is in profit, then it exits
        # when the price falls `trailing_percent` below the highest price seen.
        # (It previously also required a +trailing_percent gain *before* arming,
        # which made it unreachable whenever take-profit was <= trailing_percent.)
        trailing_active = bool(pos.get("trailing_active"))
        if pos.get("trailing_enabled") and entry and price > entry:
            trailing_active = True

        patch = {
            "current_price": price,
            "peak_price": peak,
            "trailing_active": trailing_active,
            "pnl_usd": round(pnl_usd, 4),
            "pnl_percent": round(pnl_pct, 2),
            "market_cap": quote.get("market_cap"),
            "liquidity": quote.get("liquidity_usd"),
            "volume_24h": quote.get("volume_24h"),
            # Set only when the exit decided above was re-checked against the price
            # a sale would really get and came out different (see `_confirm_exit`).
            # Cleared on every tick with no disagreement, so the row never keeps
            # showing a gap that has closed.
            "exit_recheck": None,
        }

        # ---- exit rules (checked in priority order) ----
        exit_reason = exit_decision(
            price=price,
            peak=peak,
            pnl_pct=pnl_pct,
            tp_percent=float(pos.get("tp_percent") or 0),
            sl_percent=float(pos.get("sl_percent") or 0),
            trailing_percent=float(pos.get("trailing_percent") or 0),
            trailing_active=trailing_active,
        )

        if exit_reason:
            # The display price can lie (unindexed pool). Re-check the exit on
            # the price a sale would really get before acting on it.
            exit_reason = await self._confirm_exit(pos, exit_reason, price, peak, entry)
            if not exit_reason:
                check = self._exit_check.pop(pos["address"], None)
                if check:
                    # The row keeps the displayed PnL, so hand it the executable
                    # one: otherwise "+39 %" on screen with no sale looks like a
                    # broken take-profit instead of a price nobody was paying.
                    patch["exit_recheck"] = check
                store.update_position(pos["id"], patch)
                store.push_position_update(store.get_position(pos["id"]) or pos)
                return
            self._exit_check.pop(pos["address"], None)
            store.update_position(pos["id"], patch, persist=False)
            await self.sell(pos, percent=100.0, reason=exit_reason)
            return

        # No exit decided: an earlier « aucune route » count no longer describes
        # the position, so it must not survive until the rule fires again.
        self._no_route.pop(pos["id"], None)
        if pos.get("no_route_failures"):
            patch["no_route_failures"] = 0
        self._exit_check.pop(pos["address"], None)
        store.update_position(pos["id"], patch)
        store.push_position_update(store.get_position(pos["id"]) or pos)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._manage_loop())

    async def stop(self) -> None:
        self._running = False
        for task in list(self._bg):
            task.cancel()
        self._bg.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None


trader = Trader()
