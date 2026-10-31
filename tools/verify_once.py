r"""Un seul achat par token, et un PnL de trade fermé qui ne bouge plus.

    .\venv\Scripts\python.exe tools\verify_once.py

Pourquoi ce harnais existe
--------------------------
Deux plaintes, deux causes distinctes — et aucune des deux n'était celle qu'on
croyait.

1) « Le bot snipe le même token 2 fois, 3 fois. »  Il ne le faisait pas : les 83
   positions de `data/positions.json` portent 83 adresses **distinctes**, et aucun
   token n'a jamais été racheté. Deux choses donnaient cette impression :
     * le journal écrivait **deux lignes `action="buy"` par achat papier** — la
       ligne d'achat, puis une *note* (« simulation refusée faute de SOL ») qui
       portait elle aussi l'action d'achat ;
     * `shortAddr()` affichait « 7ZY...ump » et « FTM...ump » : tous les contrats
       pump.fun finissent par « pump », donc la queue affichée était identique
       pour tous et trois tokens *différents* nommés « GM » semblaient être le
       même.
   Et la protection réelle contre un second achat n'était qu'une mémoire de 30
   minutes (`bot._recent`), perdue au redémarrage : après 30 min, ou après un
   redémarrage, le même contrat pouvait bel et bien être racheté.

2) « Une fois le trade fermé, son PnL doit être visible et rester le même. »  Le
   PnL réalisé est bien enregistré et figé (mesuré : deux relevés à quelques
   secondes d'écart, 0 ligne modifiée). Ce harnais le démontre au lieu de
   l'affirmer, et vérifie qu'aucune boucle ne peut réécrire un trade fermé.

Ce qu'il prouve
---------------
1. la règle seule : `find_position_by_address` trouve une position ouverte **et**
   une position fermée, ne trouve pas un token jamais traité, et ne confond pas
   deux contrats qui portent le même nom ;
2. un appel Telegram pour un contrat déjà acheté est refusé **avant** la file
   d'analyse (donc sans dépense de requête GMGN) ;
3. un snipe automatique sur un contrat déjà acheté n'achète rien, le dit dans le
   journal, et laisse la position existante intacte ;
4. un **autre** contrat portant le **même nom** reste snipable (pas de blocage
   par ressemblance) ;
5. un token **rejeté** la première fois reste snipable plus tard : la règle porte
   sur les positions, pas sur les appels ;
6. un snipe **manuel** obéit à la règle comme un appel Telegram — y compris le
   bouton « Analyser & sniper », refusé **avant** l'analyse (donc sans requête
   GMGN) ; son seul privilège reste de ne pas dépendre de `auto_snipe` ;
7. la règle **survit à un redémarrage** (nouveau `Bot`, mémoire vide) ;
8. le réglage `snipe_once_per_token` remis à `false` rend l'ancien comportement ;
9. la boucle de gestion ne touche **jamais** un trade fermé : le PnL réalisé, le
   prix de sortie et la raison de sortie restent ceux de la clôture ;
10. un achat papier n'écrit plus qu'**une** ligne d'achat, et la note de
    financement se lit comme la suite du **même** achat.

Aucune écriture disque : `state._write` est remplacé par un compteur, les appels
réseau sont remplacés, et `sign_and_send` lève si on l'appelle.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import pathlib
import sys
from collections import deque

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import bot as bot_mod  # noqa: E402
from backend import config  # noqa: E402
from backend import jupiter  # noqa: E402
from backend import solana_rpc as solana  # noqa: E402
from backend import state as state_mod  # noqa: E402
from backend import trader as trader_mod  # noqa: E402

OK = "  [OK]"
KO = "  [ECHEC]"
fails: list[str] = []
passed = 0


_CONSOLE_FALLBACK = {
    "\u2500": "-",    # ─
    "\u2550": "=",    # ═
    "\u23ed": ">>",   # ⏭
    "\ufe0f": "",      # sélecteur de variante (emoji)
}


def _printable(text: str) -> str:
    """La console Windows (cp1252) ne sait pas écrire les emojis ni les cadres.

    Les translittérer plutôt que les laisser lever : un harnais qui tombe sur son
    propre compte rendu ne prouve plus rien. Là où la console est en UTF-8 (le
    cas de `run_all.py`, qui capture en `encoding="utf-8"`), rien ne change.
    """
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
    line = f"{OK if condition else KO} {label}" + (f"  — {detail}" if detail else "")
    print(_printable(line))


def title(text: str) -> None:
    print(_printable(f"\n{'=' * 78}\n{text}\n{'=' * 78}"))


def section(text: str) -> None:
    print(_printable(f"\n  ── {text} " + "─" * max(0, 70 - len(text))))


SETTINGS_FILE = ROOT / "data" / "settings.json"
settings_before = hashlib.sha256(SETTINGS_FILE.read_bytes()).hexdigest()

# Trois contrats pump.fun **réels**, deux d'entre eux portant le même nom : c'est
# exactement ce que l'utilisateur lisait comme « le même token snipé plusieurs
# fois ».
SONG_A = "7ZYkyb6h8yB4Ny1CU5v7T2ijAaQMzxdBuJL353bvpump"
SONG_B = "FTMtRCUZXno1iwkaAAUjCnPSZrnoLSmhgYr6D7uxpump"
GM_A = "BgAEsU9etsn3bEE6VBkT1Y8PR2BSEbwhG6bC2JdYpump"
GM_B = "AihvkxZrnRkVLoyjFBwLdxzPCf1XdfgTBgB1gkwBpump"
FRESH = "Fresh" + "1" * 39
NEVER = "Never" + "2" * 39


# ============================================ le magasin, sans disque
# `state._write` est le seul point d'écriture du magasin : le remplacer par un
# compteur rend le harnais hermétique **et** prouve que le double a bien été
# traversé (sinon les vérifications seraient vides).
write_calls: list[str] = []
state_mod._write = lambda path, data: write_calls.append(str(path))


def fresh_store() -> state_mod.Store:
    st = state_mod.Store()
    st.positions = []
    st.calls = deque(maxlen=state_mod.MAX_CALLS)
    st.logs = deque(maxlen=state_mod.MAX_LOGS)
    st.stats = {}
    st.runtime["sol_price"] = 150.0
    return st


store = fresh_store()
bot_mod.store = store
trader_mod.store = store
state_mod.store = store

saved = {
    "store": store,
    "analyze": bot_mod.analyze,
    "trader": bot_mod.trader,
    "wallet_address": solana.wallet_address,
    "sign_only": solana.sign_only,
    "simulate": solana.simulate_signed,
    "mint_decimals": solana.mint_decimals,
    "send": solana.sign_and_send,
    "quote": jupiter.quote,
    "build": jupiter.swap_transaction,
    "revalue": trader_mod.trader._revalue,
}


class SettingsGuard:
    """Photographie / restaure ``config._settings`` sans jamais toucher au fichier."""

    def __enter__(self):
        self.saved = copy.deepcopy(config._settings)
        return self

    def __exit__(self, *exc):
        config._settings.clear()
        config._settings.update(self.saved)
        return False


engine = trader_mod.trader


def source_of(fn) -> str:
    import inspect
    try:
        return inspect.getsource(fn)
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------- fabriques

def position(address: str, symbol: str, *, status: str = "open",
             paper: bool = False, pnl_usd: float = 0.0,
             pnl_percent: float = 0.0) -> dict:
    pos = {
        "id": "fixture_" + address[:6],
        "address": address,
        "chain": "sol",
        "symbol": symbol,
        "name": symbol,
        "logo": "",
        "status": status,
        # 0,1 SOL à 150 $ = 15 $ de coût : les jetons doivent valoir autant à
        # l'entrée pour que le PnL du test soit cohérent (15 000 000 × 1e-6 $).
        "tokens": 15_000_000.0,
        "decimals": 6,
        "entry_price": 1.0e-6,
        "current_price": 1.0e-6,
        "peak_price": 1.0e-6,
        "trailing_active": False,
        "trailing_enabled": False,
        "tp_percent": 15.0,
        "sl_percent": 15.0,
        "trailing_percent": 10.0,
        "amount_sol": 0.1,
        "sol_committed": 0.1,
        "cost_usd": 15.0,
        "cost_usd_total": 15.0,
        "pnl_usd": pnl_usd,
        "pnl_percent": pnl_percent,
    }
    if paper:
        pos["paper"] = True
    return pos


def report(address: str, symbol: str, decision: str = "sniped") -> dict:
    return {
        "address": address, "symbol": symbol, "name": symbol, "logo": "",
        "decision": decision,
        "reasons": [] if decision == "sniped" else ["Volume 24 h < MarketCap"],
        "failed_ids": [], "checks": [], "source": "telegram", "channel": "canal",
        "message": "", "ts": 0, "market_cap": 12_000.0, "volume_24h": 30_000.0,
        "liquidity": 4_000.0, "top10_pct": 12.0, "buyers": 40, "sellers": 8,
        "age_minutes": 3.0, "holders": 120, "kols": 1, "smart_wallets": 2,
        "website": "https://exemple.test", "socials": ["https://x.test/a"],
        "price_usd": 1.0e-6, "price_at_call": 1.0e-6, "error": False,
        # La décision de l'analyseur est remplacée : ce n'est pas elle qu'on éprouve.
        "_decision": decision,
    }


verdicts: dict[str, tuple[str, str]] = {}
# Compte les analyses réellement lancées : c'est ainsi qu'on prouve qu'un refus
# n'a coûté **aucune** requête GMGN, et non simplement qu'il a échoué.
analyze_calls: list[str] = []


async def fake_analyze(address, *a, **kw):
    analyze_calls.append(address)
    symbol, decision = verdicts.get(address, ("VERIF", "sniped"))
    return report(address, symbol, decision)


class FakeTrader:
    """Double du trader : enregistre l'achat, ouvre une position, ne signe rien."""

    def __init__(self) -> None:
        self.buys: list[dict] = []
        self.wallet = ""

    async def buy(self, rep: dict) -> dict:
        self.buys.append(dict(rep))
        paper = bool(config.get("paper_trading"))
        pos = position(rep["address"], rep.get("symbol", "?"), status="open",
                       paper=paper)
        pos["id"] = f"snipe{len(self.buys)}"
        store.add_position(pos)
        return {"executed": True, "position_id": pos["id"], "paper": paper,
                "reason": "ok"}

    async def refresh_wallet(self):  # noqa: ANN201
        return None


bots = FakeTrader()
bot_mod.analyze = fake_analyze
bot_mod.trader = bots
solana.sign_and_send = lambda *a, **kw: (_ for _ in ()).throw(
    AssertionError("aucune transaction ne doit être envoyée par cette vérification"))


def logs_of(action: str) -> list[dict]:
    return [e for e in store.logs if e.get("action") == action]


def messages(action: str = "") -> list[str]:
    return [str(e.get("message")) for e in store.logs
            if not action or e.get("action") == action]


def new_bot() -> bot_mod.Bot:
    b = bot_mod.Bot()
    b.queue = asyncio.Queue(maxsize=bot_mod.QUEUE_MAX)
    return b


async def process(bot: bot_mod.Bot, address: str, *, source: str = "telegram") -> dict:
    """Fait tourner le vrai `_process` sur un appel donné."""
    store.logs.clear()
    job = {"address": address, "message": "", "channel": "canal",
           "source": source, "attempts": 0, "ts": __import__("time").time()}
    await bot._process(job)
    return store.calls[0] if store.calls else {}


# ==================================================== 1. la règle, seule
title("1) La règle : un contrat déjà acheté se retrouve, un autre non")


def rule() -> None:
    store.positions = [
        position(SONG_A, "Songgang", status="closed", pnl_usd=-1.59, pnl_percent=-15.16),
        position(GM_A, "GM", status="open"),
    ]
    check("une position FERMÉE compte comme déjà traité (le trade est fini, le contrat non)",
          (store.find_position_by_address(SONG_A) or {}).get("status") == "closed")
    check("une position OUVERTE compte comme déjà traité",
          (store.find_position_by_address(GM_A) or {}).get("status") == "open")
    check("un contrat jamais acheté n'est pas bloqué",
          store.find_position_by_address(FRESH) is None)
    check("un autre contrat portant le même nom que le fermé n'est PAS bloqué",
          store.find_position_by_address(SONG_B) is None,
          "« Songgang » existe déjà, mais c'est une autre adresse")
    check("une adresse vide ne fait rien lever",
          store.find_position_by_address("") is None)


rule()


# ==================================================== 2. l'entrée : handle_call
title("2) L'appel Telegram d'un contrat déjà acheté est refusé avant la file")


async def section2() -> None:
    store.positions = [position(SONG_A, "Songgang", status="closed",
                                pnl_usd=-1.59, pnl_percent=-15.16)]
    bot = new_bot()

    res = await bot.handle_call(SONG_A, "", "canal")
    check("l'appel est refusé", res.get("queued") is False
          and res.get("reason") == "already-traded", f"réponse={res}")
    check("rien n'entre dans la file d'analyse (aucune requête GMGN dépensée)",
          bot.queue.qsize() == 0)
    line = messages("once")
    check("le journal le dit, avec le nom du token et la raison",
          len(line) == 1 and "Songgang" in line[0] and "trade clôturé" in line[0],
          line[0] if line else "aucune ligne")
    check("le compteur d'appels ignorés est incrémenté", bot.skipped == 1)

    section("un contrat jamais acheté passe normalement")
    res2 = await bot.handle_call(FRESH, "", "canal")
    check("l'appel est mis en file", res2.get("queued") is True, f"réponse={res2}")
    check("la file contient bien l'appel", bot.queue.qsize() == 1)

    section("le token rejeté la première fois reste snipable (la règle porte sur les positions)")
    res3 = await bot.handle_call(NEVER, "", "canal")
    check("un contrat sans position connue est mis en file", res3.get("queued") is True)

    section("un snipe MANUEL est refusé à l'entrée, comme un appel Telegram")
    # Bot neuf : la mémoire de 30 minutes ne doit pas décider à la place du
    # garde-fou qu'on éprouve ici.
    manual_bot = new_bot()
    auto_bot = new_bot()
    store.logs.clear()
    analyze_calls.clear()
    res4 = await manual_bot.handle_call(SONG_A, "", "canal", source="manual")
    check("l'appel manuel est refusé", res4.get("queued") is False
          and res4.get("reason") == "already-traded", f"réponse={res4}")
    check("il n'entre pas dans la file", manual_bot.queue.qsize() == 0)
    check("aucune analyse n'est lancée pour lui", analyze_calls == [],
          f"{len(analyze_calls)} analyse(s)")
    line4 = messages("once")
    check("le journal nomme la règle, snipe manuel compris",
          len(line4) == 1 and "un seul achat par token" in line4[0],
          line4[0] if line4 else "aucune ligne")
    auto = await auto_bot.handle_call(SONG_A, "", "canal")
    check("le même contrat en automatique est refusé de la même façon",
          auto.get("queued") is False and auto.get("reason") == "already-traded",
          f"réponse={auto}")


asyncio.run(section2())


# ==================================================== 3. le bot n'achète pas 2 fois
title("3) Le snipe automatique d'un contrat déjà acheté n'achète rien")


async def section3() -> None:
    bot = new_bot()
    bots.buys.clear()
    store.calls = deque(maxlen=state_mod.MAX_CALLS)

    with SettingsGuard():
        config._settings["paper_trading"] = True
        config._settings["auto_snipe"] = True
        config._settings["live_trading"] = True
        config._settings["snipe_once_per_token"] = True
        config._settings["max_concurrent_positions"] = 10

        section("position FERMÉE : le contrat a déjà été acheté une fois")
        verdicts[SONG_A] = ("Songgang", "sniped")
        rep = await process(bot, SONG_A)
        check("aucun achat n'est déclenché", bots.buys == [],
              f"achats={len(bots.buys)}")
        check("l'appel est enregistré comme non exécuté",
              rep.get("executed") is False)
        check("la raison affichée nomme la règle",
              "déjà traité" in str((rep.get("execution") or {}).get("reason")),
              str((rep.get("execution") or {}).get("reason")))
        line = messages("once")
        check("le journal explique : « trade clôturé », un seul achat par token",
              len(line) == 1 and "trade clôturé" in line[0]
              and "un seul achat par token" in line[0],
              line[0] if line else "aucune ligne")
        check("la ligne « validé » n'est PAS écrite (les critères passaient)",
              messages("sniped") == [])

        section("position OUVERTE : rien n'est rajouté, et la position est intacte")
        store.positions.append(position(GM_A, "GM", status="open",
                                        pnl_usd=2.5, pnl_percent=8.0))
        before = copy.deepcopy(next(p for p in store.positions
                                    if p["address"] == GM_A))
        verdicts[GM_A] = ("GM", "sniped")
        bots.buys.clear()
        rep2 = await process(bot, GM_A)
        after = next(p for p in store.positions if p["address"] == GM_A)
        check("aucun second achat", bots.buys == [])
        check("la position ouverte n'est pas modifiée (ni son PnL, ni son statut)",
              before == after)
        line2 = messages("once")
        check("le journal dit « position ouverte »",
              len(line2) == 1 and "position ouverte" in line2[0],
              line2[0] if line2 else "aucune ligne")

        section("un contrat DIFFÉRENT qui porte le même nom est bien acheté")
        verdicts[SONG_B] = ("Songgang", "sniped")
        bots.buys.clear()
        rep3 = await process(bot, SONG_B)
        check("le second « Songgang » est acheté", len(bots.buys) == 1
              and bots.buys[0]["address"] == SONG_B,
              f"achats={len(bots.buys)}")
        check("il est bien marqué exécuté", rep3.get("executed") is True)

        section("un token rejeté la première fois, snipé plus tard")
        verdicts[NEVER] = ("NEUF", "rejected")
        bots.buys.clear()
        rep4 = await process(bot, NEVER)
        check("aucune position n'est ouverte pour un rejet",
              rep4.get("executed") is False and bots.buys == [])
        check("le rejet n'a laissé aucune position",
              store.find_position_by_address(NEVER) is None)
        verdicts[NEVER] = ("NEUF", "sniped")
        rep5 = await process(bot, NEVER)
        check("le même contrat, snipé le jour où il passe, est bien acheté",
              len(bots.buys) == 1 and bots.buys[0]["address"] == NEVER,
              f"achats={len(bots.buys)}")
        check("l'achat est marqué exécuté", rep5.get("executed") is True)

        section("la règle survit à un REDÉMARRAGE (mémoire des appels vide)")
        restarted = new_bot()
        bots.buys.clear()
        rep6 = await process(restarted, NEVER)
        check("le nouveau Bot n'achète pas un contrat déjà acheté",
              bots.buys == [], f"achats={len(bots.buys)}")
        check("il le dit aussi", "déjà traité" in str(
            (rep6.get("execution") or {}).get("reason")))
        check("la mémoire de 30 minutes, elle, est bien repartie de zéro",
              restarted._recent == {})

        section("snipe MANUEL : refusé lui aussi sur un contrat déjà traité")
        bots.buys.clear()
        store.logs.clear()
        rep7 = await process(bot, NEVER, source="manual")
        check("aucun achat manuel n'est déclenché", bots.buys == [],
              f"achats={len(bots.buys)}")
        check("l'appel est enregistré non exécuté", rep7.get("executed") is False)
        check("la raison nomme la règle",
              "déjà traité" in str((rep7.get("execution") or {}).get("reason")),
              str((rep7.get("execution") or {}).get("reason")))
        line7 = messages("once")
        check("le journal le dit aussi", len(line7) == 1
              and "un seul achat par token" in line7[0],
              line7[0] if line7 else "aucune ligne")

        section("le bouton « Analyser & sniper » refuse SANS lancer d'analyse")
        bots.buys.clear()
        store.logs.clear()
        analyze_calls.clear()
        rep9 = await bot.force_snipe(NEVER)
        check("aucun achat n'est déclenché", bots.buys == [])
        check("aucune requête GMGN n'est dépensée", analyze_calls == [],
              f"{len(analyze_calls)} analyse(s)")
        check("la réponse est un refus explicite",
              rep9.get("executed") is False
              and rep9.get("reason") == "already-traded", f"réponse={rep9}")
        check("elle porte la phrase que l'utilisateur lira",
              "un seul achat par token" in str((rep9.get("reasons") or [""])[0]),
              str((rep9.get("reasons") or [""])[0]))
        line9 = messages("once")
        check("le journal précise que c'est le snipe manuel qui est refusé",
              len(line9) == 1 and "snipe manuel refusé" in line9[0],
              line9[0] if line9 else "aucune ligne")

        section("contrôle positif : le bouton marche sur un contrat jamais acheté")
        verdicts[FRESH] = ("NEUF3", "sniped")
        bots.buys.clear()
        analyze_calls.clear()
        rep10 = await bot.force_snipe(FRESH)
        check("l'analyse a bien eu lieu", analyze_calls == [FRESH],
              f"{analyze_calls}")
        check("et l'achat manuel passe", len(bots.buys) == 1
              and bots.buys[0]["address"] == FRESH, f"achats={len(bots.buys)}")
        check("il est marqué exécuté", rep10.get("executed") is True)

        section("deux clics SIMULTANÉS : un seul achat peut partir")
        RACE = "Race" + "7" * 40
        gate = asyncio.Event()
        entered = asyncio.Event()
        slow_analyze = bot_mod.analyze

        async def blocking_analyze(address, *a, **kw):
            entered.set()
            await gate.wait()
            return report(address, "RACE", "sniped")

        bots.buys.clear()
        bot_mod.analyze = blocking_analyze
        try:
            first = asyncio.create_task(bot.force_snipe(RACE))
            await asyncio.wait_for(entered.wait(), 10)
            second = await bot.force_snipe(RACE)
            gate.set()
            rep_first = await first
        finally:
            bot_mod.analyze = slow_analyze
            gate.set()
        check("le second clic est refusé pendant que le premier tourne",
              second.get("reason") == "in-progress", f"réponse={second}")
        check("aucun second achat n'est déclenché", len(bots.buys) == 1,
              f"achats={len(bots.buys)}")
        check("le premier clic, lui, aboutit", rep_first.get("executed") is True)
        check("le contrat n'est plus marqué « en cours » après le snipe",
              bot._manual_inflight == set(), str(bot._manual_inflight))
        later = await bot.force_snipe(RACE)
        check("un clic plus tard retombe sur la règle, pas sur « en cours »",
              later.get("reason") == "already-traded", f"réponse={later}")

        section("le réglage désactivé rend l'ancien comportement")
        config._settings["snipe_once_per_token"] = False
        bots.buys.clear()
        rep8 = await process(bot, NEVER)
        check("sans la règle, le contrat est racheté", len(bots.buys) == 1,
              f"achats={len(bots.buys)}")
        check("l'achat est marqué exécuté", rep8.get("executed") is True)


asyncio.run(section3())


# ==================================================== 4. le PnL d'un trade fermé
title("4) Un trade fermé n'est plus jamais réévalué")


async def _no_exit(pos, reason, *a, **kw):
    raise AssertionError(
        "aucune sortie ne doit être décidée dans cette section (le prix est choisi "
        "pour ne franchir aucun seuil)")


async def _no_sell(*a, **kw):
    raise AssertionError("aucune vente ne doit être déclenchée par cette section")


async def section4() -> None:
    engine = trader_mod.trader
    closed_before = position(SONG_A, "Songgang", status="closed",
                             pnl_usd=-1.59, pnl_percent=-15.16)
    closed_before.update({"exit_price": 4.26e-6, "exit_reason": "stop_loss",
                          "closed_at": 1_792_410_781_378})
    opened = position(GM_A, "GM", status="open")
    store.positions = [copy.deepcopy(closed_before), opened]

    # +5 % : au-dessus du stop, sous le take-profit, donc **aucune** sortie n'est
    # décidée et la vérification ne dépend ni du réseau ni de `_confirm_exit`.
    quote = {"price_usd": 1.05e-6, "market_cap": 16_000.0, "liquidity_usd": 5_000.0,
             "volume_24h": 40_000.0}
    # Filet : si une sortie était tout de même décidée, on veut un échec net, pas
    # un appel réseau.
    engine._confirm_exit = _no_exit
    engine.sell = _no_sell
    # Le corps exact de la boucle de gestion : elle ne prend que les positions
    # ouvertes, puis réévalue chaque tick.
    section("la boucle de gestion ne voit que les positions ouvertes")
    selected = [p["address"] for p in store.open_positions()]
    check("le contrat du trade fermé n'est pas dans la sélection",
          SONG_A not in selected and GM_A in selected, f"sélection={selected}")
    check("la source de `_manage_loop` sélectionne bien par `open_positions()`",
          "store.open_positions()" in source_of(__import__(
              "backend.trader", fromlist=["trader"]).trader._manage_loop))

    for pos in store.open_positions():
        await engine._revalue(pos, quote)

    closed_after = next(p for p in store.positions if p["address"] == SONG_A)
    check("le PnL réalisé du trade fermé est inchangé",
          closed_after.get("pnl_usd") == -1.59
          and closed_after.get("pnl_percent") == -15.16,
          f"pnl_usd={closed_after.get('pnl_usd')} · {closed_after.get('pnl_percent')}%")
    check("le prix de sortie et la raison de sortie sont inchangés",
          closed_after.get("exit_price") == 4.26e-6
          and closed_after.get("exit_reason") == "stop_loss")
    check("le statut reste « closed » et la date de clôture intacte",
          closed_after.get("status") == "closed"
          and closed_after.get("closed_at") == 1_792_410_781_378)
    check("l'enregistrement fermé est identique, octet pour octet",
          closed_after == closed_before)

    moved = next(p for p in store.positions if p["address"] == GM_A)
    check("la position ouverte, elle, a bien été réévaluée (le test n'est pas vide)",
          moved.get("current_price") == 1.05e-6
          and float(moved.get("pnl_percent") or 0) > 0,
          f"prix={moved.get('current_price')} · {moved.get('pnl_percent')}%")

    section("contrôle : c'est bien le statut qui gèle le PnL, pas la valeur elle-même")
    # La protection tient désormais à deux endroits : `_manage_loop` ne lit que les
    # positions ouvertes, et `_revalue` refuse de son côté tout enregistrement
    # fermé. La même boucle en passant **toutes** les positions ne réécrit donc
    # plus rien — ce qui, seul, ne prouverait pas que le chiffre gelé n'est pas
    # gelé « par nature ». Le témoin ci-dessous lève le doute : même
    # enregistrement, même tick, seul le statut change, et le PnL est bien
    # réécrit. C'est donc le statut qui protège, et non le hasard des valeurs.
    temoin = copy.deepcopy(closed_before)
    temoin["status"] = "open"
    store.positions = [temoin]
    await engine._revalue(temoin, quote)
    check("avec un statut « open », le même tick réécrit bel et bien le PnL",
          temoin.get("pnl_usd") != -1.59,
          f"deviendrait pnl_usd={temoin.get('pnl_usd')} "
          f"({temoin.get('pnl_percent')}%)")

    store.positions = [copy.deepcopy(closed_before), position(GM_A, "GM")]
    for pos in store.positions:
        await engine._revalue(pos, quote)
    unfiltered = next(p for p in store.positions if p["address"] == SONG_A)
    check("la boucle non filtrée ne touche plus au trade fermé (garde-fou de `_revalue`)",
          unfiltered.get("pnl_usd") == -1.59 and unfiltered == closed_before,
          f"pnl_usd={unfiltered.get('pnl_usd')} "
          f"({unfiltered.get('pnl_percent')}%)")
    check("`_revalue` refuse explicitement les positions fermées dans son code",
          'pos.get("status") == "closed"' in source_of(engine._revalue))

    section("le PnL réalisé du tableau de bord est celui, figé, des trades fermés")
    store.positions = [copy.deepcopy(closed_before), position(GM_A, "GM")]
    stats = store.recompute_stats()
    check("le réalisé du mode réel vient des positions fermées non papier",
          abs(float(stats.get("realized_pnl") or 0) - (-1.59)) < 1e-9,
          f"realized_pnl={stats.get('realized_pnl')}")
    check("les trades fermés sont comptés, l'ouvert ne l'est pas comme réalisé",
          stats.get("trades") == 1 and stats.get("open_count") == 1,
          f"trades={stats.get('trades')} ouvertes={stats.get('open_count')}")


asyncio.run(section4())


# ==================================================== 5. une seule ligne d'achat
title("5) Un achat papier n'écrit plus qu'UNE ligne d'achat")


async def _fake_quote(*, input_mint, output_mint, amount, slippage_bps=3000, timeout=20.0):
    price = 1.0e-4
    if output_mint == SOL_MINT:
        out = int(round(int(amount) / 1e6 * price * 1e9))
    else:
        out = int(round((int(amount) / 1e9) / price * 1e6))
    return {"inAmount": str(amount), "outAmount": str(out), "priceImpactPct": "0.01",
            "routePlan": [{"swapInfo": {"label": "Pump.fun"}}]}


async def _fake_build(**kw):
    return {"swapTransaction": "FAUSSE"}


async def _fake_sim_funding(signed, *, sig_verify=True):
    """La simulation refuse **faute de SOL** : c'est le cas de tous les achats
    papier de ce portefeuille, et c'est lui qui produisait la seconde ligne."""
    return {"err": {"InstructionError": [0, {"Custom": 1}]},
            "logs": ["Transfer: insufficient lamports 4849152, need 100000000"]}


async def _fake_decimals(mint):
    return 6


def _fake_wallet_address():
    return engine.wallet


SOL_MINT = "So11111111111111111111111111111111111111112"


async def section5() -> None:
    jupiter.quote = _fake_quote
    jupiter.swap_transaction = _fake_build
    solana.sign_only = lambda tx: tx
    solana.simulate_signed = _fake_sim_funding
    solana.mint_decimals = _fake_decimals
    solana.wallet_address = _fake_wallet_address

    with SettingsGuard():
        store.positions = []
        store.logs.clear()
        config._settings["paper_trading"] = True
        config._settings["trade_amount_sol"] = 0.1
        config._settings["slippage"] = 30
        config._settings["priority_fee"] = 0.00002
        config._settings["execution_backend"] = "jupiter"
        engine.wallet = "VerifWallet" + "1" * 32

        res = await engine._paper_buy({"address": FRESH, "symbol": "PAP",
                                       "name": "P", "logo": "", "price_usd": 1e-6})
        check("l'achat papier est exécuté", res.get("executed") is True,
              f"raison={res.get('reason')!r}")

        buys = logs_of("buy")
        notes = logs_of("note")
        check("exactement UNE ligne d'action « buy » pour un achat", len(buys) == 1,
              f"{len(buys)} ligne(s) : {[b['message'][:60] for b in buys]}")
        check("la ligne d'achat est bien celle de l'achat",
              len(buys) == 1 and "ACHAT PAPIER PAP" in buys[0]["message"])
        check("la note de financement n'est plus comptée comme un achat",
              len(notes) == 1 and "ACHAT" not in notes[0]["message"],
              notes[0]["message"] if notes else "aucune note")
        check("la note se lit comme la suite du MÊME achat",
              notes and notes[0]["message"].startswith("↳ même achat"),
              notes[0]["message"] if notes else "")
        check("aucune autre ligne d'action d'achat ou de vente",
              logs_of("sell") == [])

        pos = store.find_position_by_address(FRESH) or {}
        check("le remplissage reste honnêtement marqué « non validé »",
              pos.get("paper_funding_only") is True
              and pos.get("paper_simulated") is False,
              f"funding_only={pos.get('paper_funding_only')} "
              f"simulated={pos.get('paper_simulated')}")

        section("la source ne peut plus produire l'ancienne ligne trompeuse")
        src = pathlib.Path(ROOT / "backend" / "trader.py").read_text(encoding="utf-8")
        check("l'ancien message « Simulation refusée faute de SOL » a disparu",
              "Simulation refusée faute de SOL" not in src)
        check("la note de financement porte l'action « note », jamais l'action du swap",
              'level="warn", token=token_addr, action="note",' in src)


asyncio.run(section5())


# ==================================================== 6. l'écran ne peut plus confondre
title("6) Deux contrats différents portant le même nom ne se ressemblent plus")


def short_addr(addr: str) -> str:
    """La règle de `app.js`, transcrite : 6 caractères de tête, et la queue
    **avant** le suffixe constant « pump » pour ces contrats-là."""
    if not addr:
        return "—"
    if len(addr) <= 9:
        return addr
    tail = addr[-8:-4] if addr.endswith("pump") else addr[-3:]
    return addr[:6] + "\u2026" + tail


def section6() -> None:
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    needle = 'class="token-addr" title='
    n_titles = app.count(needle)

    check("les deux « Songgang » réels se distinguent",
          short_addr(SONG_A) != short_addr(SONG_B),
          f"{short_addr(SONG_A)}  vs  {short_addr(SONG_B)}")
    check("les « GM » réels se distinguent aussi",
          short_addr(GM_A) != short_addr(GM_B),
          f"{short_addr(GM_A)}  vs  {short_addr(GM_B)}")
    heads = {a[:3] for a in (SONG_A, SONG_B, GM_A, GM_B)}
    check("l'ancienne forme ne gardait que 3 caractères de tête et une queue identique",
          SONG_A[-3:] == SONG_B[-3:] == GM_A[-3:] == "ump" and len(heads) == 4,
          f"queues communes : {SONG_A[-3:]} ; têtes distinctes : {sorted(heads)}")
    check("`shortAddr` implémente bien cette règle",
          "s.endsWith('pump') ? s.slice(-8, -4) : s.slice(-3)" in app
          and "s.slice(0, 6) + '…'" in app)
    check("l'adresse complète est disponible en infobulle",
          n_titles >= 3, f"{n_titles} emplacements")
    check("un trade fermé s'affiche comme terminé, avec sa raison",
          '`Fermée · ${p.exit_reason' in app)
    check("le tableau montre le prix de sortie d'un trade fermé",
          "p.status === 'closed' && p.exit_price ? p.exit_price" in app)
    check("le PnL affiché d'un trade fermé est le réalisé net de frais",
          "Réalisé net de tous les frais de transaction" in app)
    check("le réglage existe dans le panneau de configuration",
          "['snipe_once_per_token', 'Un seul achat par token" in app)
    check("le bouton de snipe manuel annonce la règle en infobulle",
          "Un contrat déjà traité est refusé : un seul achat par token." in app)
    check("un refus « déjà traité » s'affiche avec sa phrase, sans « Token rejeté »",
          "j.reason === 'already-traded'" in app)
    check("un second clic pendant l'analyse est annoncé comme tel",
          "j.reason === 'in-progress'" in app
          and "Snipe déjà en cours" in app)
    check("le bouton est désactivé pendant la requête (double clic impossible)",
          "if (btn && btn.disabled) return;" in app
          and "finally { if (btn) btn.disabled = false; }" in app)
    check("le snipe manuel annonce une vérification, pas une analyse déjà lancée",
          "toast('Vérification de ' + shortAddr(address) + '…')" in app)
    check("la provenance d'un remplissage non validé est expliquée à l'écran",
          "la simulation a été refusée faute de SOL" in app
          and "paper_funding_only" in app)


section6()


# ==================================================== 7. hermétisme
title("7) Hermétisme et réglages intacts")


def section7() -> None:
    check("les écritures du magasin sont toutes interceptées (aucun octet écrit)",
          len(write_calls) > 0,
          f"{len(write_calls)} écriture(s) retenue(s), le disque n'a rien reçu")
    check("les chemins interceptés sont bien ceux de `data/`",
          all("data" in w for w in write_calls) and
          {str(state_mod.POSITIONS_FILE), str(state_mod.LOGS_FILE)}
          .issubset(set(write_calls)),
          ", ".join(sorted({pathlib.Path(w).name for w in write_calls})))
    after = hashlib.sha256(SETTINGS_FILE.read_bytes()).hexdigest()
    check("`data/settings.json` est inchangé (aucun réglage modifié)",
          after == settings_before)


section7()


# ---------------------------------------------------------------- restauration
bot_mod.store = store
bot_mod.analyze = saved["analyze"]
bot_mod.trader = saved["trader"]
solana.wallet_address = saved["wallet_address"]
solana.sign_only = saved["sign_only"]
solana.simulate_signed = saved["simulate"]
solana.mint_decimals = saved["mint_decimals"]
solana.sign_and_send = saved["send"]
jupiter.quote = saved["quote"]
jupiter.swap_transaction = saved["build"]

print(f"\n{'=' * 78}")
print(f"RÉSULTAT : {passed} OK, {len(fails)} échec(s)")
if fails:
    for f in fails:
        print(f"  - {f}")
print(f"{'=' * 78}")
sys.exit(1 if fails else 0)
