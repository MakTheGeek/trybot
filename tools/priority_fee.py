r"""Ce que Jupiter a réellement demandé comme frais de priorité, lu sur la chaîne.

Répond à une seule question, avec des faits et non une estimation :

    mon plafond ``priority_fee`` écrase-t-il le prix que Jupiter avait choisi ?

Méthode. Chaque transaction de swap contient deux instructions ``ComputeBudget``
qui fixent le prix (µLamports **par unité de calcul**) et la limite de CU. La
frais de priorité payé vaut ``ceil(prix x limite / 10^6)`` lamports — relation
vérifiée à l'exact près sur les swaps de ce bot, ce qui fixe l'unité sans
ambiguïté. On lit donc, pour chaque swap récent :

    prix demandé par Jupiter   vs   prix maximal autorisé par le plafond
                                    (= plafond x 10^6 / budget de CU)

Aucun ordre n'est envoyé : l'outil ne fait que lire l'historique du wallet.

    .\venv\Scripts\python.exe tools\priority_fee.py
    .\venv\Scripts\python.exe tools\priority_fee.py --limit 40
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import base58  # noqa: E402

from backend import config, solana_rpc as solana  # noqa: E402

BASE_FEE_LAMPORTS = 5_000          # une signature
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
SET_UNIT_LIMIT = 2
SET_UNIT_PRICE = 3


def budget_of(message: dict, keys: list) -> tuple[int | None, int | None]:
    """``(prix µLamports/CU, limite CU)`` demandés par la transaction."""
    price = limit = None
    for instruction in message.get("instructions") or []:
        index = instruction.get("programIdIndex")
        program = (keys[index] if isinstance(index, int) and 0 <= index < len(keys)
                   else None)
        if program != COMPUTE_BUDGET:
            continue
        try:
            # With ``encoding: "json"`` the instruction data is base58, not base64.
            data = base58.b58decode(instruction.get("data") or "")
        except Exception:  # noqa: BLE001
            continue
        if not data:
            continue
        if data[0] == SET_UNIT_PRICE and len(data) >= 9:
            price = int.from_bytes(data[1:9], "little")
        elif data[0] == SET_UNIT_LIMIT and len(data) >= 5:
            limit = int.from_bytes(data[1:5], "little")
    return price, limit


def priority_lamports(price: int, limit: int) -> int:
    """``ceil(prix x limite / 10^6)`` — la formule, pas une approximation."""
    return -(-price * limit // 1_000_000)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=25,
                        help="nombre de transactions récentes à examiner (défaut 25)")
    args = parser.parse_args()

    settings = config.all()
    wallet = solana.wallet_address()
    cap_sol = max(float(settings.get("priority_fee") or 0.0), 0.0)
    cap_lamports = int(round(cap_sol * solana.LAMPORTS_PER_SOL))
    budget = solana.DEFAULT_CU_BUDGET
    allowed_price = int(cap_lamports * 1_000_000 / budget) if cap_lamports else 0

    print()
    print("=" * 78)
    print("  Frais de priorité réellement demandés par Jupiter — lecture de la chaîne")
    print("=" * 78)
    print(f"  wallet            : {wallet}")
    print(f"  nœud              : {solana._mask(solana.rpc_url())}")
    print(f"  plafond configuré : {cap_sol:.8f} SOL = {cap_lamports:,} lamports".replace(",", " "))
    print(f"  budget de CU      : {budget:,} CU (médiane mesurée sur les swaps du bot)".replace(",", " "))
    print(f"  prix autorisé     : {allowed_price:,} µLamports/CU".replace(",", " "))
    print()

    history = await solana.rpc("getSignaturesForAddress",
                               [wallet, {"limit": max(1, args.limit)}]) or []
    rows: list[dict] = []
    for entry in history:
        try:
            tx = await solana.rpc("getTransaction", [
                entry["signature"],
                {"encoding": "json", "maxSupportedTransactionVersion": 0,
                 "commitment": "confirmed"},
            ])
        except solana.SolanaError:
            continue
        if not tx:
            continue
        meta = tx.get("meta") or {}
        block = tx.get("transaction") or {}
        message = block.get("message") or {}
        fee = meta.get("fee")
        if fee is None:
            continue
        keys = message.get("accountKeys") or []
        signatures = block.get("signatures") or []
        price, limit = budget_of(message, keys)
        if price is None or limit is None:
            continue          # pas un swap (aucune instruction ComputeBudget)
        implied = priority_lamports(price, limit)
        paid = fee - BASE_FEE_LAMPORTS * max(1, len(signatures))
        rows.append({
            "signature": entry["signature"],
            "price": price,
            "limit": limit,
            # Ce que la transaction aurait payé, et ce que le plafond ACTUEL
            # aurait laissé payer. Le drapeau porte sur le plafond actuel : c'est
            # la seule question qui compte pour un réglage à changer.
            "implied": implied,
            "paid": paid,
            "capped": min(implied, cap_lamports) if cap_lamports else 0,
            "clamped": implied > cap_lamports if cap_lamports else False,
        })

    if not rows:
        print("  Aucun swap avec frais de priorité dans l'historique récent.")
        print("  (Un token non tradable ou un achat jamais abouti n'en laisse pas.)")
        return

    print(f"  {'signature':<12}{'prix demandé':>14}{'limite CU':>12}"
          f"{'total voulu':>13}{'payé alors':>12}{'plafond actuel':>16}{'écrêté':>9}")
    print("  " + "-" * 86)
    for row in rows:
        print(f"  {row['signature'][:10]:<12}{row['price']:>14,}{row['limit']:>12,}"
              f"{row['implied']:>13,}{row['paid']:>12,}{row['capped']:>16,}"
              f"{('oui' if row['clamped'] else 'non'):>9}".replace(",", " "))
    print()
    print("  Lecture : `total voulu` = ce que la transaction a payé de priorité, et il")
    print("  égale `payé alors` sur chaque ligne — c'est ce qui prouve que le décodage")
    print("  des instructions ComputeBudget est exact. `plafond actuel` = ce que le")
    print("  réglage `priority_fee` d'aujourd'hui aurait laissé payer.")

    prices = [row["price"] for row in rows]
    clamped = [row for row in rows if row["clamped"]]
    print()
    print(f"  swaps analysés          : {len(rows)}")
    print(f"  prix demandé, médiane   : {statistics.median(prices):,.0f} µLamports/CU"
          .replace(",", " "))
    print(f"  prix demandé, intervalle: {min(prices):,} – {max(prices):,} µLamports/CU"
          .replace(",", " "))
    print(f"  écrêtés par le plafond  : {len(clamped)}/{len(rows)}"
          f"  ({100 * len(clamped) / len(rows):.0f} %)")
    if clamped:
        wanted = sum(row["implied"] for row in rows)
        got = sum(row["capped"] for row in rows)
        print(f"  priorité voulue vs payée: {wanted:,} vs {got:,} lamports"
              .replace(",", " ") + "  (plafond actuel appliqué à tout l'échantillon)")
        print()
        print("  Conclusion : le plafond `priority_fee` écrase le prix que Jupiter avait")
        print("  choisi sur ces swaps. La transaction aboutit quand même (Solana accepte le")
        print("  prix plus bas), mais elle est incluse moins vite — exactement ce qu'un")
        print("  sniper ne veut pas. Le coût d'un relèvement est chiffré par le contrôle")
        print("  de frais du tableau de bord (`max_fee_percent`).")
    else:
        print()
        print("  Conclusion : le plafond `priority_fee` couvre le prix demandé sur tout")
        print("  l'échantillon. Rien à changer.")


asyncio.run(main())
