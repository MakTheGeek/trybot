r"""Fermer une position à la main : la confirmation, la part vendue, et les refus.

    .\venv\Scripts\python.exe tools\verify_close.py

Pourquoi ce harnais existe
--------------------------
Le bouton existait déjà, mais il vendait **100 % de la position en un clic**, sans
confirmation : sur une position réelle, c'est exactement le genre de clic qu'on
regrette. Et rien ne permettait de sortir à moitié, alors qu'une sortie partielle
est une chose ordinaire.

Le journal a montré un second point faible, réel celui-là : une vente a échoué sur
un **HTTP 429 de Jupiter** (« Too many requests ») et la position est restée
ouverte. Une vente refusée doit donc le dire **à l'endroit du clic**, pas dans une
notification qui a déjà disparu quand on relit le tableau.

Ce qu'il prouve
---------------
1. les refus, sur la route réelle : identifiant inconnu, position déjà fermée —
   avec un message qui dit *pourquoi* (« déjà fermée ») et pas un calcul
   d'arrondi (« le montant vaut zéro ») ;
2. une vente **partielle** : la position reste ouverte, les jetons sont divisés,
   le produit de la vente s'accumule, et le PnL continue de suivre le prix ;
3. une vente **totale** : la position se ferme et son PnL réalisé est écrit ;
4. les **bornes du pourcentage** : 0 et négatif refusés, au-delà de 100 ramené à
   100, une valeur non numérique ne provoque pas de vente accidentelle ;
5. l'**invariant de sécurité**, dans les deux sens : une position papier se ferme
   en virtuel même si le mode papier est éteint et sans wallet ; une position
   réelle sans wallet est refusée et reste ouverte ;
6. une vente **refusée** (le cas 429) laisse la position strictement intacte et
   l'écrit dans le journal ;
7. l'**interface** : c'est bien la confirmation qui déclenche la vente, les trois
   parts 25 / 50 / 100 sont proposées, et le refus s'affiche dans la boîte de
   dialogue avec la mention « la position est inchangée ».

Aucune écriture disque : `state._write` est remplacé par un compteur, les appels
réseau sont remplacés, et `sign_and_send` lève si on l'appelle.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
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
DECIMALS = 6
# Prix de vente simulé, en SOL par jeton. Choisi pour que l'aller-retour soit
# gagnant : 15 000 000 jetons achetés 0,1 SOL se revendent 0,18 SOL.
UNIT_SOL = 1.2e-8

PAPER_ADDR = "Paper" + "1" * 39
REAL_ADDR = "Real" + "2" * 39

SETTINGS_FILE = ROOT / "data" / "settings.json"
settings_before = hashlib.sha256(SETTINGS_FILE.read_bytes()).hexdigest()


# ============================================ le magasin, sans disque
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

engine = trader_mod.trader
saved = {
    "quote": jupiter.quote,
    "wallet_address": solana.wallet_address,
    "send": solana.sign_and_send,
    "refresh": engine.refresh_wallet,
    "price_for": engine._price_for,
    "dexscreener": trader_mod.dexscreener,
    "wallet": engine.wallet,
}


class SettingsGuard:
    def __enter__(self):
        self.saved = copy.deepcopy(config._settings)
        return self

    def __exit__(self, *exc):
        config._settings.clear()
        config._settings.update(self.saved)
        return False


# ------------------------------------------------------------- doubles

async def _fake_quote(*, input_mint, output_mint, amount, slippage_bps=3000, timeout=20.0):
    """Un devis qui vend au prix `UNIT_SOL` et achète au même prix."""
    if output_mint == SOL_MINT:                     # vente : jetons -> SOL
        out = int(round(int(amount) / (10 ** DECIMALS) * UNIT_SOL * LAMPORTS))
    else:                                           # achat : SOL -> jetons
        out = int(round((int(amount) / LAMPORTS) / UNIT_SOL * (10 ** DECIMALS)))
    return {"inAmount": str(amount), "outAmount": str(out), "priceImpactPct": "0.012",
            "routePlan": [{"swapInfo": {"label": "Pump.fun"}}]}


async def _quote_rate_limited(**kw):
    raise RuntimeError('Jupiter HTTP 429: {"code":429,"message":"[API Gateway] Too many requests"}')


async def _no_wallet():
    return None


def _fake_wallet_address():
    return engine.wallet


class _FakeDex:
    async def token(self, address, ttl=1.0):
        return {"price_usd": 1.44e-6}      # 1,2e-8 SOL × 120 $/SOL


def _send_forbidden(*args, **kwargs):
    """Le harnais ne signe rien : si ceci est atteint, il n'est plus hermétique."""
    raise AssertionError("sign_and_send a été appelé — le harnais devait rester hors ligne")


# --- installation des doubles : plus aucun accès réseau à partir d'ici -------
jupiter.quote = _fake_quote
solana.wallet_address = _fake_wallet_address
solana.sign_and_send = _send_forbidden
trader_mod.dexscreener = _FakeDex()


def _position(address: str, symbol: str, *, paper: bool, pos_id: str) -> dict:
    """Une position ouverte, cohérente : 15 M de jetons achetés 0,1 SOL."""
    return {
        "id": pos_id,
        "address": address,
        "chain": "sol",
        "symbol": symbol,
        "name": symbol,
        "logo": "",
        "status": "open",
        "tokens": 15_000_000.0,
        "tokens_bought": 15_000_000.0,
        "decimals": DECIMALS,
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
        "pnl_usd": 0.0,
        "pnl_percent": 0.0,
        "opened_at": 1_792_000_000_000,
        "paper": paper,
    }


def only(address: str) -> dict:
    return next(p for p in store.positions if p["address"] == address)


def logs(action: str) -> list[str]:
    return [str(e.get("message")) for e in store.logs if e.get("action") == action]


# ============================================ 1. les refus
title("1) La route refuse proprement, et dit pourquoi")


async def section1() -> None:
    store.positions = [_position(PAPER_ADDR, "PAP", paper=True, pos_id="pos_pap")]
    cible = _position(REAL_ADDR, "REEL", paper=False, pos_id="pos_reel")
    cible["status"] = "closed"
    cible["exit_reason"] = "take_profit"
    cible["tokens"] = 0.0
    store.positions.append(cible)

    r = await bot_mod.bot.close_position("pos_inexistant", 100.0)
    check("un identifiant inconnu est refusé", r.get("ok") is False
          and "introuvable" in str(r.get("error")), str(r))

    r2 = await bot_mod.bot.close_position("pos_reel", 100.0)
    check("une position déjà fermée est refusée", r2.get("ok") is False)
    check("le refus dit « déjà fermée » et non un calcul d'arrondi",
          "déjà fermée" in str(r2.get("error"))
          and "arrondit" not in str(r2.get("error")), str(r2.get("error")))
    check("le refus rappelle la raison de la clôture",
          "take_profit" in str(r2.get("error")), str(r2.get("error")))
    check("aucun ordre réseau n'a été tenté pour ce refus", True)


asyncio.run(section1())


# ============================================ 2. la vente partielle
title("2) Une vente partielle laisse la position ouverte")


async def section2() -> None:
    engine.wallet = "VerifWallet"
    engine._price_for = _fake_price
    with SettingsGuard():
        config._settings["paper_trading"] = True
        config._settings["slippage"] = 30
        config._settings["priority_fee"] = 0.00002
        store.positions = [_position(PAPER_ADDR, "PAP", paper=True, pos_id="pos_pap")]
        store.logs.clear()

        r = await bot_mod.bot.close_position("pos_pap", 50.0)
        p = only(PAPER_ADDR)
        check("la vente partielle est exécutée", r.get("ok") is True, str(r))
        check("la position reste OUVERTE", p.get("status") == "open", str(p.get("status")))
        check("les jetons sont divisés par deux", abs(float(p.get("tokens")) - 7_500_000.0) < 1,
              f"tokens={p.get('tokens')}")
        check("le coût engagé suit la même proportion",
              abs(float(p.get("cost_usd")) - 7.5) < 0.01, f"cost_usd={p.get('cost_usd')}")
        check("le produit réel de la vente est mémorisé",
              float(p.get("proceeds_sol") or 0) > 0, f"proceeds={p.get('proceeds_sol')}")
        check("les frais de vente sont mémorisés",
              float(p.get("sell_fees_sol") or 0) > 0, f"frais={p.get('sell_fees_sol')}")
        check("la valeur d'origine du trade est conservée pour le rendement final",
              float(p.get("cost_usd_total")) == 15.0 and float(p.get("tokens_bought")) == 15_000_000.0,
              f"total={p.get('cost_usd_total')} · jetons={p.get('tokens_bought')}")
        check("le PnL reste suivi (il n'est pas figé)", p.get("pnl_percent") is not None)

        section("une seconde vente solde le reste")
        # Le store rend l'objet lui-même, pas une copie : `p` et la position
        # stockée ne font qu'un, donc il faut photographier avant, sinon la
        # comparaison porte sur deux fois le même dictionnaire.
        apres_1re = copy.deepcopy(p)
        r2 = await bot_mod.bot.close_position("pos_pap", 100.0)
        p2 = only(PAPER_ADDR)
        check("la seconde vente est exécutée", r2.get("ok") is True, str(r2))
        check("la position est maintenant fermée", p2.get("status") == "closed",
              str(p2.get("status")))
        attendu = round(float(r.get("sol_received") or 0) + float(r2.get("sol_received") or 0), 9)
        check("le produit enregistré couvre les DEUX ventes",
              abs(float(p2.get("proceeds_sol") or 0) - attendu) < 1e-9
              and float(p2.get("proceeds_sol") or 0)
              > float(apres_1re.get("proceeds_sol") or 0),
              f"proceeds={p2.get('proceeds_sol')} · attendu={attendu} "
              f"(1re vente seule={apres_1re.get('proceeds_sol')})")
        check("le PnL réalisé est donc positif, il compte la première vente aussi",
              float(p2.get("pnl_usd") or 0) > 0, f"pnl_usd={p2.get('pnl_usd')}")
        check("la vente est tracée comme manuelle dans le journal",
              any("(manual)" in m for m in logs("sell")),
              next((m[:80] for m in logs("sell")), "aucune ligne"))


async def _fake_price(address: str) -> float:
    return 1.44e-6


asyncio.run(section2())


# ============================================ 3. la vente totale
title("3) Une vente totale ferme et fige le PnL réalisé")


async def section3() -> None:
    engine.wallet = "VerifWallet"
    engine._price_for = _fake_price
    with SettingsGuard():
        config._settings["paper_trading"] = True
        store.positions = [_position(PAPER_ADDR, "PAP", paper=True, pos_id="pos_pap3")]
        store.logs.clear()
        r = await bot_mod.bot.close_position("pos_pap3", 100.0)
        p = only(PAPER_ADDR)
        check("la vente totale est exécutée", r.get("ok") is True, str(r))
        check("le statut est « closed »", p.get("status") == "closed")
        check("le prix de sortie est écrit", float(p.get("exit_price") or 0) > 0,
              f"exit_price={p.get('exit_price')}")
        check("la raison de sortie est « manual »", p.get("exit_reason") == "manual",
              str(p.get("exit_reason")))
        check("la date de clôture est écrite", bool(p.get("closed_at")))
        check("le PnL réalisé est POSITIF (vendu 0,18 SOL pour 0,1 engagé)",
              float(p.get("pnl_usd") or 0) > 0, f"pnl_usd={p.get('pnl_usd')}")
        check("les frais totaux sont inclus dans le PnL",
              float(p.get("fees_sol") or 0) > 0, f"fees_sol={p.get('fees_sol')}")
        check("les jetons sont à zéro", float(p.get("tokens") or 0) == 0.0)

        section("et le PnL ne bouge plus : deux relectures identiques")
        snap = copy.deepcopy(p)
        for _ in range(3):
            await engine._revalue(p, {"price_usd": 9.9e-6, "market_cap": 99_000.0})
        check("après trois ticks de prix, l'enregistrement fermé est identique",
              only(PAPER_ADDR) == snap)
        check("la position fermée sort de la liste suivie par le gestionnaire",
              all(x["address"] != PAPER_ADDR for x in store.open_positions()),
              f"{len(store.open_positions())} position(s) encore suivie(s)")


asyncio.run(section3())


# ============================================ 4. les bornes du pourcentage
title("4) Le pourcentage est borné : aucun dump accidentel")


async def section4() -> None:
    engine.wallet = "VerifWallet"
    engine._price_for = _fake_price
    with SettingsGuard():
        config._settings["paper_trading"] = True

        async def essai(pct, label, attendu_execute, pos_id):
            store.positions = [_position(PAPER_ADDR, "PAP", paper=True, pos_id=pos_id)]
            store.logs.clear()
            res = await bot_mod.bot.close_position(pos_id, pct)
            p = only(PAPER_ADDR)
            reste_ouverte = p.get("status") == "open"
            check(label, bool(res.get("ok")) is attendu_execute,
                  f"ok={res.get('ok')} · {str(res.get('reason'))[:70]}")
            return p, reste_ouverte

        _, ouverte = await essai(0.0, "0 % est refusé et ne vend rien", False, "pct0")
        check("  → la position reste ouverte", ouverte)
        _, ouverte = await essai(-5.0, "un pourcentage négatif est refusé", False, "pctn")
        check("  → la position reste ouverte", ouverte)

        p, _ = await essai(150.0, "150 % est ramené à 100 % (vendu, pas ignoré)", True, "pct150")
        check("  → et la position est fermée", p.get("status") == "closed")
        p, _ = await essai("abc", "une valeur non numérique ne provoque pas de vente partielle",
                           True, "pctabc")
        check("  → elle vaut 100 % : la position est fermée", p.get("status") == "closed")


asyncio.run(section4())


# ============================================ 5. l'invariant de sécurité
title("5) Papier reste papier, réel reste réel")


async def section5() -> None:
    engine.refresh_wallet = _no_wallet
    with SettingsGuard():
        section("une position PAPIER se ferme en virtuel même si le mode papier est éteint")
        config._settings["paper_trading"] = False
        engine.wallet = ""
        engine._price_for = _fake_price
        store.positions = [_position(PAPER_ADDR, "PAP", paper=True, pos_id="inv_pap")]
        r = await bot_mod.bot.close_position("inv_pap", 100.0)
        p = only(PAPER_ADDR)
        check("la vente papier passe sans wallet", r.get("ok") is True, str(r))
        check("elle est marquée papier", p.get("paper") is True and r.get("paper") is True)
        check("la position est fermée en virtuel", p.get("status") == "closed")

        section("une position RÉELLE sans wallet est refusée et reste ouverte")
        config._settings["paper_trading"] = True
        engine.wallet = ""
        store.positions = [_position(REAL_ADDR, "REEL", paper=False, pos_id="inv_reel")]
        r2 = await bot_mod.bot.close_position("inv_reel", 100.0)
        p2 = only(REAL_ADDR)
        check("la vente est refusée", r2.get("ok") is False, str(r2))
        check("la raison est explicite", "wallet" in str(r2.get("reason", "")).lower(),
              str(r2.get("reason")))
        check("la position réelle reste OUVERTE", p2.get("status") == "open",
              str(p2.get("status")))
        check("aucun PnL n'a été écrit", float(p2.get("pnl_usd") or 0) == 0.0)


asyncio.run(section5())


# ============================================ 6. la vente refusée (429)
title("6) Une vente refusée laisse la position strictement intacte")


async def section6() -> None:
    engine.wallet = "VerifWallet"
    engine._price_for = _fake_price
    jupiter.quote = _quote_rate_limited
    with SettingsGuard():
        config._settings["paper_trading"] = True
        store.positions = [_position(PAPER_ADDR, "PAP", paper=True, pos_id="rate")]
        avant = copy.deepcopy(only(PAPER_ADDR))
        store.logs.clear()

        r = await bot_mod.bot.close_position("rate", 100.0)
        apres = only(PAPER_ADDR)
        check("la vente est refusée (le marché ne répond pas)", r.get("ok") is False, str(r))
        check("la raison dit que Jupiter n'a pas répondu (panne, pas marché vide)",
              "n'a pas répondu" in str(r.get("reason"))
              and "réessayez" in str(r.get("reason")),
              str(r.get("reason"))[:110])
        check("la position est INTACTE (statut, jetons, PnL)",
              apres.get("status") == "open"
              and apres.get("tokens") == avant.get("tokens")
              and apres.get("pnl_usd") == avant.get("pnl_usd"),
              f"statut={apres.get('status')} jetons={apres.get('tokens')}")
        check("le refus est écrit dans le journal, cause comprise",
              any("reste ouverte" in m and "429" in m for m in logs("sell")),
              next((m[:110] for m in logs("sell")), "aucune ligne"))
        check("aucun PnL réalisé n'a été écrit", not apres.get("exit_price"))
        check("aucun compteur « aucune route » n'est avancé par un 429",
              not apres.get("no_route_failures"), str(apres.get("no_route_failures")))

        section("et la vente repasse dès que le marché répond")
        jupiter.quote = _fake_quote
        r2 = await bot_mod.bot.close_position("rate", 100.0)
        check("le second essai ferme la position", r2.get("ok") is True, str(r2))
        check("elle est bien fermée cette fois", only(PAPER_ADDR).get("status") == "closed")


asyncio.run(section6())


# ============================================ 7. l'interface
title("7) L'interface : c'est la confirmation qui vend")


def section7() -> None:
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
    start = app.find("function closePosition(")
    end = app.find("function closeDialogHtml(")
    bloc = app[start:end] if start >= 0 and end > start else ""

    check("le clic ouvre une confirmation, il ne vend pas", bool(bloc) and "fetch(" not in bloc,
          f"{len(bloc)} caractères entre les deux fonctions")
    check("la boîte propose les trois parts 25 / 50 / 100", "[25, 50, 100]" in app)
    check("un bouton de confirmation explicite existe", 'id="close-confirm"' in app)
    check("le bouton d'annulation existe", 'id="close-cancel"' in app)
    check("la vente n'est déclenchée que depuis la confirmation",
          "close-confirm').onclick = () => doClose(" in app)
    check("le refus s'affiche dans la boîte, pas seulement en notification",
          'id="close-error"' in app and "La position est inchangée." in app)
    check("le texte suit la part choisie (total / partiel)",
          "la position reste <b>" in app and "est figé" in app)
    check("la phrase de vente partielle est d'un seul tenant (aucun artefact de concaténation)",
          "${rest} % restants, " in app and '" + "restants' not in app,
          "la phrase 25 / 50 % est écrite dans un seul gabarit")
    check("papier et réel sont annoncés différemment",
          "aucun ordre n'est signé ni envoyé" in app and "signe et diffuse" in app)
    check("les deux tableaux gardent leur bouton de fermeture",
          app.count("data-close=") >= 2)
    check("les infobulles annoncent la confirmation et la vente partielle",
          app.count("confirmation, vente partielle possible") == 2,
          f"{app.count('confirmation, vente partielle possible')} infobulle(s)")
    check("le bouton est rouge en mode réel, vert en papier",
          ".btn-primary.danger" in css and "close-pct.is-on" in css)
    check("le backend refuse une position déjà fermée avant tout devis",
          'if pos.get("status") != "open"' in
          (ROOT / "backend" / "bot.py").read_text(encoding="utf-8"))

    # ---- le clic doit ATTEINDRE closePosition.
    # Le bouton « Fermer » est rendu **dans** une ligne `<tr data-select>` : la
    # ligne est cliquable pour charger le graphique du token. Le délégué de clic
    # parcourt ses sélecteurs dans l'ordre du code, donc si `[data-select]` est
    # testé avant `[data-close]`, le ✕ ne ferme rien — il change simplement le
    # token sélectionné, sans un mot. C'était le cas: les deux boutons ne
    # faisaient rien. On prouve ici le piège **et** l'ordre qui le contourne.
    marks, pos_ = [], 0
    while True:
        i = app.find('<tr data-select="${p.address}">', pos_)
        if i < 0:
            break
        marks.append(i)
        pos_ = i + 1

    def inside_row(mark: int, needle: str) -> bool:
        end = app.find("</tr>", mark)
        return needle in app[mark:end]

    check("le piège existe bien : les boutons « Fermer » sont DANS une ligne cliquable",
          len(marks) >= 2 and all(inside_row(i, "data-close=") for i in marks),
          f"{len(marks)} ligne(s) « data-select » porteuse(s) d'un bouton")
    check("le bouton « TP/SL » est dans la même ligne cliquable",
          bool(marks) and inside_row(marks[0], "data-tp="))

    ev_start = app.find("function bindEvents(")
    ev_end = app.find("document.getElementById('cfg-save')", ev_start)
    events = app[ev_start:ev_end] if 0 <= ev_start < ev_end else ""
    i_close = events.find("closest('[data-close]')")
    i_tp = events.find("closest('[data-tp]')")
    i_select = events.find("closest('[data-select]')")
    i_untrack = events.find("closest('[data-feed-untrack]')")
    check("le délégué de clic est bien celui qu'on lit", bool(events),
          f"{len(events)} caractères entre bindEvents et cfg-save")
    check("« Fermer » est testé AVANT la ligne cliquable — sans quoi le ✕ ne ferait rien",
          i_close >= 0 and i_select > i_close,
          f"data-close@{i_close} · data-select@{i_select}")
    check("« TP/SL » est testé avant la ligne cliquable lui aussi",
          i_tp >= 0 and i_select > i_tp, f"data-tp@{i_tp} · data-select@{i_select}")
    check("« Ne plus suivre » garde aussi sa priorité (même piège, déjà traité)",
          i_untrack >= 0 and i_select > i_untrack,
          f"data-feed-untrack@{i_untrack} · data-select@{i_select}")

    # Le panneau « Positions ouvertes » ne montre que 8 lignes : il doit dire où
    # sont les autres, sinon l'utilisateur conclut qu'elles n'existent plus.
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    check("un emplacement est prévu sous le tableau pour les positions non affichées",
          'id="positions-more"' in html)
    check("la note a un style à elle", ".panel-note" in css)
    check("la note compte réellement les lignes qui manquent",
          "'positions-more'" in app and "open.length - 8" in app)
    check("la note renvoie vers l'onglet où tout est fermable",
          "toutes</b> sont dans l'onglet <b>Positions" in app)

    # Une liquidation sans route ne doit jamais ressembler à une vente.
    check("la ligne fermée porte un badge « prix non exécutable »",
          "prix non exécutable</span>" in app and "p.exit_unpriced" in app)
    check("l'infobulle dit que le prix n'était PAS exécutable et qu'aucune vente n'a eu lieu",
          "qui n'était PAS exécutable" in app and "aucune transaction n'a eu lieu" in app)
    check("la ligne ouverte affiche les tentatives sans route",
          "p.no_route_failures" in app and "aucune route ×" in app)


section7()


# ============================================ 8. une ouverte reste joignable
title("8) Toute position ouverte est joignable depuis l'écran")


def section8() -> None:
    # Le tableau de bord ne reçoit qu'un instantané plafonné. Si une position
    # ouverte tombe hors du plafond, elle n'existe plus pour le navigateur : on
    # ne peut ni la voir ni la fermer. Avec 34 ouvertes sur 139 positions, c'est
    # exactement ce qui se passait — le plafond gardait les 60 plus récentes et
    # 23 positions ouvertes étaient invisibles.
    st = fresh_store()
    historiques = []
    for i in range(70):
        p = _position(f"Ferme{i:03d}", f"C{i}", paper=True, pos_id=f"pos_c{i:03d}")
        p["status"] = "closed"
        p["exit_reason"] = "take-profit"
        historiques.append(p)
    ouvertes = [_position(f"Ouverte{i:03d}", f"O{i}", paper=True, pos_id=f"pos_o{i:03d}")
                for i in range(34)]
    st.positions = historiques + ouvertes          # les 34 ouvertes sont en QUEUE
    snap = st.snapshot()
    open_in_snap = [p for p in snap["positions"] if p.get("status") == "open"]
    check("l'instantané reste plafonné (le tableau de bord ne reçoit pas tout)",
          len(snap["positions"]) == state_mod.Store.POSITIONS_IN_SNAPSHOT,
          f"{len(snap['positions'])} positions")
    check("TOUTES les positions ouvertes y sont, malgré le plafond",
          len(open_in_snap) == 34, f"{len(open_in_snap)} / 34")
    check("elles passent en premier : la plus ancienne n'est plus reléguée",
          all(p.get("status") == "open" for p in snap["positions"][:34])
          and snap["positions"][34].get("status") != "open")
    check("le reste du plafond est occupé par l'historique des clôturées",
          len([p for p in snap["positions"] if p.get("status") != "open"]) == 26,
          f"{state_mod.Store.POSITIONS_IN_SNAPSHOT - 34} attendues")

    only_closed = fresh_store()
    only_closed.positions = [
        dict(_position(f"Clos{i:03d}", f"K{i}", paper=True, pos_id=f"pos_k{i:03d}"), status="closed")
        for i in range(200)
    ]
    check("sans position ouverte, le plafond garde son ancien comportement",
          len(only_closed.snapshot()["positions"]) == state_mod.Store.POSITIONS_IN_SNAPSHOT)


section8()


# ============================================ 9. aucune route de vente
title("9) Aucune route de vente : soldée une fois, au dernier prix, et dit")


async def _quote_no_route(**kw):
    raise RuntimeError('Jupiter HTTP 400: {"error":"No routes found",'
                       '"errorCode":"COULD_NOT_FIND_ANY_ROUTE"}')


def section9() -> None:
    """A drained pool pays nothing: the sale can never happen.

    The exit rule fires on every tick, so the bot used to ask for ever (measured
    on SOOTS: 34 identical failures). After a few consecutive *no-route* answers
    a paper position is closed at the last known price — once — and the record
    says that price was not executable. A real position is never written off.
    """
    async def run() -> None:
        saved_quote = jupiter.quote
        saved_balance = solana.token_ui_balance
        saved_wallet = engine.wallet
        saved_sol = store.runtime.get("sol_price")
        store.runtime["sol_price"] = 120.0
        engine._no_route.clear()
        try:
            seuil = trader_mod.UNPRICED_GIVE_UP
            p = _position(PAPER_ADDR, "VIDE", paper=True, pos_id="pos_vide")
            p["current_price"] = 1.44e-6
            store.positions = [p]
            jupiter.quote = _quote_no_route

            section("règle automatique : les tentatives sont comptées, la position reste ouverte")
            for n in range(1, seuil):
                r = await engine.sell(p, percent=100.0, reason="stop_loss")
                cur = only(PAPER_ADDR)
                check(f"tentative {n}/{seuil} : la vente échoue proprement",
                      r.get("executed") is False, str(r.get("reason"))[:80])
                check(f"tentative {n}/{seuil} : la position reste OUVERTE",
                      cur.get("status") == "open", str(cur.get("status")))
                check(f"tentative {n}/{seuil} : le compteur est écrit sur la position",
                      cur.get("no_route_failures") == n,
                      str(cur.get("no_route_failures")))
                check(f"tentative {n}/{seuil} : aucune liquidation prématurée",
                      not cur.get("exit_unpriced"))

            section("la tentative suivante solde au dernier prix connu, une seule fois")
            r = await engine.sell(p, percent=100.0, reason="stop_loss")
            fin = only(PAPER_ADDR)
            check(f"la {seuil}e tentative ferme la position",
                  fin.get("status") == "closed", str(fin.get("status")))
            check("la réponse dit que ce n'est pas une vente",
                  r.get("executed") is True and r.get("liquidation") == "unpriced",
                  str({k: r.get(k) for k in ("executed", "liquidation", "reason")})[:120])
            check("la position porte « prix non exécutable »",
                  fin.get("exit_unpriced") is True
                  and fin.get("exit_price_source") == "dexscreener",
                  f"{fin.get('exit_unpriced')} / {fin.get('exit_price_source')}")
            check("la note dit en toutes lettres que le prix n'était pas exécutable",
                  "non exécutable" in str(fin.get("exit_note")),
                  str(fin.get("exit_note")))
            check("le nombre de tentatives est conservé sur la position",
                  fin.get("no_route_failures") == seuil,
                  str(fin.get("no_route_failures")))

            fee = trader_mod.fee_model.sell_fee_sol(config.all())
            attendu = round(15_000_000.0 * 1.44e-6 / 120.0 - fee, 9)
            check("le produit est calculé au dernier prix connu",
                  abs(float(fin.get("proceeds_sol") or 0) - attendu) < 1e-7,
                  f"proceeds={fin.get('proceeds_sol')} attendu≈{attendu}")
            check("le prix de sortie est bien le dernier prix Dexscreener",
                  abs(float(fin.get("exit_price") or 0) - 1.44e-6) / 1.44e-6 < 0.001,
                  str(fin.get("exit_price")))
            check("le PnL réalisé est écrit, frais compris",
                  fin.get("pnl_usd") is not None and float(fin.get("fees_sol") or 0) > 0,
                  f"pnl={fin.get('pnl_usd')} frais={fin.get('fees_sol')}")
            check("le journal dit « liquidée au dernier prix connu » et « NON exécutable »",
                  any("liquidée au dernier prix connu" in m and "NON exécutable" in m
                      and "VIDE" in m for m in logs("sell")),
                  next((m[:100] for m in logs("sell") if "VIDE" in m), "aucune ligne"))
            check("un seul message de liquidation pour cette position, pas un par tick",
                  sum(1 for m in logs("sell")
                      if "liquidée au dernier prix connu" in m and "VIDE" in m) == 1)

            # --- un clic n'est pas une règle automatique -------------------
            # A rule re-fires every few seconds, so counting protects it from a
            # loop. A click happens once: making the user click four times would
            # read as a broken button (which is exactly how it was reported).
            section("un clic explicite ferme en UNE fois, sans attendre les quatre refus")
            p_clic = _position(PAPER_ADDR, "CLIC", paper=True, pos_id="pos_clic")
            p_clic["current_price"] = 1.44e-6
            store.positions = [p_clic]
            engine._no_route.clear()
            r = await bot_mod.bot.close_position("pos_clic", 100.0)
            clic = only(PAPER_ADDR)
            check("le premier clic ferme déjà la position",
                  clic.get("status") == "closed", str(clic.get("status")))
            check("la réponse dit que ce n'est pas une vente",
                  r.get("ok") is True and r.get("liquidation") == "unpriced",
                  str({k: r.get(k) for k in ("ok", "liquidation")}))
            check("elle reste étiquetée « prix non exécutable »",
                  clic.get("exit_unpriced") is True
                  and clic.get("exit_price_source") == "dexscreener")
            check("une seule tentative est comptée, pas quatre",
                  clic.get("no_route_failures") == 1,
                  str(clic.get("no_route_failures")))
            ligne = next((m for m in logs("sell")
                          if "liquidée au dernier prix connu" in m and "CLIC" in m), "")
            check("le journal dit que c'est à la demande de l'utilisateur",
                  "à votre demande" in ligne, ligne[:110])
            check("le prix du journal est celui écrit sur la position",
                  f"${float(clic.get('exit_price')):.10f}" in ligne,
                  f"exit_price={clic.get('exit_price')} · {ligne[:80]}")

            section("une vente partielle sur un pool vide est refusée, avec la consigne")
            p_part = _position(PAPER_ADDR, "PART", paper=True, pos_id="pos_part")
            store.positions = [p_part]
            engine._no_route.clear()
            r = await bot_mod.bot.close_position("pos_part", 25.0)
            part = only(PAPER_ADDR)
            check("le quart n'est pas vendu", r.get("ok") is False,
                  str(r.get("reason"))[:80])
            check("la position reste ouverte et entière",
                  part.get("status") == "open"
                  and abs(float(part.get("tokens") or 0) - 15_000_000.0) < 1.0,
                  f"{part.get('status')} / {part.get('tokens')}")
            check("aucun compteur n'est avancé par ce refus",
                  not part.get("no_route_failures"), str(part.get("no_route_failures")))
            check("le message dit de fermer la totalité",
                  "Fermez la totalité" in str(r.get("reason")),
                  str(r.get("reason"))[:110])

            section("le PnL d'une liquidation ne bouge plus")
            avant = copy.deepcopy(fin)
            store.positions = [fin]
            await engine._revalue(fin, {"price_usd": 9e-6})
            apres = only(PAPER_ADDR)
            check("un tick de prix postérieur ne rouvre pas la position",
                  apres.get("status") == "closed" and apres.get("exit_unpriced") is True)
            check("son PnL reste identique, octet pour octet",
                  apres.get("pnl_usd") == avant.get("pnl_usd")
                  and apres.get("pnl_percent") == avant.get("pnl_percent"),
                  f"{apres.get('pnl_usd')} vs {avant.get('pnl_usd')}")

            section("une panne passagère ne compte pas")
            jupiter.quote = _quote_rate_limited
            p2 = _position(PAPER_ADDR, "RATE", paper=True, pos_id="pos_tempo")
            store.positions = [p2]
            for _ in range(trader_mod.UNPRICED_GIVE_UP + 2):
                await bot_mod.bot.close_position("pos_tempo", 100.0)
            tempo = only(PAPER_ADDR)
            check("un 429 répété ne remplit PAS le compteur",
                  not tempo.get("no_route_failures"),
                  str(tempo.get("no_route_failures")))
            check("et la position reste ouverte, sans prix inventé",
                  tempo.get("status") == "open" and not tempo.get("exit_unpriced"))
            check("le message dit que la panne est passagère, pas le marché",
                  any("réessayez dans un instant" in m for m in logs("sell")),
                  next((m[:110] for m in logs("sell") if "RATE" in m), "aucune ligne"))

            section("une position réelle n'est jamais soldée au dernier prix")
            reelle = _position(REAL_ADDR, "REEL", paper=False, pos_id="pos_reel_vide")
            store.positions = [reelle]

            async def _balance(wallet, mint):
                return (15_000_000.0, DECIMALS, 15_000_000 * 10 ** DECIMALS)

            engine.wallet = "Re" + "1" * 42
            solana.token_ui_balance = _balance
            jupiter.quote = _quote_no_route
            r2 = await bot_mod.bot.close_position("pos_reel_vide", 100.0)
            cur2 = only(REAL_ADDR)
            check("elle échoue franchement, sans inventer de vente",
                  r2.get("ok") is False, str(r2.get("reason"))[:90])
            check("elle reste OUVERTE, sans « prix non exécutable »",
                  cur2.get("status") == "open" and not cur2.get("exit_unpriced"))
            check("aucun compteur de liquidation n'est écrit sur une réelle",
                  not cur2.get("no_route_failures"))

            section("et le compteur repart de zéro quand une route revient")
            jupiter.quote = _quote_no_route
            p3 = _position(PAPER_ADDR, "RETOUR", paper=True, pos_id="pos_retour")
            store.positions = [p3]
            for _ in range(seuil - 1):
                await engine.sell(p3, percent=100.0, reason="trailing_stop")
            check("deux échecs sont bien comptés",
                  only(PAPER_ADDR).get("no_route_failures") == seuil - 1)
            jupiter.quote = _fake_quote
            r3 = await engine.sell(p3, percent=100.0, reason="trailing_stop")
            fin3 = only(PAPER_ADDR)
            check("la vente passe dès qu'une route existe",
                  r3.get("executed") is True and fin3.get("status") == "closed",
                  str(r3.get("reason")))
            check("la position n'est pas marquée « non exécutable »",
                  not fin3.get("exit_unpriced"))
            check("le compteur est remis à zéro",
                  fin3.get("no_route_failures") == 0, str(fin3.get("no_route_failures")))
        finally:
            jupiter.quote = saved_quote
            solana.token_ui_balance = saved_balance
            engine.wallet = saved_wallet
            if saved_sol is None:
                store.runtime.pop("sol_price", None)
            else:
                store.runtime["sol_price"] = saved_sol
            engine._no_route.clear()

    asyncio.run(run())


section9()


# ============================================ 10. hermétisme
title("10) Hermétisme et réglages intacts")


def section10() -> None:
    check("les écritures du magasin sont interceptées (aucun octet écrit)",
          len(write_calls) > 0,
          f"{len(write_calls)} écriture(s) retenue(s), le disque n'a rien reçu")
    check("`data/settings.json` est inchangé",
          hashlib.sha256(SETTINGS_FILE.read_bytes()).hexdigest() == settings_before)


section10()


# ---------------------------------------------------------------- restauration
jupiter.quote = saved["quote"]
solana.wallet_address = saved["wallet_address"]
solana.sign_and_send = saved["send"]
engine.refresh_wallet = saved["refresh"]
engine._price_for = saved["price_for"]
trader_mod.dexscreener = saved["dexscreener"]
engine.wallet = saved["wallet"]

print(f"\n{'=' * 78}")
print(f"RÉSULTAT : {passed} OK, {len(fails)} échec(s)")
if fails:
    for f in fails:
        print(f"  - {f}")
print(f"{'=' * 78}")
sys.exit(1 if fails else 0)
