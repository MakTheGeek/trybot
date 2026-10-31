"""Recalcule le PnL des positions fermées **tous frais inclus**.

Le bot enregistre les SOL réellement reçus à la vente, mais les frais de
transaction (réseau + priorité) payés à l'achat et à la vente étaient jusqu'ici
absents du PnL : sur un trade de 0,001 SOL, ils pèsent ~2 %, donc plus qu'une
bonne partie du résultat. Ce script mesure sur la chaîne, pour chaque position
fermée, les frais des transactions d'achat et de vente (``meta.fee``), ajoute
les 5 000 lamports de la transaction de fermeture du compte de token, puis
réécrit ``pnl_usd`` / ``pnl_percent`` / ``fees_sol`` / ``exit_price`` avec la
même fonction que le bot en direct (``backend.trader.realized_pnl``).

    python tools/backfill_fees.py            # simulation, n'écrit rien
    python tools/backfill_fees.py --apply    # réécrit data/positions.json

À lancer bot **arrêté** : le serveur garde les positions en mémoire et réécrit le
fichier à chaque mise à jour.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import solana_rpc as solana  # noqa: E402
from backend.trader import realized_pnl  # noqa: E402

POSITIONS = ROOT / "data" / "positions.json"
TX_PARAMS = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
             "commitment": "confirmed"}
# Frais de la transaction de fermeture du compte de token : elle est construite
# sans ComputeBudget (aucun frais de priorité), donc 5 000 lamports pile.
CLOSE_FEE = 5000 / solana.LAMPORTS_PER_SOL


def token_amounts(meta: dict, wallet: str) -> dict[str, dict[str, float]]:
    """Soldes de tokens du wallet par mint, avant/après la transaction."""
    out: dict[str, dict[str, float]] = {}
    for phase, key in (("preTokenBalances", "pre"), ("postTokenBalances", "post")):
        for entry in meta.get(phase) or []:
            if entry.get("owner") != wallet:
                continue
            raw = (entry.get("uiTokenAmount") or {}).get("uiAmountString")
            try:
                amount = float(raw) if raw is not None else 0.0
            except ValueError:
                amount = 0.0
            out.setdefault(entry.get("mint"), {})[key] = amount
    return out


def sol_delta(meta: dict, message: dict, wallet: str) -> float | None:
    keys = message.get("accountKeys") or []
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    for index, entry in enumerate(keys):
        pubkey = entry.get("pubkey") if isinstance(entry, dict) else entry
        if pubkey == wallet and index < len(pre) and index < len(post):
            return (float(post[index]) - float(pre[index])) / solana.LAMPORTS_PER_SOL
    return None


async def collect(wallet: str) -> dict[str, dict]:
    """Toutes les transactions récentes du wallet, indexées par signature."""
    signatures = await solana.rpc(
        "getSignaturesForAddress", [wallet, {"limit": 300}], timeout=40.0)
    if not isinstance(signatures, list):
        raise SystemExit("getSignaturesForAddress n'a rien renvoyé.")
    txs: dict[str, dict] = {}
    for entry in reversed(signatures):          # du plus ancien au plus récent
        signature = (entry or {}).get("signature")
        if not signature:
            continue
        try:
            result = await solana.rpc("getTransaction", [signature, TX_PARAMS], timeout=25.0)
        except Exception as exc:                # noqa: BLE001
            print(f"    ! {signature[:12]}… illisible ({str(exc)[:60]})")
            continue
        if not isinstance(result, dict):
            continue
        meta = result.get("meta") or {}
        message = ((result.get("transaction") or {}).get("message") or {})
        txs[signature] = {
            "signature": signature,
            "slot": result.get("slot"),
            "delta": sol_delta(meta, message, wallet),
            "fee": (float(meta["fee"]) / solana.LAMPORTS_PER_SOL)
                   if meta.get("fee") is not None else None,
            "mints": token_amounts(meta, wallet),
        }
    return txs


def find_sell(position: dict, txs: dict[str, dict]) -> dict | None:
    """La transaction qui a soldé le solde du mint de cette position."""
    mint = position["address"]
    buy_slot = (txs.get(position.get("tx_buy") or "") or {}).get("slot") or -1
    best: dict | None = None
    for tx in txs.values():
        balances = tx["mints"].get(mint)
        if not balances:
            continue
        before = float(balances.get("pre") or 0.0)
        after = float(balances.get("post") or 0.0)
        if before <= 0 or after >= before:
            continue                       # ni une vente, ni une sortie complète
        if tx["slot"] < buy_slot:
            continue
        if best is None or tx["slot"] < best["slot"]:
            best = tx
    return best


async def main(apply: bool) -> None:
    positions = json.loads(POSITIONS.read_text(encoding="utf-8"))
    wallet = solana.wallet_address()
    print(f"Wallet {wallet} — {len(positions)} positions, "
          f"lecture de la chaîne…\n")
    txs = await collect(wallet)

    changed = 0
    for position in positions:
        if position.get("status") != "closed":
            continue
        label = (f"{position.get('symbol')} ({position['id']})")
        amount_sol = float(position.get("amount_sol") or 0.0)
        received = float(position.get("sol_received") or 0.0)
        cost = float(position.get("cost_usd") or 0.0)
        if amount_sol <= 0 or received <= 0 or cost <= 0:
            print(f"  {label}: hors modèle (position adoptée) — inchangée")
            continue

        buy = txs.get(position.get("tx_buy") or "")
        sell = find_sell(position, txs)
        buy_fee = float((buy or {}).get("fee") or 0.0)
        sell_fee = float((sell or {}).get("fee") or 0.0)
        close_fee = CLOSE_FEE if float(position.get("rent_recovered") or 0.0) > 0 else 0.0
        tokens = 0.0
        if sell:
            tokens = float((sell["mints"].get(position["address"]) or {}).get("pre") or 0.0)
        if tokens <= 0:                       # repli : entrée = montant / prix
            entry = float(position.get("entry_price") or 0.0)
            if entry > 0:
                sol_price_entry = float(position.get("sol_price_entry") or 0.0)
                tokens = (amount_sol * sol_price_entry / entry) if sol_price_entry else 0.0

        # Prix du SOL au moment de la clôture : reconstitué depuis les chiffres
        # d'alors (pnl = reçu × prix − coût), pour rester comparable à l'ancien.
        old_pnl = float(position.get("pnl_usd") or 0.0)
        sol_price = ((old_pnl + cost) / received) if received > 0 else 0.0
        if sol_price <= 0:
            sol_price = float(position.get("sol_price_entry") or 0.0)

        realised = realized_pnl(
            proceeds_sol=received, sell_fees=sell_fee, committed_sol=amount_sol,
            buy_fee=buy_fee, close_fee=close_fee, sol_price=sol_price,
            cost_usd=cost, tokens=tokens,
        )
        print(f"  {label}")
        print(f"      frais: achat {buy_fee*1e9:,.0f} + vente {sell_fee*1e9:,.0f} "
              f"+ clôture {close_fee*1e9:,.0f} = {realised['fees_sol']*1e9:,.0f} lamports")
        print(f"      PnL  : ${old_pnl:+.4f} ({position.get('pnl_percent'):+.2f}%)"
              f"  ->  ${realised['pnl_usd']:+.4f} ({realised['pnl_percent']:+.2f}%)")
        if tokens > 0:
            print(f"      sortie: {realised['exit_price']:.10g} "
                  f"(vs {position.get('exit_price') or 0:.10g})")
        if apply:
            position["pnl_usd"] = round(realised["pnl_usd"], 4)
            position["pnl_percent"] = round(realised["pnl_percent"], 2)
            position["fees_sol"] = round(realised["fees_sol"], 9)
            if realised["exit_price"] > 0:
                position["exit_price"] = realised["exit_price"]
        changed += 1

    print(f"\n{changed} positions recalculées")
    if apply:
        POSITIONS.write_text(
            json.dumps(positions, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"data/positions.json réécrit ({len(positions)} positions)")
        print("Redémarre le bot : les stats sont recalculées au premier /api/state.")
    else:
        print("Simulation — rien n'a été écrit (ajouter --apply).")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
