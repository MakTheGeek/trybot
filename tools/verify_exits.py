r"""Sorties (TP / SL / trailing) et montant du trade : ce qui se passe vraiment.

    .\venv\Scripts\python.exe tools\verify_exits.py

Pourquoi ce harnais existe
--------------------------
`tools/verify_params.py` pilote bien la boucle de gestion, mais il **remplace**
`_confirm_exit` par un double qui rend toujours la même raison (ligne 441). Il
prouve donc le seuil, et jamais le contrôle du prix exécutable — précisément le
maillon qui annule un take-profit quand le prix affiché n'est pas un prix que le
marché paie. Les deux suites étaient vertes pendant qu'un take-profit réglé à
+15 % ne se déclenchait pas alors que le prix affiché atteignait +39 % : c'est ce
contrôle qui avait parlé, et rien à l'écran ne l'expliquait. Ce harnais éprouve
donc la chaîne entière, dans les deux modes, sans jamais rien envoyer.

Ce qu'il prouve
---------------
1. la règle seule : seuils, bornes inclusives, priorité, et le 0 qui désactive au
   lieu de déclencher ;
2. le contrôle du prix exécutable : un take-profit est honoré quand le marché paie
   ce prix, **annulé** quand il ne le paie pas, et un stop protecteur n'est
   **jamais** affaibli ;
3. le gestionnaire de bout en bout, position **papier** : seuil franchi -> vente,
   faux pic -> rien ;
4. le même gestionnaire avec une position **réelle** : mêmes décisions, et rien
   n'est jamais signé ;
5. le montant du trade : exactement `trade_amount_sol`, dans les deux modes ;
6. l'invariant de mode : une position papier se ferme toujours virtuellement, une
   position réelle se vend toujours pour de vrai, quel que soit le mode courant.

Aucune écriture dans `data/` : les méthodes du magasin et les appels réseau sont
remplacés, et `sign_and_send` lève si on l'appelle.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402
from backend import jupiter  # noqa: E402
from backend import solana_rpc as solana  # noqa: E402
from backend import state as state_mod  # noqa: E402
from backend import trader as trader_mod  # noqa: E402

OK = "  [OK]"
KO = "  [ECHEC]"
fails: list[str] = []
passed = 0


# La console Windows (cp1252) ne sait pas écrire les cadres : les translittérer
# plutôt que de laisser tomber le harnais sur son propre compte rendu. Là où la
# sortie est capturée en UTF-8, rien ne change.
_CONSOLE_FALLBACK = {"\u2500": "-", "\u2550": "=", "\u23ed": ">>", "\ufe0f": ""}


def _printable(text: str) -> str:
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        for src, dst in _CONSOLE_FALLBACK.items():
            text = text.replace(src, dst)
        return text.encode(enc, "replace").decode(enc, "replace")


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
    else:
        fails.append(label)
    print(_printable(f"{OK if condition else KO} {label}"
                     + (f"  — {detail}" if detail else "")))


def title(text: str) -> None:
    print(_printable(f"\n{'=' * 78}\n{text}\n{'=' * 78}"))


def section(text: str) -> None:
    print(_printable(f"\n  ── {text} " + "─" * max(0, 70 - len(text))))


LAMPORTS = 1_000_000_000
SOL_MINT = "So11111111111111111111111111111111111111112"
engine = trader_mod.trader
exit_decision = trader_mod.exit_decision
SETTINGS_FILE = ROOT / "data" / "settings.json"
settings_before = hashlib.sha256(SETTINGS_FILE.read_bytes()).hexdigest()


class SettingsGuard:
    """Snapshot / restore ``config._settings`` without ever touching the file."""

    def __enter__(self):
        self.saved = copy.deepcopy(config._settings)
        return self

    def __exit__(self, *exc):
        config._settings.clear()
        config._settings.update(self.saved)
        return False


# ==================================================== 1. la règle, seule
title("1) La règle de sortie : seuils, bornes, priorité")


def rule(pnl: float, *, tp: float = 0.0, sl: float = 0.0, trail: float = 0.0,
         peak: float = 0.0, price: float = 0.0, armed: bool = False) -> str:
    return exit_decision(price=price, peak=peak, pnl_pct=pnl, tp_percent=tp,
                         sl_percent=sl, trailing_percent=trail,
                         trailing_active=armed)


check("take-profit atteint exactement au seuil (borne inclusive)",
      rule(15.0, tp=15.0) == "take_profit", "pnl = +15,00 % avec TP à 15 %")
check("take-profit pas encore atteint 0,01 % sous le seuil",
      rule(14.99, tp=15.0) == "", "pnl = +14,99 %")
check("take-profit à 0 désactivé (ne déclenche pas au premier tick)",
      rule(999.0, tp=0.0) == "")
check("stop-loss atteint exactement au seuil (borne inclusive)",
      rule(-15.0, sl=15.0) == "stop_loss", "pnl = −15,00 % avec SL à 15 %")
check("stop-loss pas encore atteint 0,01 % au-dessus",
      rule(-14.99, sl=15.0) == "", "pnl = −14,99 %")
check("stop-loss à 0 désactivé", rule(-999.0, sl=0.0) == "")
check("trailing non armé : ne sort jamais, même après un grand repli",
      rule(-90.0, tp=1e9, trail=10.0, peak=100.0, price=10.0, armed=False) == "")
check("trailing armé : sort au seuil exact (borne inclusive)",
      rule(0.0, tp=1e9, trail=10.0, peak=100.0, price=90.0, armed=True) == "trailing_stop",
      "prix = 90 = pic × 0,90")
check("trailing armé : ne sort pas 0,1 au-dessus du seuil",
      rule(0.0, tp=1e9, trail=10.0, peak=100.0, price=90.1, armed=True) == "")
check("trailing suit le pic, pas l'entrée",
      rule(-40.0, tp=1e9, trail=10.0, peak=200.0, price=100.0, armed=True) == "trailing_stop"
      and rule(-40.0, tp=1e9, trail=10.0, peak=100.0, price=100.0, armed=True) == "")
check("trailing avec un pic nul ne sort pas (division par zéro évitée)",
      rule(-50.0, tp=1e9, trail=10.0, peak=0.0, price=10.0, armed=True) == "")
check("trailing à 0 désactivé", 
      rule(0.0, tp=1e9, trail=0.0, peak=100.0, price=1.0, armed=True) == "")
check("le take-profit prime sur le trailing",
      rule(20.0, tp=15.0, trail=5.0, peak=100.0, price=80.0, armed=True) == "take_profit")
check("le stop-loss prime sur le trailing",
      rule(-20.0, sl=15.0, tp=1e9, trail=5.0, peak=100.0, price=80.0, armed=True) == "stop_loss")

# ====== 2. le contrôle du prix exécutable

saved = {
    "exec": engine._executable_price, "recheck": engine._log_exit_recheck,
    "sell": engine.sell, "close": engine._close_position,
    "swap": engine._jupiter_swap, "refresh": engine.refresh_wallet,
    "log": trader_mod.store.log, "add": trader_mod.store.add_position,
    "update": trader_mod.store.update_position,
    "push": trader_mod.store.push_position_update,
    "get": trader_mod.store.get_position,
    "new_id": trader_mod.store.new_position_id,
    "recompute": trader_mod.store.recompute_stats,
    "broadcast": trader_mod.store.broadcast,
    "quote": jupiter.quote, "build": jupiter.swap_transaction,
    "sign_only": solana.sign_only, "simulate": solana.simulate_signed,
    "send": solana.sign_and_send, "decimals": solana.mint_decimals,
    "ui_balance": solana.token_ui_balance, "wallet_address": solana.wallet_address,
}
saved_runtime = copy.deepcopy(state_mod.store.runtime)

sent: list[int] = []
log_lines: list[str] = []
registry: dict[str, dict] = {}
sell_calls: list[str] = []
swap_calls: list[dict] = []
close_calls: list[str] = []
_id_seq = [0]


async def _no_send(*a, **kw):
    sent.append(1)
    raise AssertionError("aucune transaction ne doit être envoyée par cette vérification")


def _record_log(message, *a, **kw):
    log_lines.append(str(message))


async def _record_sell(position, *, percent=100.0, reason="manual"):
    sell_calls.append(reason)
    return {"executed": True, "reason": reason}


async def _record_swap(**kw):
    """Double du chemin réseau : enregistre l'appel, ne signe rien.

    Le montant reçu est celui que le vrai code a demandé — c'est lui qu'on
    vérifie. Un appel ``dry_run`` (mode papier) reçoit une réponse de papier, un
    appel réel une réponse de swap, pour que les deux chemins restent exerçables.
    """
    swap_calls.append(dict(kw))
    if kw.get("dry_run"):
        return {"_paper": True, "_out_amount": 5_000_000, "_in_amount": 1_000_000,
                "_price_impact": 0.0, "_route": "Rayon", "_simulated": True,
                "_funding_only": False, "_quoted_only": False}
    if kw.get("side") == "buy":
        return {"tx_hash": "FAUX", "_tokens": 1000.0, "order_id": "FAUX-ID"}
    return {"tx_hash": "FAUX", "_sol_received": 0.05, "_sell_fee": 5e-05,
            "_remaining_tokens": 0.0}


async def _record_close(position, reason, **kw):
    close_calls.append(reason)
    return {"executed": True, "reason": reason}


def _record_update(pos_id, patch, persist=True):
    if pos_id in registry:
        registry[pos_id].update(patch)


trader_mod.store.log = _record_log
trader_mod.store.add_position = lambda p: registry.__setitem__(p["id"], p)
trader_mod.store.update_position = _record_update
trader_mod.store.push_position_update = lambda p: None
trader_mod.store.get_position = lambda pid: registry.get(pid)
trader_mod.store.new_position_id = lambda: (
    _id_seq.__setitem__(0, _id_seq[0] + 1) or f"verif{_id_seq[0]}")
trader_mod.store.recompute_stats = lambda: {}
trader_mod.store.broadcast = lambda *a, **kw: None
engine.sell = _record_sell
# `_log_exit_recheck` n'est **pas** remplacé : c'est lui qui écrit la ligne de
# journal, et cette ligne fait partie de ce qu'on vérifie. Il est capté par le
# double de `store.log` ci-dessus, et sa déduplication d'une minute est remise à
# zéro à chaque section.
engine._close_position = _record_close
solana.sign_and_send = _no_send
state_mod.store.runtime["sol_price"] = 150.0


def make_pos(*, paper: bool) -> dict:
    pos = {
        "id": "p1",
        "address": "Verif" + "3" * 40,
        "symbol": "VERIF",
        "status": "open",
        "tokens": 1_000_000.0,
        "decimals": 6,
        "entry_price": 1.0e-6,
        "current_price": 1.0e-6,
        "peak_price": 1.0e-6,
        "trailing_active": False,
        "trailing_enabled": False,
        "tp_percent": 15.0,
        "sl_percent": 15.0,
        "trailing_percent": 10.0,
        "cost_usd_total": 0.1 * 150.0,
        "amount_sol": 0.1,
        "sol_committed": 0.1,
    }
    if paper:
        pos["paper"] = True
    return pos


async def confirm(paper: bool, reason: str, *, display: float, real: float,
                  entry: float = 1.0e-6, peak: float | None = None) -> str:
    """Appelle le VRAI _confirm_exit avec un prix exécutable imposé."""
    pos = make_pos(paper=paper)

    async def fake(mint, *, raw=None, decimals=None):
        fake.last = (mint, raw, decimals)
        return real

    fake.last = None
    engine._executable_price = fake
    got = await engine._confirm_exit(pos, reason, display, peak or display, entry)
    confirm.last = fake.last
    return got


confirm.last = None


def setup_engine() -> None:
    engine.wallet = "TestWa11et" + "1" * 33
    engine.sol_balance = 1.0
    engine._closing.clear()
    # La ligne « Sortie recalculée » n'est écrite qu'une fois par minute et par
    # token ; chaque section repart donc d'un journal vierge.
    engine._last_exit_log.clear()


async def section2() -> None:
    title("2) Le contrôle du prix exécutable : le maillon qui annule un TP")
    setup_engine()
    entry = 1.0e-6

    section("un take-profit n'est pris que si le marché paie ce prix")
    got = await confirm(True, "take_profit", display=entry * 1.20, real=entry * 1.18)
    check("affiché +20 %, exécutable +18 % (>= TP 15 %) -> TP honoré",
          got == "take_profit", f"raison rendue : {got!r}")
    got = await confirm(True, "take_profit", display=entry * 1.60, real=entry * 0.98)
    check("affiché +60 %, exécutable −2 % (faux pic) -> TP ANNULÉ",
          got == "", f"raison rendue : {got!r}")
    got = await confirm(True, "take_profit", display=entry * 1.20, real=entry * 1.14)
    check("affiché +20 %, exécutable +14 % (juste sous le TP) -> TP annulé",
          got == "", "un centime de moins que le seuil ne le franchit pas")

    section("un stop protecteur n'est jamais affaibli")
    got = await confirm(True, "stop_loss", display=entry * 0.70, real=entry * 0.68)
    check("affiché −30 %, exécutable −32 % (accord) -> stop-loss honoré",
          got == "stop_loss", f"raison rendue : {got!r}")
    got = await confirm(True, "stop_loss", display=entry * 0.85, real=entry * 0.60)
    check("affiché −15 %, exécutable −40 % (le marché est pire) -> stop-loss honoré",
          got == "stop_loss", "on ne protège pas avec un prix que personne ne paie")
    got = await confirm(True, "stop_loss", display=entry * 0.70, real=entry * 0.868)
    check("écart de 24 % sous la tolérance de 25 % -> décision conservée",
          got == "stop_loss", "prix exécutable repassé au-dessus du SL, mais écart accepté")
    got = await confirm(True, "stop_loss", display=entry * 0.70, real=entry * 0.91)
    check("écart de 30 % au-delà de la tolérance -> recalculé et annulé",
          got == "", "un crash d'affichage sans acheteur ne déclenche pas le stop")
    got = await confirm(True, "trailing_stop", display=entry * 0.60, real=entry * 0.59)
    check("trailing stop protégé de la même façon",
          got == "trailing_stop", f"raison rendue : {got!r}")

    section("un devis indisponible ne désactive jamais la protection")
    got = await confirm(True, "take_profit", display=entry * 1.60, real=0.0)
    check("TP sans devis de contrôle -> décision conservée", got == "take_profit")
    got = await confirm(True, "stop_loss", display=entry * 0.70, real=0.0)
    check("SL sans devis de contrôle -> décision conservée", got == "stop_loss")

    section("le contrôle parle du bon montant, selon le mode")
    await confirm(True, "take_profit", display=entry * 1.6, real=entry * 0.98)
    mint, raw, decimals = confirm.last or (None, None, None)
    check("position papier : cotée sur ses propres unités (pas de compte on-chain)",
          raw == 1_000_000 * (10 ** 6) and decimals == 6,
          f"unités={raw} décimales={decimals}")
    await confirm(False, "take_profit", display=entry * 1.6, real=entry * 0.98)
    mint, raw, decimals = confirm.last or (None, None, None)
    check("position réelle : cotée sur le solde on-chain (unités lues, pas inventées)",
          raw is None and decimals is None,
          f"unités={raw} décimales={decimals}")

    check("une annulation laisse une trace dans le journal",
          any("recalcul" in line and "take_profit" in line for line in log_lines),
          "la ligne existe : c'est ce qui manque à l'écran")

# ================================ 3. le gestionnaire, position papier


async def drive(pos: dict, price: float, real: float | None = None) -> str:
    """Un tick du gestionnaire, avec un prix exécutable imposé."""

    async def fake(mint, *, raw=None, decimals=None):
        return price if real is None else real

    engine._executable_price = fake
    before = len(sell_calls)
    await engine._revalue(pos, {"price_usd": price})
    return sell_calls[before] if len(sell_calls) > before else ""


async def section3() -> None:
    title("3) Le gestionnaire de bout en bout — position PAPIER")
    setup_engine()
    with SettingsGuard():
        config._settings["paper_trading"] = True
        config._settings["tp_percent"] = 15.0
        config._settings["sl_percent"] = 15.0
        config._settings["trailing_percent"] = 10.0
        config._settings["trailing_enabled"] = False

        pos = make_pos(paper=True)
        registry.clear()
        registry[pos["id"]] = pos
        entry = float(pos["entry_price"])
        sell_calls.clear()

        section("le take-profit se déclenche, et pas trop tôt")
        check("+10 % (sous le TP de 15 %) : on garde",
              await drive(pos, entry * 1.10) == "")
        # +15,5 % et non +15 % pile : à ce niveau, la division qui calcule le PnL
        # peut rendre 14,999999999999998 %, et la borne inclusive manque de
        # 1e-14 % — sans conséquence en trading, mais le cas exact est prouvé sur
        # la règle seule en section 1, où le PnL est fourni sans division.
        check("+15,5 % affiché et exécutable au même niveau : vente",
              await drive(pos, entry * 1.155) == "take_profit")

        section("le cas signalé : le prix affiché atteint le TP, le marché non")
        pos["status"] = "open"
        pos["peak_price"] = entry
        sell_calls.clear()
        check("pic affiché à +60 %, mais le marché ne paie que −2 % : RIEN n'est vendu",
              await drive(pos, entry * 1.60, real=entry * 0.98) == "",
              "c'est le comportement exact observé : TP non déclenché")
        check("la position reste ouverte et garde son pic affiché",
              pos.get("status") == "open" and abs(float(pos["peak_price"]) - entry * 1.60) < 1e-12,
              f"statut={pos.get('status')} pic={float(pos['peak_price']):.3e}")
        check("l'écran continue d'afficher le PnL du prix Dexscreener (+60 %), "
              "pas celui du prix exécutable (−2 %)",
              abs(float(pos.get("pnl_percent") or 0) - 60.0) < 0.01,
              f"pnl={pos.get('pnl_percent')} % — c'est précisément l'écart qui donne "
              "l'impression que le take-profit ne fonctionne pas")
        check("l'annulation est journalisée avec les deux prix",
              any("recalcul" in line and "annul" in line for line in log_lines),
              "la ligne « Sortie … recalculée … (sortie annulée) » est écrite")
        chk = pos.get("exit_recheck") or {}
        check("l'écart mesuré est porté sur la position, pour la ligne de tableau",
              abs(float(chk.get("display") or 0) - entry * 1.60) < 1e-15
              and abs(float(chk.get("exec") or 0) - entry * 0.98) < 1e-15
              and chk.get("reason") == "take_profit" and chk.get("real") == "",
              f"affiché={chk.get('display')} exécutable={chk.get('exec')} "
              f"règle appliquée={chk.get('real')!r}")
        check("un tick sans désaccord fait disparaître l'écart de la position",
              await drive(pos, entry * 1.05) == "" and pos.get("exit_recheck") is None,
              "la ligne ne garde pas un écart qui s'est refermé")

        section("le stop-loss et le trailing, sur la même position")
        pos["status"] = "open"
        pos["tp_percent"] = 1e9
        pos["peak_price"] = entry
        sell_calls.clear()
        check("−30 % affiché, −32 % exécutable : vente sur stop-loss",
              await drive(pos, entry * 0.70, real=entry * 0.68) == "stop_loss")

        pos["status"] = "open"
        pos["trailing_enabled"] = True
        pos["peak_price"] = entry
        pos["trailing_active"] = False
        sell_calls.clear()
        check("trailing : +50 % atteint, puis repli à +30 % : vente",
              await drive(pos, entry * 1.50) == ""
              and await drive(pos, entry * 1.30) == "trailing_stop",
              f"appels = {sell_calls}")

        section("la position garde ses propres seuils")
        pos["status"] = "open"
        pos["trailing_enabled"] = False
        pos["tp_percent"] = 15.0
        pos["peak_price"] = entry
        sell_calls.clear()
        config._settings["tp_percent"] = 5.0
        check("TP réglé à 5 % après l'ouverture : la position sort toujours à 15 %, pas à 5 %",
              await drive(pos, entry * 1.10) == "" and float(pos["tp_percent"]) == 15.0,
              f"tp de la position={pos['tp_percent']} (réglage courant=5.0)")

# ================================ 4. le gestionnaire, position réelle


async def section4() -> None:
    title("4) Le gestionnaire de bout en bout — position RÉELLE (mode réel)")
    setup_engine()
    with SettingsGuard():
        config._settings["paper_trading"] = False
        config._settings["tp_percent"] = 15.0
        config._settings["sl_percent"] = 15.0
        config._settings["trailing_enabled"] = False

        pos = make_pos(paper=False)
        registry.clear()
        registry[pos["id"]] = pos
        entry = float(pos["entry_price"])
        sell_calls.clear()

        check("+20 % affiché, +19 % exécutable : vente sur take-profit",
              await drive(pos, entry * 1.20, real=entry * 1.19) == "take_profit",
              f"appels = {sell_calls}")
        check("aucune transaction n'a été signée ou diffusée", sent == [],
              f"envois={len(sent)}")

        pos["status"] = "open"
        pos["peak_price"] = entry
        sell_calls.clear()
        check("faux pic (+60 % affiché, −2 % exécutable) : rien n'est vendu non plus",
              await drive(pos, entry * 1.60, real=entry * 0.98) == "")

        pos["status"] = "open"
        pos["tp_percent"] = 1e9
        pos["peak_price"] = entry
        sell_calls.clear()
        check("−30 % : le stop-loss sort, en mode réel comme en papier",
              await drive(pos, entry * 0.70, real=entry * 0.68) == "stop_loss")

# ============================================ 5. le montant du trade


async def _fake_quote(*, input_mint, output_mint, amount, slippage_bps=3000, timeout=20.0):
    price_sol = 1e-4
    if output_mint == SOL_MINT:
        out = int(round(int(amount) / 1e6 * price_sol * 1e9))
    else:
        out = int(round((int(amount) / 1e9) / price_sol * 1e6))
    return {"inAmount": str(amount), "outAmount": str(out), "priceImpactPct": "0.01",
            "routePlan": [{"swapInfo": {"label": "Rayon"}}]}


async def _fake_build(**kw):
    return {"swapTransaction": "FAUSSE"}


async def _fake_sim(signed, *, sig_verify=True):
    return {"err": None, "unitsConsumed": 1000}


async def _fake_decimals(mint):
    return 6


async def _fake_ui_balance(wallet, mint):
    return ("V", 6, 1_000_000)


def _fake_wallet_address():
    """`solana_rpc.wallet_address` est **synchrone** dans le vrai code (appelé sans
    `await` à deux endroits) : le double doit l'être aussi, sinon le vrai code
    reçoit une coroutine au lieu d'une adresse."""
    return engine.wallet


async def section5() -> None:
    title("5) Le montant du trade : exactement `trade_amount_sol`, dans les deux modes")
    jupiter.quote = _fake_quote
    jupiter.swap_transaction = _fake_build
    solana.sign_only = lambda tx: tx
    solana.simulate_signed = _fake_sim
    solana.mint_decimals = _fake_decimals
    solana.token_ui_balance = _fake_ui_balance
    solana.wallet_address = _fake_wallet_address

    with SettingsGuard():
        registry.clear()
        config._settings["execution_backend"] = "jupiter"
        config._settings["slippage"] = 30
        engine.sol_balance = 1.0
        engine.wallet = "TestWa11et" + "1" * 33

        section("achat papier : le montant engagé est celui du réglage")
        config._settings["paper_trading"] = True
        config._settings["trade_amount_sol"] = 0.1
        buy = await engine.buy({"address": "Verif" + "4" * 40, "symbol": "V1",
                                "name": "V", "logo": "", "price_usd": 1e-6})
        pos = registry.get(buy.get("position_id") or "") or {}
        check("0,1 SOL réglé -> 0,1 SOL engagé",
              buy.get("executed") is True
              and abs(float(pos.get("amount_sol") or 0) - 0.1) < 1e-12
              and abs(float(pos.get("sol_committed") or 0) - 0.1) < 1e-12,
              f"engagé={pos.get('sol_committed')} SOL (raison={buy.get('reason')!r})")
        check("aucune transaction envoyée pour un achat papier", sent == [])

        config._settings["trade_amount_sol"] = 0.05
        buy2 = await engine.buy({"address": "Verif" + "5" * 40, "symbol": "V2",
                                 "name": "V", "logo": "", "price_usd": 1e-6})
        pos2 = registry.get(buy2.get("position_id") or "") or {}
        check("changer le réglage à 0,05 SOL change bien le montant engagé",
              abs(float(pos2.get("amount_sol") or 0) - 0.05) < 1e-12,
              f"engagé={pos2.get('amount_sol')} SOL")

        section("achat réel (Jupiter) : exactement le montant réglé, en lamports")
        # À partir d'ici le chemin réseau lui-même est remplacé : on veut prouver
        # le montant demandé, pas l'appeler.
        engine._jupiter_swap = _record_swap
        config._settings["paper_trading"] = False
        config._settings["trade_amount_sol"] = 0.05
        swap_calls.clear()
        sell_calls.clear()
        registry.clear()
        buy3 = await engine.buy({"address": "Verif" + "6" * 40, "symbol": "V3",
                                 "name": "V", "logo": "", "price_usd": 1e-6})
        call = swap_calls[-1] if swap_calls else {}
        check("le swap est construit pour 0,05 SOL = 50 000 000 lamports",
              call.get("amount") == 50_000_000 and call.get("side") == "buy",
              f"amount={call.get('amount')} lamports, côté={call.get('side')!r}")
        check("l'achat réel ouvre une position non papier",
              buy3.get("executed") is True
              and not (registry.get(buy3.get("position_id") or "") or {}).get("paper"),
              f"exécuté={buy3.get('executed')} raison={buy3.get('reason')!r}")

        section("le solde est vraiment contrôlé avant d'envoyer")
        engine.sol_balance = 0.004849152
        config._settings["trade_amount_sol"] = 0.1
        swap_calls.clear()
        buy4 = await engine.buy({"address": "Verif" + "7" * 40, "symbol": "V4",
                                 "name": "V", "logo": "", "price_usd": 1e-6})
        check("0,0048 SOL pour un achat de 0,1 SOL : refusé, rien n'est construit",
              buy4.get("executed") is False and swap_calls == []
              and "insuffisant" in (buy4.get("reason") or "").lower(),
              f"raison={buy4.get('reason')!r}")

        engine.sol_balance = 103_000_000 / LAMPORTS      # exactement le montant + la réserve
        swap_calls.clear()
        buy5 = await engine.buy({"address": "Verif" + "8" * 40, "symbol": "V5",
                                 "name": "V", "logo": "", "price_usd": 1e-6})
        check("103 000 000 lamports (= montant + 0,003 de réserve, au lamport près) : accepté",
              buy5.get("executed") is True and len(swap_calls) == 1,
              f"exécuté={buy5.get('executed')} raison={buy5.get('reason')!r}")

        engine.sol_balance = 102_999_999 / LAMPORTS      # un lamport de moins
        swap_calls.clear()
        buy6 = await engine.buy({"address": "Verif" + "9" * 40, "symbol": "V6",
                                 "name": "V", "logo": "", "price_usd": 1e-6})
        check("102 999 999 lamports (un lamport sous la réserve) : refusé",
              buy6.get("executed") is False and swap_calls == [],
              f"raison={buy6.get('reason')!r}")

        section("mode papier : le chemin réel n'est jamais emprunté")
        config._settings["paper_trading"] = True
        config._settings["trade_amount_sol"] = 0.1
        engine.sol_balance = 100.0
        swap_calls.clear()
        buy7 = await engine.buy({"address": "Verif" + "A" * 40, "symbol": "V7",
                                 "name": "V", "logo": "", "price_usd": 1e-6})
        paper_pos = registry.get(buy7.get("position_id") or "") or {}
        call7 = swap_calls[-1] if swap_calls else {}
        check("un achat en mode papier ne construit aucun swap réel (simulation seule)",
              paper_pos.get("paper") is True
              and bool(call7.get("dry_run")) is True
              and not any(not c.get("dry_run") for c in swap_calls),
              f"papier={paper_pos.get('paper')} dry_run={call7.get('dry_run')} "
              f"({len(swap_calls)} appel(s), aucun réel)")
        check("le montant reste celui du réglage en mode papier",
              abs(float(paper_pos.get("amount_sol") or 0) - 0.1) < 1e-12,
              f"engagé={paper_pos.get('amount_sol')} SOL")

# ============================================ 6. l'invariant de mode


async def section6() -> None:
    title("6) L'invariant de mode : aucune position n'est jamais abandonnée")
    engine.sell = saved["sell"]           # la VRAIE fonction sell
    engine._close_position = _record_close
    engine._jupiter_swap = _record_swap

    with SettingsGuard():
        config._settings["execution_backend"] = "jupiter"
        config._settings["paper_trading"] = False      # mode RÉEL
        setup_engine()

        section("une position papier reste virtuelle, même en mode réel")
        paper_pos = make_pos(paper=True)
        registry.clear()
        registry[paper_pos["id"]] = paper_pos
        swap_calls.clear()
        close_calls.clear()
        res = await engine.sell(paper_pos, percent=100.0, reason="take_profit")
        call = swap_calls[-1] if swap_calls else {}
        check("le sac virtuel n'est pas vendu pour de vrai (devis seul)",
              res.get("executed") is True and call.get("dry_run") is True,
              f"dry_run={call.get('dry_run')} raison={res.get('reason')!r}")
        check("sortir du mode papier n'abandonne pas une position papier",
              res.get("paper") is True and sent == [],
              "vente papier : cotée, jamais signée")

        section("une position réelle est vendue pour de vrai, même en mode papier")
        config._settings["paper_trading"] = True       # mode PAPIER
        real_pos = make_pos(paper=False)
        registry.clear()
        registry[real_pos["id"]] = real_pos
        swap_calls.clear()
        close_calls.clear()
        res2 = await engine.sell(real_pos, percent=100.0, reason="stop_loss")
        call2 = swap_calls[-1] if swap_calls else {}
        check("le sac réel passe par une vraie vente (dry_run désactivé)",
              res2.get("executed") is True and not call2.get("dry_run"),
              f"dry_run={call2.get('dry_run')!r} côté={call2.get('side')!r}")
        check("et c'est bien une vente, pas un achat",
              call2.get("side") == "sell" and call2.get("input_mint") == real_pos["address"])
        check("rien n'a été signé par ce harnais", sent == [], f"envois={len(sent)}")

        section("le pourcentage de vente ne peut jamais être détourné")
        for bad, want in ((-5, "nul"), (0, "nul"), ("x", "100 %")):
            registry.clear()
            p = make_pos(paper=True)
            registry[p["id"]] = p
            swap_calls.clear()
            r = await engine.sell(p, percent=bad, reason="manual")
            got = (swap_calls[-1].get("amount") if swap_calls else None)
            if bad == "x":
                check("un pourcentage illisible devient 100 % (jamais 0)",
                      r.get("executed") is True,
                      f"raison={r.get('reason')!r}")
            else:
                check(f"un pourcentage {bad} est refusé (vente {want})",
                      r.get("executed") is False and swap_calls == [],
                      f"raison={r.get('reason')!r}")

# ==================================================== 7. câblage


def source_of(fn) -> str:
    return inspect.getsource(fn)


def section7() -> None:
    title("7) Le câblage du code (ce que le harnais doit pouvoir casser)")
    revalue = source_of(trader_mod.Trader._revalue)
    check("_revalue confirme toute sortie avant de vendre",
          "_confirm_exit" in revalue
          and revalue.index("_confirm_exit") < revalue.index("await self.sell"),
          "_confirm_exit est appelé avant sell")
    check("une sortie annulée sort de la boucle sans vendre",
          "if not exit_reason:" in revalue
          and "return" in revalue.split("if not exit_reason:")[1]
          .split("await self.sell")[0],
          "le return est bien là")
    sell_src = source_of(trader_mod.Trader.sell)
    check("sell route une position papier vers la voie virtuelle, en premier",
          'if position.get("paper"):' in sell_src
          and sell_src.index('if position.get("paper"):')
          < sell_src.index("self._paper_sell"),
          "la sortie papier précède tout appel réseau")
    check("le take-profit n'échappe pas au contrôle du prix exécutable",
          "if reason != \"take_profit\" and gap < _EXIT_PRICE_TOLERANCE:" in
          source_of(trader_mod.Trader._confirm_exit),
          "la tolérance de 25 % ne s'applique pas au TP")
    check("les seuils de sortie sont posés sur la position, pas relus du réglage courant",
          'float(settings["tp_percent"])' in source_of(trader_mod.Trader._paper_buy)
          and 'pos.get("tp_percent")' in revalue,
          "ouverture : settings ; sortie : position")
    check("le contrôle du prix exécutable enregistre l'écart qu'il a mesuré",
          'self._exit_check[pos["address"]]' in
          source_of(trader_mod.Trader._confirm_exit),
          "_confirm_exit alimente _exit_check")
    check("cet écart est porté sur la position, et remis à zéro à chaque tick",
          '"exit_recheck": None' in revalue
          and 'patch["exit_recheck"] = check' in revalue,
          "_revalue le recopie dans le patch, puis le remet à None")
    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    check("la ligne de position l'affiche (badge + PnL au prix exécutable)",
          "function exitRecheck" in app_js and "exit_recheck" in app_js
          and app_js.count("exitRecheck(") >= 3,
          "lu dans frontend/app.js : les deux tableaux l'appellent")
    check("le badge s'explique : prix affiché, prix exécutable, règle appliquée",
          "prix exécutable (ce que le marché paie)" in app_js
          and "Règle appliquée" in app_js and "exéc. " in app_js)


async def main() -> None:
    try:
        await section2()
        await section3()
        await section4()
        await section5()
        await section6()
        section7()
    finally:
        engine._executable_price = saved["exec"]
        engine._log_exit_recheck = saved["recheck"]
        engine.sell = saved["sell"]
        engine._close_position = saved["close"]
        engine._jupiter_swap = saved["swap"]
        engine.refresh_wallet = saved["refresh"]
        trader_mod.store.log = saved["log"]
        trader_mod.store.add_position = saved["add"]
        trader_mod.store.update_position = saved["update"]
        trader_mod.store.push_position_update = saved["push"]
        trader_mod.store.get_position = saved["get"]
        trader_mod.store.new_position_id = saved["new_id"]
        trader_mod.store.recompute_stats = saved["recompute"]
        trader_mod.store.broadcast = saved["broadcast"]
        jupiter.quote = saved["quote"]
        jupiter.swap_transaction = saved["build"]
        solana.sign_only = saved["sign_only"]
        solana.simulate_signed = saved["simulate"]
        solana.sign_and_send = saved["send"]
        solana.mint_decimals = saved["decimals"]
        solana.token_ui_balance = saved["ui_balance"]
        solana.wallet_address = saved["wallet_address"]
        state_mod.store.runtime.clear()
        state_mod.store.runtime.update(saved_runtime)


asyncio.run(main())

print()
print("=" * 78)
settings_after = hashlib.sha256(SETTINGS_FILE.read_bytes()).hexdigest()
check("data/settings.json est resté identique octet pour octet",
      settings_before == settings_after,
      f"{settings_before[:16]} -> {settings_after[:16]}")
check("aucune transaction n'a été envoyée de tout le harnais", sent == [],
      f"envois={len(sent)}")
print("=" * 78)
print(f"  {passed}/{passed + len(fails)} vérifications passées")
if fails:
    print(f"  ÉCHECS ({len(fails)}):")
    for name in fails:
        print(f"    - {name}")
    print("  RESULTAT : ECHEC")
    sys.exit(1)
print("  RESULTAT : TOUT PASSE")
print("=" * 78)
