r"""Auto-contrôle SolSnipBot — vérifie les filtres, les seuils et la chaîne d'exécution.

Aucun ordre n'est envoyé : les swaps sont signés puis **simulés** sur le RPC.
L'analyse de la section 4 est une lecture GMGN réelle (elle consomme 1 à 2 appels).

    .\venv\Scripts\python.exe tools\selfcheck.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from solders.transaction import VersionedTransaction  # noqa: E402

from backend import config, jupiter, net  # noqa: E402
from backend import solana_rpc as solana  # noqa: E402
from backend import trader as trader_mod  # noqa: E402
from backend.clock import clock  # noqa: E402

OK = "  [OK]   "
KO = "  [ECHEC]"
fails: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{OK if condition else KO} {label}" + (f"  — {detail}" if detail else ""))
    if not condition:
        fails.append(label)


def title(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ 1. TLS/RPC
title("1) TLS et RPC")
print(f"  TLS via magasin Windows (truststore) : {net.USING_OS_STORE}")
print(f"  RPC configuré                        : {solana._mask(solana.rpc_url())}")


async def rpc_section() -> None:
    slot = await solana.rpc("getSlot", [])
    check("le RPC répond (getSlot)", isinstance(slot, int) and slot > 0, f"slot={slot}")
    genesis = await solana.rpc("getGenesisHash", [])
    check("identité mainnet-beta",
          genesis == "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d", str(genesis))
    wallet = solana.wallet_address()
    balance = await solana.sol_balance(wallet)
    check("solde du wallet lisible", balance is not None, f"{balance:.9f} SOL")
    # Aucune adresse n'est écrite en dur ici : on compare la clé locale au wallet
    # que GMGN associe à la clé (quand il est connu), sinon on valide la forme.
    linked = getattr(trader_mod, "wallet", "") or ""
    check("adresse de la clé locale lisible (base58, 32-44)",
          isinstance(wallet, str) and 32 <= len(wallet) <= 44,
          wallet)
    check("adresse de la clé locale = wallet suivi",
          (not linked) or wallet == linked,
          f"clé={wallet} gmgn={linked or '— non chargé'}")

    # balance_delta: la lecture du PnL réalisé doit fonctionner sur le vrai RPC.
    history = await solana.rpc(
        "getSignaturesForAddress", [wallet, {"limit": 5}]
    ) or []
    deltas = []
    for entry in history[:3]:
        deltas.append(await solana.balance_delta(entry["signature"], wallet))
    print(f"  deltas SOL des 3 dernières transactions : {deltas}")
    check("balance_delta lit le delta on-chain",
          any(isinstance(d, float) for d in deltas) or not history,
          f"{len(history)} transaction(s) dans l'historique")


# --------------------------------------------------- 1b. le noeud, en profondeur
async def node_check() -> None:
    title("1b) Nœud : pool de connexions, lot de lectures, WebSocket, clé masquée")
    settings = config.all()

    # --- the key must never be printable -------------------------------------
    masked = solana._mask(solana.rpc_url())
    print(f"  URL masquée                          : {masked}")
    check("aucune clé API en clair dans l'URL affichée",
          "***" in masked or "?" not in masked, masked)
    check("le masquage ne produit pas de %2A illisible", "%2A" not in masked)
    check("une URL sans secret reste intacte",
          solana._mask("https://api.mainnet-beta.solana.com")
          == "https://api.mainnet-beta.solana.com")

    # --- the a key placeholder can come from the environment ------------------
    os.environ["HELIUS_API_KEY"] = "cle-de-controle"
    try:
        check("le placeholder {api_key} est résolu depuis l'environnement",
              solana._expand_keys("https://x.example/?api-key={api_key}")
              .endswith("cle-de-controle"))
    finally:
        os.environ.pop("HELIUS_API_KEY", None)

    # --- one client, reused ---------------------------------------------------
    check("les connexions HTTP sont mutualisées (même client)",
          net.client() is net.client())
    check("le cœur TLS valide toujours les certificats",
          net.ssl_context().verify_mode is not None, f"truststore={net.USING_OS_STORE}")

    # --- failover policy ------------------------------------------------------
    # A simulation broadcasts nothing, so it must fail over. It used to share
    # sendTransaction's exclusion, which silently removed the pre-flight on the
    # day the primary node went quiet.
    check("une simulation PEUT basculer sur le nœud de secours",
          len(solana._endpoints("simulateTransaction")) == 2)
    check("une diffusion ne bascule JAMAIS (pas de double envoi)",
          len(solana._endpoints("sendTransaction")) == 1)

    original = solana._call_sync
    attempts: list[str] = []

    def dying(url: str, method: str, params: list, timeout: float):
        attempts.append(method)
        raise solana.SolanaError(f"nœud mort: {solana._mask(url)}")

    solana._call_sync = dying  # type: ignore[assignment]
    try:
        attempts.clear()
        try:
            await solana.simulate_signed("pas-une-transaction")
        except solana.SolanaError:
            pass
        check("une simulation est bien retentée (2 tentatives)", len(attempts) == 2,
              f"{len(attempts)} tentative(s)")
        attempts.clear()
        try:
            await solana.rpc("sendTransaction", ["x", {}])
        except solana.SolanaError:
            pass
        check("une diffusion n'est tentée qu'une fois", len(attempts) == 1,
              f"{len(attempts)} tentative(s)")
    finally:
        solana._call_sync = original  # type: ignore[assignment]

    # --- a dead primary must be recorded, not hidden --------------------------
    saved = settings.get("solana_rpc_url")
    config._settings["solana_rpc_url"] = "http://127.0.0.1:9/mort"
    try:
        slot = await solana.rpc("getSlot", [])
        check("une lecture aboutit malgré un nœud primaire mort",
              isinstance(slot, int) and slot > 0, f"slot={slot}")
        dead = [host for host, entry in solana.rpc_health()["endpoints"].items()
                if entry["fail"] and not entry["ok"]]
        check("la panne du primaire est enregistrée et affichable",
              any("127.0.0.1" in host for host in dead), f"en échec: {dead}")
    finally:
        config._settings["solana_rpc_url"] = saved

    # --- a batch: several reads, one round trip -------------------------------
    # Timed on the best of three of each rather than on a single pair: one slow
    # answer from the node (measured at 1.7 s where the median is 0.3 s, with no
    # failure recorded by the node health) would otherwise turn a true statement
    # into a red check. The minimum is the jitter-free cost of the operation, and
    # the test stays exactly as strict — three round trips can never beat one,
    # whatever the sampling.
    wallet = solana.wallet_address()
    single_samples: list[float] = []
    one = 0
    for _ in range(3):
        start = time.perf_counter()
        one = await solana.rpc("getSlot", [])
        single_samples.append((time.perf_counter() - start) * 1000)
        await asyncio.sleep(0.2)
    batch_samples: list[float] = []
    for _ in range(3):
        start = time.perf_counter()
        batch = await solana.rpc_batch([
            ("getSlot", []),
            ("getBalance", [wallet]),
            ("getLatestBlockhash", [{"commitment": "confirmed"}]),
        ])
        batch_samples.append((time.perf_counter() - start) * 1000)
        await asyncio.sleep(0.2)
    single_ms = min(single_samples)
    batch_ms = min(batch_samples)
    check("un lot de 3 lectures rend 3 résultats dans l'ordre",
          isinstance(batch, list) and len(batch) == 3 and isinstance(batch[0], int))
    check("le lot donne la même réponse que l'appel isolé", batch[0] >= one,
          f"lot slot={batch[0]} / isolé slot={one}")
    check("le lot coûte le prix d'une lecture (un seul aller-retour)",
          batch_ms < single_ms * 2.5,
          f"meilleur lot {batch_ms:.0f} ms vs meilleure lecture {single_ms:.0f} ms "
          f"(lots {[round(v) for v in batch_samples]} ms / "
          f"lectures {[round(v) for v in single_samples]} ms)")
    try:
        await solana.rpc_batch([("getSlot", []), ("sendTransaction", ["x", {}])])
        check("un lot contenant une diffusion est refusé", False, "il a été accepté")
    except solana.SolanaError:
        check("un lot contenant une diffusion est refusé", True)

    # --- the socket: a live proof, and a soft failure -------------------------
    await solana.start_background()
    state: dict = {}
    for _ in range(30):
        await asyncio.sleep(0.5)
        state = solana.rpc_health()["websocket"]
        if state.get("connected") and state.get("last_slot"):
            break
    check("le WebSocket se connecte et reçoit des slots",
          bool(state.get("connected")) and bool(state.get("last_slot")),
          f"slot={state.get('last_slot')} inactif depuis {state.get('idle_ms')} ms")
    started = time.perf_counter()
    soft = await solana._ws.wait("5" * 86 + "1111", timeout=3.0)
    elapsed = time.perf_counter() - started
    check("une signature inconnue rend la main (repli sondage, jamais un blocage)",
          soft is None and elapsed < 5.0, f"{elapsed:.1f} s -> {soft!r}")
    await solana.stop_background()
    check("l'arrêt du WebSocket ne lève plus d'exception", True)

    # --- the priority-fee advisory --------------------------------------------
    levels = await solana.cached_priority_fee_levels()
    advice = solana.priority_fee_advice(levels, settings)
    print(f"  Niveaux du marché (µLam/CU)          : {levels}")
    print(f"  Plafond configuré                    : {advice['cap_lamports']:,} lamports "
          f"= {advice['cap_price_microlamports_per_cu']:,} µLam/CU")
    print(f"  Total impliqué par niveau            : {advice['implied_lamports']}")
    check("l'avis sur les frais de priorité est calculé",
          advice["state"] in ("au-dessus", "en-dessous", "indisponible"),
          f"état={advice['state']}")
    check("le plafond est converti dans l'unité du marché (pas de comparaison brute)",
          advice["cu_budget"] == solana.DEFAULT_CU_BUDGET and advice["cap_lamports"] ==
          int(round(float(settings["priority_fee"]) * solana.LAMPORTS_PER_SOL)),
          f"budget CU={advice['cu_budget']:,}")
    check("la santé du nœud est exposable sans secret",
          all("***" in host or "key" not in host
              for host in solana.rpc_health()["endpoints"]))


# ------------------------------------------------------------------ 2. settings
title("2) Paramètres : aucune clé manquante")
defaults = set(config.DEFAULT_SETTINGS)
used: set[str] = set()
for name in ("analyzer.py", "trader.py", "bot.py", "alertfilter.py"):
    source = (ROOT / "backend" / name).read_text(encoding="utf-8")
    used |= set(re.findall(r'settings\[\s*"([a-z_]+)"\s*\]', source))
    used |= set(re.findall(r'settings\.get\(\s*"([a-z_]+)"', source))
used |= set(re.findall(r'config\.get\(\s*"([a-z_]+)"',
                       "".join((ROOT / "backend" / n).read_text(encoding="utf-8")
                               for n in ("trader.py", "main.py"))))
missing = sorted(used - defaults)
check(f"{len(used)} paramètres référencés existent bien", not missing,
      f"manquants: {missing}" if missing else "")

settings = config.all()
print(f"  moteur={settings['execution_backend']} | montant={settings['trade_amount_sol']} SOL | "
      f"TP={settings['tp_percent']}% SL={settings['sl_percent']}% "
      f"trail={settings['trailing_percent']}% ({'on' if settings['trailing_enabled'] else 'off'})")
print(f"  slippage={settings['slippage']}% | priority={settings['priority_fee']} SOL | "
      f"frais max={settings['max_fee_percent']}% | "
      f"positions max={settings['max_concurrent_positions']} | "
      f"trading réel={settings['live_trading']} | auto={settings['auto_snipe']}")


def _validate(patch: dict) -> dict | None:
    try:
        return config.validate(patch)
    except ValueError:
        return None


# A malformed value must be refused, never coerced: an accepted `null` once
# turned the stop loss off without a word.
INVALID = [
    ("champ numérique vidé", {"sl_percent": None}),
    ("nombre négatif", {"sl_percent": -5}),
    ("texte dans un nombre", {"tp_percent": "abc"}),
    ("champ vide", {"trade_amount_sol": ""}),
    ("texte dans un booléen", {"alert_prefilter": "peut-être"}),
    ("booléen nul", {"trailing_enabled": None}),
    ("plafond de frais vidé", {"max_fee_percent": None}),
    ("plafond de frais hors bornes", {"max_fee_percent": 101}),
    ("fenêtre inexistante", {"buyer_seller_window": "7d"}),
    ("moteur inexistant", {"execution_backend": "jupiter-x"}),
    ("RPC sans schéma", {"solana_rpc_url": "solana.leorpc.com"}),
]
accepted = [name for name, patch in INVALID if _validate(patch) is not None]
check(f"{len(INVALID)} valeurs invalides refusées", not accepted, f"acceptées: {accepted}")

VALID = [
    ("fenêtre 6h", {"buyer_seller_window": "6h"}, {"buyer_seller_window": "6h"}),
    ("booléen « oui »", {"alert_prefilter": "oui"}, {"alert_prefilter": True}),
    ("booléen « off »", {"trailing_enabled": "off"}, {"trailing_enabled": False}),
    ("canal au format lien", {"channels": ["https://t.me/gmgnsignals"]},
     {"channels": ["gmgnsignals"]}),
    ("plafond de frais à zéro (règle désactivée)", {"max_fee_percent": 0},
     {"max_fee_percent": 0.0}),
]
wrong = [name for name, patch, want in VALID if _validate(patch) != want]
check(f"{len(VALID)} valeurs valides normalisées", not wrong, f"incorrectes: {wrong}")


# ------------------------------------------------------------------ 3. filtres
title("3) Filtres — rapports réels enregistrés")
calls_file = ROOT / "data" / "calls.json"
calls = json.loads(calls_file.read_text(encoding="utf-8")) if calls_file.exists() else []
full = [c for c in calls if c.get("checks") and not c.get("prefiltered")]
prefiltered = [c for c in calls if c.get("prefiltered")]
print(f"  {len(calls)} calls enregistrés | {len(full)} analyses GMGN | "
      f"{len(prefiltered)} rejets par pré-filtre (0 appel GMGN)")

expected_ids = {"top10", "mc_vs_volume", "buyers", "links", "social_engagement",
                "kol", "smart", "market_cap", "age"}
ceiling = _f(settings["max_market_cap"])
for report in full[:3]:
    ids = {c["id"] for c in report["checks"]}
    failed = [c["id"] for c in report["checks"] if not c["passed"]]
    print(f"\n  {report.get('symbol', '?'):12s} {report['address'][:10]}… "
          f"=> {report['decision']}")
    for item in report["checks"]:
        print(f"      {item['id']:18s} {'pass' if item['passed'] else 'REJET':5s}  "
              f"{item['detail'][:62]}")
        if item["id"] == "market_cap":
            check("    plafond ATH appliqué",
                  _f(report.get("ath_market_cap")) > ceiling or item["passed"],
                  f"ATH ${_f(report.get('ath_market_cap')):,.0f} vs plafond ${ceiling:,.0f}")
    check(f"  rapport {report.get('symbol', '?')} : 9 critères", expected_ids.issubset(ids),
          f"manquants: {sorted(expected_ids - ids)}")
    check(f"  décision {report.get('symbol', '?')} cohérente",
          (report["decision"] == "rejected") == bool(failed), f"échoués={failed}")

if prefiltered:
    check("pré-filtre : rejets gratuits bien marqués",
          all(p.get("execution", {}).get("executed") is False for p in prefiltered))
    check("pré-filtre : jamais un achat", not any(p.get("decision") == "sniped"
                                                  for p in prefiltered))


# ------------------------------------------------------- 3b. coût des frais
title("3b) Frais — une transaction doit pouvoir payer ses propres frais")

from backend import fees  # noqa: E402

_prio = _f(settings.get("priority_fee"))
_amt = _f(settings.get("trade_amount_sol"))
_gate = fees.fee_gate(settings)
print(f"  réglé : {_amt:g} SOL par trade, priority fee {_prio:g} SOL, plafond "
      f"{_f(settings.get('max_fee_percent')):g} %")
print(f"  estimé : {_gate['estimated_sol']:.6f} SOL par aller-retour = "
      f"{_gate['share_percent']:.2f} % du trade -> {'accepté' if _gate['passed'] else 'REFUSÉ'}")

_f0 = fees.fee_gate({"priority_fee": 0.0, "trade_amount_sol": 1.0, "max_fee_percent": 3.0})
check("  frais = base x3 + priority x2 (aucun montant oublié)",
      abs(_f0["estimated_sol"] - 0.000015) < 1e-12, f"{_f0['estimated_sol']:.9f} SOL")
for prio, amount, cap, want in (
    (0.0001, 0.001, 3.0, False),
    (0.00002, 0.001, 3.0, False),
    (0.00002, 0.002, 3.0, True),
    (0.00002, 0.01, 3.0, True),
    (0.0001, 0.001, 0.0, True),     # plafond 0 = règle désactivée
    (0.0, 0.001, 3.0, True),
    (0.0, 0.00001, 3.0, False),     # la base seule dépasse déjà
):
    got = fees.fee_gate({"priority_fee": prio, "trade_amount_sol": amount,
                         "max_fee_percent": cap})["passed"]
    check(f"  priority {prio:g} · {amount:g} SOL · plafond {cap:g} % -> "
          f"{'accepté' if want else 'refusé'}", got == want)

shares = [fees.fee_share_percent({"priority_fee": 0.00002, "trade_amount_sol": a})
          for a in (0.001, 0.002, 0.005, 0.01, 0.05)]
check("  la part des frais baisse quand le montant augmente",
      all(shares[i] > shares[i + 1] for i in range(len(shares) - 1)),
      " < ".join(f"{s:.2f}%" for s in shares))

_c = fees.fee_check({"priority_fee": 0.0001, "trade_amount_sol": 0.001, "max_fee_percent": 3.0})
check("  le rapport porte le critère « fees » comme les autres",
      _c["id"] == "fees" and _c["passed"] is False and _c["threshold"] == 3.0,
      _c["detail"])
_c2 = fees.fee_check({"priority_fee": 0.0001, "trade_amount_sol": 0.001, "max_fee_percent": 0.0})
check("  règle désactivée : le critère passe et le dit", _c2["passed"] is True,
      _c2["label"])
for bad in ({"max_fee_percent": None}, {"max_fee_percent": ""}, {"max_fee_percent": -1},
            {"max_fee_percent": 101}, {"max_fee_percent": "abc"}):
    check(f"  plafond invalide refusé : {bad['max_fee_percent']!r}",
          _validate(bad) is None)

# Ce que la règle aurait dit des transactions réellement passées. Les positions
# papier sont exclues : elles portent un montant nominal et n'ont jamais payé de
# frais sur la chaîne — les compter ici diluerait la seule fuite démontrée.
positions_file = ROOT / "data" / "positions.json"
if positions_file.exists():
    real = json.loads(positions_file.read_text(encoding="utf-8"))
    real = [p for p in real if p.get("status") == "closed" and not p.get("paper")
            and _f(p.get("amount_sol")) > 0]
    cap = _f(config.all().get("max_fee_percent"))
    if real and cap > 0:
        over = [p for p in real
                if _f(p.get("fees_sol")) / _f(p.get("amount_sol")) * 100 > cap]
        shares_real = sorted(_f(p.get("fees_sol")) / _f(p.get("amount_sol")) * 100
                             for p in real)
        print(f"  historique : {len(real)} trades réels, frais "
              f"{shares_real[0]:.2f} % à {shares_real[-1]:.2f} % "
              f"(médiane {shares_real[len(shares_real) // 2]:.2f} %)")
        check(f"  plafond de {cap:g} % : {len(over)}/{len(real)} trades réels "
              f"passés auraient été refusés",
              len(over) >= (len(real) * 2) // 3,
              "frais réels mesurés sur la chaîne, par transaction")


# ------------------------------------------------------------------ 4. filtre live
title("4) Filtres — analyse fraîche en direct (lecture seule)")


async def live_filter_check() -> None:
    from backend.analyzer import analyze

    address = next((c["address"] for c in calls
                    if c.get("address", "").endswith("pump")), None)
    if not address:
        check("adresse de test disponible", False)
        return
    await clock.sync()
    report = await analyze(address, source="selfcheck", message="", channel="selfcheck")
    failed = [c["id"] for c in report["checks"] if not c["passed"]]
    print(f"  token : {report['symbol']} — MC ${_f(report['market_cap']):,.0f} "
          f"(ATH ${_f(report['ath_market_cap']):,.0f}) | âge {_f(report['age_minutes']):.1f} min")
    print(f"  top10 {_f(report['top10_pct']):.1f}% | achats {report['buyers']} / "
          f"ventes {report['sellers']} | site={report['has_website']} "
          f"réseaux={report['has_social']} KOL={report['kols']} "
          f"smart={report['smart_wallets']} honeypot={report['is_honeypot']}")
    print(f"  => {report['decision']} | échoués={failed or 'aucun'}")
    check("l'analyse renvoie les 10 critères", len(report["checks"]) >= 10,
          f"{len(report['checks'])} critères")
    _fee = next((c for c in report["checks"] if c["id"] == "fees"), None)
    check("le critère de frais est présent et chiffré", bool(_fee) and _fee["detail"],
          _fee["detail"] if _fee else "absent")
    check("ATH >= market cap courante",
          _f(report["ath_market_cap"]) >= _f(report["market_cap"]) - 1,
          f"ATH={_f(report['ath_market_cap']):.0f} MC={_f(report['market_cap']):.0f}")
    check("décision = tous les critères verts",
          (report["decision"] == "sniped") == (not failed), f"échoués={failed}")


# ------------------------------------------------------------------ 5. sorties
title("5) TP / SL / trailing stop")
exit_decision = trader_mod.exit_decision
tp, sl, trail = 50.0, 20.0, 10.0
scenarios = [
    ("TP atteint (+50%)", 150.0, 150.0, 50.0, tp, sl, trail, True, "take_profit"),
    ("TP dépassé (+80%)", 180.0, 180.0, 80.0, tp, sl, trail, True, "take_profit"),
    ("SL atteint (-20%)", 80.0, 100.0, -20.0, tp, sl, trail, True, "stop_loss"),
    ("SL dépassé (-45%)", 55.0, 100.0, -45.0, tp, sl, trail, True, "stop_loss"),
    ("rien (+4%)", 104.0, 104.0, 4.0, tp, sl, trail, False, ""),
    ("rien (+20%, trailing inactif)", 120.0, 120.0, 20.0, tp, sl, trail, False, ""),
    ("trailing : pic 200 -> 180", 180.0, 200.0, 80.0, 999.0, sl, trail, True, "trailing_stop"),
    ("trailing pas touché (185)", 185.0, 200.0, 85.0, 999.0, sl, trail, True, ""),
    ("trailing : pic 104 -> 93,6", 93.6, 104.0, -6.4, tp, sl, trail, True, "trailing_stop"),
    ("SL prioritaire sur trailing", 70.0, 110.0, -30.0, tp, sl, trail, True, "stop_loss"),
    ("seuil 0 = règle désactivée", 100.0, 100.0, 0.0, 0.0, 0.0, trail, False, ""),
]
for label, price, peak, pnl, t, s, tr, active, expected in scenarios:
    got = exit_decision(price=price, peak=peak, pnl_pct=pnl, tp_percent=t,
                        sl_percent=s, trailing_percent=tr, trailing_active=active)
    check(f"  {label}", got == expected, f"attendu={expected or 'aucune'} obtenu={got or 'aucune'}")

print("\n  Montants de vente (aucun arrondi silencieux vers zéro) :")
sell_units = trader_mod.sell_amount_units
amounts = [
    ("100% de 1 000 000", 1_000_000, 100.0, 1_000_000),
    ("50% de 1 000 000", 1_000_000, 50.0, 500_000),
    ("1% de 1 000 000", 1_000_000, 1.0, 10_000),
    ("0,01% de 1 000 000", 1_000_000, 0.01, 100),
    ("1% de 50 (arrondit à 0 -> refus)", 50, 1.0, 0),
    ("0% (refus)", 1_000_000, 0.0, 0),
    ("0,0001% de 1 000 000 (1 unité)", 1_000_000, 0.0001, 1),
    ("0,00001% de 1 000 000 (arrondit à 0 -> refus)", 1_000_000, 0.00001, 0),
    ("100% de 3 (poussière)", 3, 100.0, 3),
    ("120% plafonné au solde", 1_000_000, 120.0, 1_000_000),
    ("solde nul", 0, 100.0, 0),
]
for label, raw, pct, expected in amounts:
    got = sell_units(raw, pct)
    check(f"  {label}", got == expected, f"attendu={expected} obtenu={got}")


# ---------------------------------------------- 5c. PnL net de tous les frais
print("\n  PnL réalisé, tous frais de transaction inclus :")
realized_pnl = trader_mod.realized_pnl


def rp(**kw) -> dict:
    base = dict(proceeds_sol=0.0, sell_fees=0.0, committed_sol=0.0, buy_fee=0.0,
                close_fee=0.0, sol_price=100.0, cost_usd=0.0, tokens=0.0)
    base.update(kw)
    return realized_pnl(**base)


r = rp(proceeds_sol=1.5, sell_fees=0.002, committed_sol=1.0, buy_fee=0.001,
       close_fee=0.0005, cost_usd=100.0, tokens=1_000_000)
check("  frais additionnés (achat + vente + clôture)", abs(r["fees_sol"] - 0.0035) < 1e-12,
      f"frais={r['fees_sol']:.9f}")
check("  net = (produit + frais de vente) − engagé − frais",
      abs(r["net_sol"] - 0.4985) < 1e-12, f"net={r['net_sol']:.9f} SOL")
check("  PnL $ = net SOL × prix SOL", abs(r["pnl_usd"] - 49.85) < 1e-9, f"${r['pnl_usd']:.6f}")
check("  PnL % sur le capital engagé", abs(r["pnl_percent"] - 49.85) < 1e-9,
      f"{r['pnl_percent']:.4f}%")
check("  prix de sortie hors frais (comparable à l'entrée)",
      abs(r["exit_price"] - 0.0001502) < 1e-15, f"{r['exit_price']:.10f}")

# Le frais de vente est déjà déduit des SOL reçus : il ne doit pas l'être deux fois.
a = rp(proceeds_sol=0.966479, sell_fees=0.0, committed_sol=1.0, cost_usd=100.0)["net_sol"]
b = rp(proceeds_sol=0.966479, sell_fees=0.002, committed_sol=1.0, cost_usd=100.0)["net_sol"]
check("  frais de vente comptés une seule fois", abs(a - b) < 1e-12, f"{a:.9f} vs {b:.9f}")

# ZUCK, chiffres on-chain réels : 0,001 SOL engagés, 0,000966479 SOL reçus.
z = rp(proceeds_sol=0.000966479, sell_fees=0.0, committed_sol=0.001, buy_fee=0.0,
       close_fee=0.0, sol_price=103.26, cost_usd=0.1032, tokens=33857.467238)
check("  ZUCK sans frais : −3,35 % (ancien calcul)", abs(z["pnl_percent"] + 3.354) < 0.02,
      f"{z['pnl_percent']:.3f}%")
z2 = rp(proceeds_sol=0.000966479, sell_fees=0.000009648, committed_sol=0.001,
        buy_fee=0.000009648, close_fee=0.000005, sol_price=103.26, cost_usd=0.1032,
        tokens=33857.467238)
check("  ZUCK avec tous les frais : ≈ −4,8 %", z2["pnl_percent"] < z["pnl_percent"],
      f"{z['pnl_percent']:.3f}% -> {z2['pnl_percent']:.3f}%")
check("  détail des frais ZUCK", abs(z2["fees_sol"] - 0.000024296) < 1e-12,
      f"frais={z2['fees_sol']:.9f} SOL ({z2['pnl_usd']:+.4f} $)")

# Un gain de marché peut être effacé par les frais : le PnL doit le montrer.
g = rp(proceeds_sol=0.00102, sell_fees=0.00002, committed_sol=0.001, buy_fee=0.00002,
       close_fee=0.000005, cost_usd=0.1032)
check("  un petit gain de marché devient une perte nette", g["net_sol"] < 0,
      f"net={g['net_sol']:.9f} SOL")


# --------------------------------------------------- 5b. sortie contrôlée
title("5b) Sortie contrôlée par le prix exécutable (devis réel)")


async def exit_price_check() -> None:
    """An exit decided on a display price must be confirmed by a real quote."""
    engine = trader_mod.trader
    original = engine._executable_price

    def fake_quote(real: float):
        async def price_of(_mint: str) -> float:
            return real
        return price_of

    position = {
        "address": "So11111111111111111111111111111111111111112",
        "entry_price": 1.0e-05, "tp_percent": 15.0, "sl_percent": 10.0,
        "trailing_percent": 10.0, "trailing_enabled": False, "trailing_active": False,
    }
    display = 6.0e-06  # the display price claims −40%: a stop loss
    cases = [
        ("prix exécutable +60% -> take profit", 1.6e-05, "stop_loss", "take_profit"),
        ("prix exécutable au-dessus du stop -> annulée", 1.02e-05, "stop_loss", ""),
        ("devis indisponible -> décision conservée", 0.0, "stop_loss", "stop_loss"),
        ("prix cohérents -> décision conservée", 6.1e-06, "stop_loss", "stop_loss"),
    ]
    for label, real, reason, expected in cases:
        engine._executable_price = fake_quote(real)
        got = await engine._confirm_exit(position, reason, display, display, 1.0e-05)
        check(f"  {label}", got == expected, f"attendu={expected!r} obtenu={got!r}")

    # A take-profit must be executable: ZUCK's display price claimed +16.3% while
    # a sale really got −3.3%, and the old ±25% tolerance let it through — the
    # position closed at a loss still labelled "take_profit".
    entry = 3.0486627742830932e-06
    tp_position = {**position, "entry_price": entry, "tp_percent": 15.0}
    tp_display = 3.547e-06  # +16.3% on the display: a take profit
    tp_cases = [
        ("exécutable +16,0% -> take profit confirmé", 3.536e-06, "take_profit"),
        ("exécutable -3,3% -> take profit annulé (ZUCK)", 2.947e-06, ""),
        ("exécutable -12,1% -> stop-loss retenu", 2.68e-06, "stop_loss"),
        ("devis indisponible -> décision conservée", 0.0, "take_profit"),
    ]
    for label, real, expected in tp_cases:
        engine._executable_price = fake_quote(real)
        got = await engine._confirm_exit(tp_position, "take_profit", tp_display, tp_display, entry)
        check(f"  {label}", got == expected, f"attendu={expected!r} obtenu={got!r}")
    engine._executable_price = original


# ------------------------------------------------------------------ 6. exécution
title("6) Jupiter + RPC configuré — signature locale et simulation")
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


async def execution_check() -> None:
    import inspect

    wallet = solana.wallet_address()
    quote = await jupiter.quote(input_mint=SOL_MINT, output_mint=USDC_MINT,
                                amount=1_000_000,
                                slippage_bps=int(settings["slippage"]) * 100)
    out = _f(quote.get("outAmount"))
    check("devis Jupiter obtenu", out > 0,
          f"0,001 SOL -> {out / 1e6:.6f} USDC")
    built = await jupiter.swap_transaction(quote_response=quote, user_public_key=wallet,
                                           max_priority_lamports=100_000)
    signed = solana.sign_only(built["swapTransaction"])
    transaction = VersionedTransaction.from_bytes(base64.b64decode(signed))
    check("signature locale valide", transaction.verify_with_results() == [True])
    check("payeur = wallet configuré", str(transaction.message.account_keys[0]) == wallet)

    # A route Jupiter returns can be unexecutable at that very instant. Measured
    # here: a « Whirlpool > Manifest » route failed simulation with
    # InstructionError [6, Custom 1] on the configured node *and* on a second
    # one, while three other routes for the same swap went through. So the bot
    # must simulate before broadcasting — otherwise it pays the base fee and the
    # priority fee (up to 105 000 lamports, 10 % of a 0,001 SOL trade) for a swap
    # that never happens — and take a fresh quote when a route is refused.
    source = inspect.getsource(trader_mod.Trader._jupiter_swap)
    check("le bot simule avant d'envoyer (aucun frais payé pour rien)",
          "simulate_signed" in source)
    check("une route refusée est réessayée avec un devis neuf",
          "ROUTE_ATTEMPTS" in source and "swap_transaction" in source.split("for attempt")[-1])
    check("l'échec de toutes les routes n'envoie rien",
          "transaction non envoyée" in source)

    refused: list = []
    route = "?"
    simulation: dict = {}
    for attempt in range(1, trader_mod.ROUTE_ATTEMPTS + 1):
        if attempt > 1:
            quote = await jupiter.quote(input_mint=SOL_MINT, output_mint=USDC_MINT,
                                        amount=1_000_000,
                                        slippage_bps=int(settings["slippage"]) * 100)
            built = await jupiter.swap_transaction(quote_response=quote,
                                                   user_public_key=wallet,
                                                   max_priority_lamports=100_000)
            signed = solana.sign_only(built["swapTransaction"])
        route = " > ".join((step.get("swapInfo") or {}).get("label", "?")
                           for step in (quote.get("routePlan") or [])) or "?"
        simulation = await solana.simulate_signed(signed, sig_verify=True)
        if simulation.get("err") is None:
            break
        refused.append(simulation.get("err"))
        await asyncio.sleep(0.35)
    check("le RPC accepte la transaction signée", simulation.get("err") is None,
          f"route={route} | essais={attempt} | err={simulation.get('err')} | "
          f"unités={simulation.get('unitsConsumed')}")
    if refused:
        print(f"         {len(refused)} route(s) refusée(s) par la simulation, "
              f"remplacée(s) par un devis neuf : {refused}")


# ------------------------------------------------- 7. suivi de prix (Dexscreener)
title("7) Suivi de prix continu — un seul poller Dexscreener partage")

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"


async def route_retry_check() -> None:
    """A refused route is never broadcast, and a good one still is.

    No network: the quote, the build, the simulation and the send are all
    replaced by doubles, so what is being tested is only the money rule —
    nothing is paid for a transaction that cannot execute.
    """
    engine = trader_mod.trader
    saved_quote = jupiter.quote
    saved_build = jupiter.swap_transaction
    saved_sim = solana.simulate_signed
    saved_send = solana.sign_and_send
    saved_log = trader_mod.store.log

    state = {"quotes": 0, "sent": 0, "sims": 0, "fast_sleep": 0}
    verdicts = [{"err": {"InstructionError": [6, {"Custom": 1}]}}, {"err": None}]

    async def fake_quote(**kw):
        state["quotes"] += 1
        return {"outAmount": "1000000", "routePlan": []}

    async def fake_build(**kw):
        return {"swapTransaction": "AAA"}

    async def fake_sim(signed, *, sig_verify=True):
        state["sims"] += 1
        return verdicts[min(state["sims"] - 1, len(verdicts) - 1)]

    async def fake_send(tx, **kw):
        state["sent"] += 1
        return "Sig" + "1" * 84

    def fake_log(*a, **kw):
        return None

    async def fake_confirm(*a, **kw):
        raise solana.SolanaError("hors ligne")

    async def fake_balances(*a, **kw):
        return (1.0, 6, 1_000_000)

    saved_confirm = solana.confirm
    saved_bal = solana.token_ui_balance
    saved_sign_only = solana.sign_only
    jupiter.quote = fake_quote
    jupiter.swap_transaction = fake_build
    solana.sign_only = lambda tx: tx
    solana.simulate_signed = fake_sim
    solana.sign_and_send = fake_send
    solana.confirm = fake_confirm
    solana.token_ui_balance = fake_balances
    trader_mod.store.log = fake_log
    try:
        # 1) first route refused -> fresh quote -> second route accepted -> sent
        verdicts[:] = [{"err": {"InstructionError": [6, {"Custom": 1}]}}, {"err": None}]
        state.update(quotes=0, sent=0, sims=0)
        try:
            await engine._jupiter_swap(input_mint=SOL_MINT, output_mint=USDC_MINT,
                                       amount=1_000_000, side="buy")
            raised = ""
        except Exception as exc:  # noqa: BLE001
            raised = str(exc)[:70]
        check("route refusée -> devis neuf -> tâche envoyée", state["sent"] == 1
              and state["quotes"] == 2 and not raised,
              f"essais={state['quotes']} envois={state['sent']} erreur={raised!r}")

        # 2) every route refused -> nothing is ever sent
        verdicts[:] = [{"err": {"InstructionError": [6, {"Custom": 1}]}}]
        state.update(quotes=0, sent=0, sims=0)
        try:
            await engine._jupiter_swap(input_mint=SOL_MINT, output_mint=USDC_MINT,
                                       amount=1_000_000, side="buy")
            raised = ""
        except solana.SolanaError as exc:
            raised = str(exc)
        check("aucune route praticable -> rien n'est envoyé, aucun frais payé",
              state["sent"] == 0 and "aucun frais payé" in raised,
              f"essais={state['quotes']} envois={state['sent']} erreur={raised[:60]!r}")

        # 3) a simulation that cannot be read must not block a real trade
        async def broken_sim(signed, *, sig_verify=True):
            raise solana.SolanaError("nœud muet")

        solana.simulate_signed = broken_sim
        state.update(quotes=0, sent=0, sims=0)
        try:
            await engine._jupiter_swap(input_mint=SOL_MINT, output_mint=USDC_MINT,
                                       amount=1_000_000, side="buy")
            raised = ""
        except Exception as exc:  # noqa: BLE001
            raised = str(exc)[:70]
        check("simulation indisponible -> envoi direct (jamais bloquant)",
              state["sent"] == 1 and not raised,
              f"envois={state['sent']} erreur={raised!r}")
    finally:
        jupiter.quote = saved_quote
        jupiter.swap_transaction = saved_build
        solana.simulate_signed = saved_sim
        solana.sign_only = saved_sign_only
        solana.sign_and_send = saved_send
        solana.confirm = saved_confirm
        solana.token_ui_balance = saved_bal
        trader_mod.store.log = saved_log


# ------------------------------------------------------------------ 8. papier
async def paper_check() -> None:
    """Paper mode: everything real except the transaction.

    No network: the quote, the build, the simulation and the mint decimals are all
    replaced by doubles, and ``sign_and_send`` by a function that fails the test if
    it is ever called. What is verified here is therefore the property that
    matters — **no order is ever sent** — plus the fact that the virtual
    bookkeeping uses exactly the fee scale of the "fees" criterion, so a paper PnL
    can be compared with a real one.
    """
    from backend import fees as fees_mod
    from backend import state as state_mod
    from backend.state import store as st

    engine = trader_mod.trader
    saved = {
        "quote": jupiter.quote, "build": jupiter.swap_transaction,
        "sign_only": solana.sign_only, "simulate": solana.simulate_signed,
        "send": solana.sign_and_send, "decimals": solana.mint_decimals,
        "ui_balance": solana.token_ui_balance, "log": trader_mod.store.log,
        "price_for": engine._price_for,
    }
    sent: list[int] = []
    builds: list[int] = []
    sims: list[int] = []
    positions_before = list(st.positions)
    calls_before = list(st.calls)
    paper_before = config._settings.get("paper_trading")
    sol_before = st.runtime.get("sol_price")
    wallet_before = engine.wallet

    # A flat market, calibrated once: a token is worth 2e-08 SOL, quoted at
    # 150 $/SOL. The trade *size* is read from the live settings, because a size
    # baked in here silently breaks the moment the user changes their amount —
    # at 0,1 SOL a 0,002-hardcoded market turned a flat round trip into a −98 %
    # loss instead of exactly the fees.
    price_sol = 0.002 / 100_000.0
    size = float(config.all().get("trade_amount_sol") or 0.0)
    tokens_bought = size / price_sol

    async def fake_quote(*, input_mint, output_mint, amount, slippage_bps=3000, timeout=20.0):
        if output_mint == SOL_MINT:  # sell: raw token units -> lamports
            out = int(round(int(amount) / 1e6 * price_sol * 1e9))
        else:                        # buy: lamports -> raw token units
            out = int(round((int(amount) / 1e9) / price_sol * 1e6))
        return {"inAmount": str(amount), "outAmount": str(out), "priceImpactPct": "0.012",
                "routePlan": [{"swapInfo": {"label": "Pump.fun"}}]}

    async def fake_build(**kw):
        builds.append(1)
        return {"swapTransaction": "PAPIER"}

    async def fake_sim(signed, *, sig_verify=True):
        sims.append(1)
        return {"err": None, "unitsConsumed": 1000}

    async def no_send(*a, **kw):
        sent.append(1)
        raise AssertionError("le mode papier ne doit jamais envoyer de transaction")

    async def fake_decimals(mint):
        return 6

    async def fake_ui_balance(wallet, mint):
        return (1.0, 6, 1_000_000)

    async def fake_price_for(address):
        return 0.0

    def fake_log(*a, **kw):
        return None

    jupiter.quote = fake_quote
    jupiter.swap_transaction = fake_build
    solana.sign_only = lambda tx: tx
    solana.simulate_signed = fake_sim
    solana.sign_and_send = no_send
    solana.mint_decimals = fake_decimals
    solana.token_ui_balance = fake_ui_balance
    trader_mod.store.log = fake_log
    engine._price_for = fake_price_for
    engine.wallet = "Papier111111111111111111111111111111111111"
    st.runtime["sol_price"] = 150.0
    try:
        # The checks below count the *test's own* positions ("exactly one paper
        # position open"). A position left open by a live paper session is an
        # open position too and would be counted alongside it, so the suite
        # would report a failure that is really just a busy account — the kind
        # of false alarm that teaches a user to ignore the report. The book is
        # emptied here, inside the try, so the finally block always puts it back
        # exactly as the call list is.
        st.positions.clear()
        check("mode papier désactivé par défaut", config.DEFAULT_SETTINGS["paper_trading"] is False)
        check("l'interrupteur papier est un booléen validé",
              _validate({"paper_trading": True}) == {"paper_trading": True}
              and _validate({"paper_trading": "oui"}) == {"paper_trading": True}
              and _validate({"paper_trading": "peut-être"}) is None
              and _validate({"paper_trading": None}) is None)

        # A simulation can fail for the wallet's balance instead of the route's own
        # validity. Confusing the two would either block every paper test on an
        # empty wallet or hide a route that truly cannot execute.
        check("solde insuffisant distingué d'une route refusée",
              trader_mod.simulation_refusal_is_funding(
                  {"err": {"InstructionError": [0, {"Custom": 1}]},
                   "logs": ["Transfer: insufficient lamports 0, need 2000000"]}) is True
              and trader_mod.simulation_refusal_is_funding(
                  {"err": {"InstructionError": [6, {"Custom": 1}]},
                   "logs": ["Program log: WhirlpoolError"]}) is False
              and trader_mod.simulation_refusal_is_funding({}) is False)

        attempts = {"quote": 0}

        async def counting_quote(**kw):
            attempts["quote"] += 1
            return await fake_quote(**kw)

        async def poor_sim(signed, *, sig_verify=True):
            return {"err": {"InstructionError": [0, {"Custom": 1}]},
                    "logs": ["Transfer: insufficient lamports 0, need 2000000"]}

        jupiter.quote = counting_quote
        solana.simulate_signed = poor_sim
        raised = ""
        try:
            await engine._jupiter_swap(input_mint=SOL_MINT, output_mint=USDC_MINT,
                                       amount=1_000_000, side="buy")
        except Exception as exc:  # noqa: BLE001
            raised = str(exc)
        check("solde insuffisant: cause réelle, un seul essai, rien d'envoyé",
              "Fonds insuffisants" in raised and attempts["quote"] == 1 and sent == [],
              f"essais={attempts['quote']} envois={len(sent)} erreur={raised[:50]!r}")
        jupiter.quote = fake_quote
        solana.simulate_signed = fake_sim

        settings = config.all()
        check("achat + vente + clôture = l'aller-retour du critère « frais »",
              abs((fees_mod.buy_fee_sol(settings) + fees_mod.sell_fee_sol(settings)
                   + fees_mod.close_fee_sol(settings))
                  - fees_mod.estimated_round_trip_sol(settings)) < 1e-15,
              f"{fees_mod.estimated_round_trip_sol(settings):.8f} SOL")

        # ---- the whole paper cycle ----------------------------------------
        config._settings["paper_trading"] = True
        buy = await engine.buy({"address": "Papier" + "1" * 38, "symbol": "PAPIER",
                                "name": "Test papier", "logo": "", "price_usd": 1e-9})
        check("achat papier exécuté sans aucun wallet ni solde",
              buy.get("executed") is True, f"raison={buy.get('reason')!r}")
        pos = st.get_position(buy.get("position_id") or "") or {}
        check("aucune transaction envoyée (achat)", sent == [], f"envois={len(sent)}")
        check("la position est marquée papier et garde sa provenance",
              bool(pos.get("paper")) is True and pos.get("paper_route") == "Pump.fun"
              and abs(float(pos.get("paper_price_impact") or 0) - 0.012) < 1e-9,
              f"route={pos.get('paper_route')} impact={pos.get('paper_price_impact')}")
        check("le remplissage est celui du devis réel",
              abs(float(pos.get("tokens") or 0) - tokens_bought) < 1e-6,
              f"tokens={pos.get('tokens')}")
        check("le prix d'entrée vient des SOL engagés (impact compris)",
              abs(float(pos.get("entry_price") or 0) - (size * 150.0) / tokens_bought) < 1e-12,
              f"entrée={pos.get('entry_price')}")
        check("les frais d'achat suivent le barème",
              abs(float(pos.get("buy_fee_sol") or 0) - fees_mod.buy_fee_sol(settings)) < 1e-15)
        check("l'achat papier VALIDE la route par une simulation réelle",
              len(sims) >= 1 and len(builds) >= 1 and bool(pos.get("paper_simulated")),
              f"constructions={len(builds)} simulations={len(sims)} "
              f"simulé={pos.get('paper_simulated')}")
        check("filtre par mode: les deux populations restent séparées",
              len(st.open_positions(paper=True)) == 1
              and all(not p.get("paper") for p in st.open_positions(paper=False))
              and len(st.open_positions()) == len(st.open_positions(paper=True))
              + len(st.open_positions(paper=False)),
              f"papier={len(st.open_positions(paper=True))} réel={len(st.open_positions(paper=False))}")

        # A flat round trip must lose exactly the modelled fees: nothing else is
        # charged, so any drift here would mean the paper scale is not the fee
        # scale, and a paper PnL would stop being comparable with a real one.
        builds_before, sims_before = len(builds), len(sims)
        await engine._paper_sell(pos, percent=100.0, reason="test")
        closed = st.get_position(pos["id"]) or {}
        fees_run = fees_mod.estimated_round_trip_sol(settings)
        check("vente papier: aucun ordre envoyé", sent == [], f"envois={len(sent)}")
        check("vente papier: cotée, jamais simulée (tokens virtuels)",
              len(builds) == builds_before and len(sims) == sims_before,
              f"constructions={len(builds) - builds_before} simulations={len(sims) - sims_before}")
        check("la position papier est fermée", closed.get("status") == "closed")
        check("aller-retour à plat = exactement les frais modélisés",
              abs(float(closed.get("fees_sol") or 0) - fees_run) < 1e-12
              and abs(float(closed.get("pnl_percent") or 0)
                      - (-fees_run / size * 100.0)) < 0.01,
              f"frais={closed.get('fees_sol')} pnl%={closed.get('pnl_percent')} "
              f"attendu={-fees_run / size * 100.0:.4f}%")
        check("la sortie garde la trace de la route",
              closed.get("exit_reason") == "test")

        # ---- the separation that makes the test trustworthy ----------------
        stats = st.recompute_stats()
        check("les stats réelles n'incluent aucune position papier",
              stats["trades"] == len([p for p in st.positions
                                      if p.get("status") == "closed" and not p.get("paper")]))
        check("le tableau de bord papier compte le trade",
              stats["paper"]["trades"] == len([p for p in st.positions
                                               if p.get("status") == "closed"
                                               and p.get("paper")])
              and stats["paper"]["trades"] >= 1,
              f"papier={stats['paper']['trades']} "
              f"fermés={len([p for p in st.positions if p.get('status') == 'closed' and p.get('paper')])}")

        # ---- a partial sell must not corrupt the final accounting ----------
        # Selling half the bag prorates what is *left*, but the final return and
        # the average exit price must still be measured against the whole
        # position — otherwise the loss is doubled and the exit price inflated.
        buy2 = await engine.buy({"address": "Papier" + "2" * 38, "symbol": "PARTIEL",
                                 "name": "Test partiel", "logo": "", "price_usd": 1e-9})
        p2 = st.get_position(buy2.get("position_id") or "") or {}
        check("second achat papier pour le test partiel", bool(p2.get("status")) is True,
              f"raison={buy2.get('reason')!r}")
        base_total = float(p2.get("cost_usd_total") or 0)
        await engine._paper_sell(p2, percent=50.0, reason="partiel")
        half = st.get_position(p2["id"]) or {}
        check("vente partielle: le capital total reste la référence",
              abs(float(half.get("cost_usd_total") or 0) - base_total) < 1e-9
              and abs(float(half.get("cost_usd") or 0) - base_total / 2) < 1e-6,
              f"total={half.get('cost_usd_total')} restant={half.get('cost_usd')}")
        check("vente partielle: les unités d'origine sont conservées",
              abs(float(half.get("tokens_bought") or 0) - tokens_bought) < 1e-6
              and abs(float(half.get("tokens") or 0) - tokens_bought / 2.0) < 1e-6,
              f"achetés={half.get('tokens_bought')} restants={half.get('tokens')}")
        check("vente partielle: le produit encaissé est cumulé",
              abs(float(half.get("proceeds_sol") or 0)
                  - (size / 2.0 - fees_mod.sell_fee_sol(settings))) < 1e-9,
              f"produit={half.get('proceeds_sol')}")
        await engine._paper_sell(half, percent=100.0, reason="partiel_fin")
        done = st.get_position(p2["id"]) or {}
        fees2 = (fees_mod.buy_fee_sol(settings) + 2 * fees_mod.sell_fee_sol(settings)
                 + fees_mod.close_fee_sol(settings))
        check("vente en deux fois: trois frais comptés, pourcentage sur le capital total",
              abs(float(done.get("fees_sol") or 0) - fees2) < 1e-12
              and abs(float(done.get("pnl_percent") or 0)
                      - float(done.get("pnl_usd") or 0) / (base_total or 1) * 100.0) < 0.01,
              f"frais={done.get('fees_sol')} attendu={fees2:.8f} "
              f"pnl%={done.get('pnl_percent')} pnl$={done.get('pnl_usd')}")

        # ---- a real bag must never be closed virtually ---------------------
        st.runtime["sol_price"] = 150.0
        real = {"id": "reel-test", "address": "Reel" + "1" * 40, "symbol": "REEL",
                "status": "open", "tokens": 1.0, "entry_price": 1e-9, "cost_usd": 0.3,
                "amount_sol": 0.002, "sol_committed": 0.002, "paper": False}
        st.positions.append(real)
        before = len(sent)
        await engine.sell(real, percent=100.0, reason="test")
        check("une position RÉELLE est vendue pour de vrai, même en mode papier",
              len(sent) == before + 1,
              f"envois={len(sent) - before} (0 = elle aurait été fermée en virtuel)")
        st.positions.remove(real)

        # ---- and a paper bag must always be closed virtually ---------------
        config._settings["paper_trading"] = False
        paper_pos = {**closed, "id": "papier-test", "status": "open", "tokens": tokens_bought,
                     "decimals": 6, "paper": True, "proceeds_sol": 0.0, "sell_fees_sol": 0.0}
        st.positions.append(paper_pos)
        before = len(sent)
        res = await engine.sell(paper_pos, percent=100.0, reason="test2")
        check("une position PAPIER est fermée en virtuel même mode papier éteint",
              len(sent) == before and res.get("executed") is True,
              f"envois={len(sent) - before} exécuté={res.get('executed')}")
    finally:
        jupiter.quote = saved["quote"]
        jupiter.swap_transaction = saved["build"]
        solana.sign_only = saved["sign_only"]
        solana.simulate_signed = saved["simulate"]
        solana.sign_and_send = saved["send"]
        solana.mint_decimals = saved["decimals"]
        solana.token_ui_balance = saved["ui_balance"]
        trader_mod.store.log = saved["log"]
        engine._price_for = saved["price_for"]
        engine.wallet = wallet_before
        st.runtime["sol_price"] = sol_before
        config._settings["paper_trading"] = paper_before
        st.positions[:] = positions_before
        st.calls.clear()
        st.calls.extend(calls_before)
        state_mod._write(state_mod.POSITIONS_FILE, list(st.positions))
        st.recompute_stats()


async def price_feed_check() -> None:
    from backend.pricefeed import price_feed
    from backend.state import store as st

    # A) the ranges a user can type: a feed faster than 0,5 s would hammer a
    # free API, and 0 recent calls is a legitimate way to switch that off.
    check("intervalle de suivi 0,5 s accepté", _validate({"price_feed_seconds": 0.5}) is not None)
    check("intervalle de suivi 60 s accepté", _validate({"price_feed_seconds": 60}) is not None)
    check("intervalle de suivi 0,2 s refusé", _validate({"price_feed_seconds": 0.2}) is None)
    check("intervalle de suivi 120 s refusé", _validate({"price_feed_seconds": 120}) is None)
    check("appels suivis = 0 accepté", _validate({"price_feed_calls": 0}) is not None)
    check("appels suivis = 20 accepté", _validate({"price_feed_calls": 20}) is not None)
    check("appels suivis = 21 refusé", _validate({"price_feed_calls": 21}) is None)
    check("suivi activé/désactivé (booléen)", _validate({"price_feed_enabled": False}) == {"price_feed_enabled": False})

    # B) one poller only: the position manager must read the feed, never poll
    # Dexscreener itself (that would double the request rate for the same data).
    import inspect
    source = inspect.getsource(trader_mod.Trader._manage_loop)
    check("le gestionnaire lit le flux (price_feed.quote)", "price_feed.quote" in source)
    check("le gestionnaire ne re-interroge pas Dexscreener",
          "prices_multi" not in source)

    # C) what gets followed, and what gets released
    saved_calls = list(store_calls_backup())
    # Open positions are followed first, before any recent call — so a book left
    # open by a live session would be tracked alongside the two test tokens and
    # every "exactly these addresses" check below would fail on a busy account.
    saved_positions = list(st.positions)
    st.positions.clear()
    try:
        set_store_calls([{"address": USDC}])
        config._settings["price_feed_calls"] = 1
        config._settings["price_feed_enabled"] = True
        price_feed._tracked.clear()
        price_feed._selected = ""

        wanted = price_feed._wanted()
        check("un call récent est suivi automatiquement", wanted == [USDC], f"suivis={wanted}")
        check("son étiquette est « call »", _kinds(price_feed, USDC) == ["call"],
              f"obtenu={_kinds(price_feed, USDC)}")

        price_feed.track(WSOL, kind="watch")
        check("un ajout manuel entre dans le flux", set(price_feed._wanted()) == {USDC, WSOL})
        price_feed.untrack(WSOL, kind="watch")
        check("le retrait libère le token et ses ticks",
              _kinds(price_feed, WSOL) is None and price_feed._wanted() == [USDC])

        price_feed.select(USDC)
        price_feed.select(WSOL)
        check("la sélection remplace l'ancienne", _kinds(price_feed, USDC) == ["call"],
              f"USDC={_kinds(price_feed, USDC)} WSOL={_kinds(price_feed, WSOL)}")
        check("le token sélectionné est suivi", "select" in (_kinds(price_feed, WSOL) or []))

        # D) a real poll: the feed must publish Dexscreener's own numbers
        price_feed.select("")
        set_store_calls([{"address": USDC}, {"address": WSOL}])
        config._settings["price_feed_calls"] = 2
        price_feed._wanted()
        before = price_feed.counters()
        await price_feed._poll_once()
        after = price_feed.counters()
        check("une requête Dexscreener émise", after["requests"] == before["requests"] + 1,
              f"{before['requests']} -> {after['requests']}")
        check("aucune erreur de flux", after["errors"] == 0 and not after["last_error"],
              after["last_error"])
        rows = {r["address"]: r for r in price_feed.status()["rows"]}
        usdc = rows.get(USDC) or {}
        wsol = rows.get(WSOL) or {}
        check("USDC coté près de 1 $ (donnée réelle)",
              0.9 < float(usdc.get("price_usd") or 0) < 1.1,
              f"prix={usdc.get('price_usd')}")
        check("WSOL coté (donnée réelle)", float(wsol.get("price_usd") or 0) > 1,
              f"prix={wsol.get('price_usd')}")
        check("les deux tokens viennent d'une seule requête", after["latency_ms"] >= 0 and len(rows) == 2,
              f"latence={after['latency_ms']} ms, lignes={len(rows)}")
        check("la latence est mesurée", after["latency_ms"] > 0, f"{after['latency_ms']} ms")
        check("les symboles sont réels", usdc.get("symbol") and wsol.get("symbol"),
              f"{usdc.get('symbol')} / {wsol.get('symbol')}")

        quote, age = price_feed.quote(USDC)
        check("le gestionnaire reçoit le dernier tick", quote.get("price_usd", 0) > 0 and age >= 0,
              f"age={age} ms")
        check("l'historique de ticks alimente la tendance",
              len(rows[USDC]["spark"]) == 1, f"{len(rows[USDC]['spark'])} point(s)")

        # E) disabled feed: no address is ever requested
        config._settings["price_feed_enabled"] = False
        check("flux désactivé -> aucune adresse interrogée", price_feed._wanted() == [])
        config._settings["price_feed_enabled"] = True

        # F) nothing wanted any more -> everything released
        set_store_calls([])
        price_feed._selected = ""
        price_feed._wanted()
        check("plus rien à suivre -> flux vidé", price_feed.counters()["tracked"] == 0,
              f"suivis={price_feed.counters()['tracked']}")
    finally:
        set_store_calls(saved_calls)
        st.positions[:] = saved_positions
        config._settings["price_feed_calls"] = 5


def _kinds(feed, address):
    for row in feed.status()["rows"]:
        if row["address"] == address:
            return row["kinds"]
    return None


def store_calls_backup():
    from backend.state import store
    return list(store.calls)


def set_store_calls(calls):
    from backend.state import store
    store.calls.clear()
    for call in calls:
        store.calls.append(call)


async def main() -> None:
    # Line-buffered: a piped stdout is block-buffered, and the sections used to
    # appear out of order in the report (a title printed before the checks it
    # introduced). The order of a diagnostic report is part of its usefulness.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    await rpc_section()
    await node_check()
    await live_filter_check()
    await exit_price_check()
    await route_retry_check()
    await paper_check()
    await price_feed_check()
    await execution_check()


asyncio.run(main())

print()
print("=" * 72)
print(f"RESULTAT : {'TOUT PASSE' if not fails else str(len(fails)) + ' ECHEC(S)'}")
for failure in fails:
    print(f"  - {failure}")
print("=" * 72)
sys.exit(1 if fails else 0)
