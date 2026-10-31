r"""Vérification fonctionnelle de chaque paramètre.

Le tableau de bord laisse régler des dizaines de paramètres (tous ceux de
`config.DEFAULT_SETTINGS`). Les enregistrer ne prouve rien :
il faut montrer que **modifier la valeur change le comportement du code qui
l'utilise**. C'est ce que fait ce harnais, paramètre par paramètre.

    .\venv\Scripts\python.exe tools\verify_params.py

Ce qu'il prouve, et comment
---------------------------
* il lit le **catalogue** (`config.DEFAULT_SETTINGS`) et cherche, pour chaque clé,
  le module du backend qui la consomme — un paramètre rangé mais jamais lu
  « fonctionne » en apparence seulement ;
* il **écrit puis relit** chaque clé dans un fichier de réglages *temporaire*
  (jamais `data/settings.json`) : une valeur perdue au rechargement est un
  réglage qui se réinitialise tout seul ;
* il éprouve les **bornes de validation** de chaque clé numérique, une par une ;
* il branche des **doubles** (devis, construction, simulation, GMGN, Dexscreener)
  pour piloter les vraies fonctions — `analyze`, `prefilter`, `exit_decision`,
  `_revalue`, `_process`, `buy`, `_jupiter_swap` — et constate le changement.

Rien n'est envoyé sur la chaîne : `sign_and_send` lève une exception si on
l'appelle, et aucune écriture ne touche `data/`.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import alertfilter, config  # noqa: E402
from backend import analyzer as analyzer_mod  # noqa: E402
from backend import bot as bot_mod  # noqa: E402
from backend import jupiter, fees  # noqa: E402
from backend import solana_rpc as solana  # noqa: E402
from backend import state as state_mod  # noqa: E402
from backend import trader as trader_mod  # noqa: E402
from backend.clock import clock  # noqa: E402

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


SOL_MINT = "So11111111111111111111111111111111111111112"


class SettingsGuard:
    """Snapshot / restore ``config._settings`` without ever touching the file."""

    def __enter__(self):
        self.saved = copy.deepcopy(config._settings)
        return self

    def __exit__(self, *exc):
        config._settings.clear()
        config._settings.update(self.saved)
        return False


# =========================================================== 1. catalogue
title("1) Catalogue : chaque paramètre est-il réellement lu par le code ?")

defaults = config.DEFAULT_SETTINGS
print(f"  {len(defaults)} clés déclarées dans DEFAULT_SETTINGS")

sources = {p.name: p.read_text(encoding="utf-8")
           for p in sorted((ROOT / "backend").glob("*.py"))}
consumers = {name: text for name, text in sources.items() if name != "config.py"}

orphans: list[str] = []
for key in defaults:
    pattern = re.compile(r"""["'\[]""" + re.escape(key) + r"""["'\]]""")
    readers = [name for name, text in consumers.items() if pattern.search(text)]
    if not readers:
        orphans.append(key)
check(f"les {len(defaults)} paramètres sont lus par au moins un module du backend",
      not orphans, f"jamais lus : {orphans}" if orphans else
      f"{len(defaults)}/{len(defaults)} branchés")

stored = json.loads((ROOT / "data" / "settings.json").read_text(encoding="utf-8"))
unknown = sorted(set(stored) - set(defaults))
check("aucune clé inconnue dans data/settings.json", not unknown, f"inconnues : {unknown}")
check(f"les {len(stored)} clés enregistrées existent au catalogue",
      not unknown and len(stored) <= len(defaults))

bad_default = []
for key, value in defaults.items():
    try:
        cleaned = config.validate({key: value})
    except ValueError as exc:
        bad_default.append(f"{key}: {exc}")
        continue
    if key not in cleaned or type(cleaned[key]) is not type(value):
        bad_default.append(f"{key}: {cleaned.get(key)!r} ({type(value).__name__})")
check("chaque valeur par défaut passe la validation et garde son type",
      not bad_default, "; ".join(bad_default))

# =========================================================== 2. persistance
title("2) Persistance : une valeur écrite est-elle relue telle quelle ?")

ALTERNATES: dict[str, object] = {
    "channels": ["autre_canal"],
    "live_trading": False,
    "paper_trading": True,
    "trade_amount_sol": 0.25,
    "tp_percent": 33.0,
    "sl_percent": 7.5,
    "trailing_percent": 4.0,
    "trailing_enabled": False,
    "auto_snipe": False,
    "snipe_once_per_token": False,
    "slippage": 12.0,
    "priority_fee": 0.00007,
    "anti_mev": False,
    "max_concurrent_positions": 3,
    "max_fee_percent": 1.5,
    "execution_backend": "gmgn",
    "solana_rpc_url": "https://exemple.invalid",
    "solana_rpc_fallback_url": "https://exemple.invalid",
    "max_market_cap": 12345.0,
    "max_age_minutes": 7.0,
    "max_top10_percent": 42.0,
    "require_mc_gt_volume": False,
    "require_more_buyers": False,
    "buyer_seller_window": "1m",
    "require_website": False,
    "require_social": False,
    "require_description": True,
    "require_social_engagement": False,
    "max_social_dup": 9,
    "require_kol": False,
    "require_smart_wallet": False,
    "alert_prefilter": False,
    "price_poll_seconds": 3.0,
    "price_feed_enabled": False,
    "price_feed_seconds": 1.5,
    "price_feed_calls": 9,
    "secrets_local_only": True,
    "chain": "sol",
}
missing_alt = sorted(set(defaults) - set(ALTERNATES))
check("chaque clé du catalogue a une valeur alternative de test",
      not missing_alt, f"sans alternative : {missing_alt}")

tmp_file = pathlib.Path(tempfile.gettempdir()) / "solsnipbot-verify-settings.json"
saved_file = config.SETTINGS_FILE
saved_settings = copy.deepcopy(config._settings)
config.SETTINGS_FILE = tmp_file

lost: list[str] = []
for key, alt in ALTERNATES.items():
    expect = config.validate({key: alt})[key]
    got = config.update({key: alt})[key]
    on_disk = json.loads(tmp_file.read_text(encoding="utf-8"))
    config._settings.clear()
    config._settings.update(config.DEFAULT_SETTINGS)
    reloaded = config.load()[key]
    if not (got == expect == on_disk[key] == reloaded):
        lost.append(f"{key}: {got!r} / disque {on_disk[key]!r} / relu {reloaded!r}")
check(f"les {len(ALTERNATES)} paramètres survivent à écriture + relecture",
      not lost, "; ".join(lost))
check("le fichier de test est bien un fichier temporaire",
      tmp_file != saved_file and str(saved_file).endswith("settings.json"))

# Un réglage corrompu ne doit jamais devenir un réglage par défaut silencieux.
try:
    tmp_file.write_text(json.dumps({"sl_percent": "abc", "tp_percent": 25.0}),
                        encoding="utf-8")
    loaded = config.load()
    check("une valeur corrompue retombe sur sa valeur par défaut (et est signalée)",
          loaded["sl_percent"] == config.DEFAULT_SETTINGS["sl_percent"]
          and loaded["tp_percent"] == 25.0
          and config.LAST_DROPPED == ["sl_percent"],
          f"repli={loaded['sl_percent']} abandonnés={config.LAST_DROPPED}")
finally:
    config.SETTINGS_FILE = saved_file
    config._settings.clear()
    config._settings.update(saved_settings)
    if tmp_file.exists():
        tmp_file.unlink()

# =========================================================== 3. bornes
title("3) Validation : les bornes de chaque paramètre numérique")

print(f"  {len(config.NUMERIC_RANGES)} paramètres numériques, bornes testées une à une")
boundary_bad: list[str] = []
for key, (low, high) in config.NUMERIC_RANGES.items():
    span = max(abs(high - low), 1.0)
    under = low - max(span * 1e-3, 1e-6)
    over = high + max(span * 1e-3, 1e-6)
    # `chain` et les fenêtres n'ont pas de bornes : tous ceux-ci en ont.
    def ok(value):
        try:
            config.validate({key: value})
            return True
        except ValueError:
            return False
    cases = [
        (f"borne basse {low:g}", low, True),
        (f"borne haute {high:g}", high, True),
        (f"sous la borne ({under:g})", under, False),
        (f"au-dessus de la borne ({over:g})", over, False),
        ("vide (None)", None, False),
        ("chaîne vide", "", False),
        ("texte", "abc", False),
        ("NaN", float("nan"), False),
        ("infini", float("inf"), False),
        ("-infini", float("-inf"), False),
    ]
    for label, value, want in cases:
        if ok(value) != want:
            boundary_bad.append(f"{key} · {label}")
check(f"{len(config.NUMERIC_RANGES)} × 10 cas de bornes", not boundary_bad,
      "; ".join(boundary_bad[:6]) + ("…" if len(boundary_bad) > 6 else ""))

int_keys = [k for k, v in config.DEFAULT_SETTINGS.items() if isinstance(v, int)
            and not isinstance(v, bool)]
bad_int = [k for k in int_keys
           if type(config.validate({k: float(config.NUMERIC_RANGES[k][0]) + 0.4})[k])
           is not int]
check(f"les clés entières le restent après arrondi ({', '.join(int_keys)})",
      not bad_int, f"non entières : {bad_int}")

bool_case = [
    (True, True), (False, False), (1, True), (0, False),
    ("oui", True), ("vrai", True), ("true", True), ("on", True), ("1", True),
    ("non", False), ("faux", False), ("false", False), ("off", False), ("", False),
]
bad_bool = [repr(v) for v, want in bool_case
            if config.validate({"live_trading": v})["live_trading"] is not want]
check(f"{len(bool_case)} écritures booléennes interprétées correctement",
      not bad_bool, f"fausses : {bad_bool}")
refused_bools = []
for value in (None, "peut-être", [], {}):
    try:
        config.validate({"live_trading": value})
        refused_bools.append(repr(value))
    except ValueError:
        pass
check("un booléen illisible est refusé, jamais lu comme « faux »",
      not refused_bools, f"acceptés : {refused_bools}")

bad_select = []
for key, options in config.SELECT_OPTIONS.items():
    for value in options:
        try:
            config.validate({key: value})
        except ValueError:
            bad_select.append(f"{key}={value} refusé")
    try:
        config.validate({key: "__inexistant__"})
        bad_select.append(f"{key}=__inexistant__ accepté")
    except ValueError:
        pass
check(f"{len(config.SELECT_OPTIONS)} listes de choix : chaque option valide, toute autre refusée",
      not bad_select, "; ".join(bad_select))

bad_url = []
for key in ("solana_rpc_url", "solana_rpc_fallback_url"):
    for value, want in (("https://ok.invalid", True), ("http://ok.invalid", True),
                        ("ok.invalid", False), ("", "fallback" in key),
                        ("ftp://ok.invalid", False)):
        try:
            config.validate({key: value})
            got = True
        except ValueError:
            got = False
        if got != want:
            bad_url.append(f"{key}={value!r}")
check("les URL de nœud exigent un schéma http(s) (le secours peut être vide)",
      not bad_url, "; ".join(bad_url))

ch = config.validate({"channels": "https://t.me/gmgnsignals, pumpdotfunalert"})["channels"]
check("les canaux acceptent un lien, une virgule, et sont normalisés",
      ch == ["gmgnsignals", "pumpdotfunalert"], f"{ch}")
try:
    config.validate({"channels": " , "})
    refused_channels = False
except ValueError:
    refused_channels = True
check("une liste de canaux vide est refusée (aucune écoute silencieuse)",
      refused_channels)
check("une clé inconnue est ignorée, pas enregistrée",
      config.validate({"clé_bidon": 1}) == {})

# =========================================================== 4. sorties
title("4) TP / SL / trailing : la valeur réglée change bien la décision")

exit_decision = trader_mod.exit_decision


def decide(price, peak, entry=100.0, tp=0.0, sl=0.0, trail=0.0, armed=False):
    pnl = (price / entry - 1) * 100.0
    return exit_decision(price=price, peak=peak, pnl_pct=pnl, tp_percent=tp,
                         sl_percent=sl, trailing_percent=trail,
                         trailing_active=armed or price > entry)


section("take-profit : sort au-delà du seuil réglé")
tp_bad = []
for tp, want in ((5, "take_profit"), (20, "take_profit"), (22, ""), (0, "")):
    got = decide(121.0, 121.0, tp=tp)          # marché à +21 %
    if got != want:
        tp_bad.append(f"tp={tp} -> {got or 'aucune'}")
check("TP 5 / 20 / 22 / 0 face à +21 % du marché", not tp_bad, "; ".join(tp_bad))
check("un prix pile au seuil puis un cheveu au-dessus : la sortie part au tick suivant",
      decide(120.0, 120.0, tp=20) == "" and decide(120.5, 120.5, tp=20) == "take_profit",
      "la comparaison est flottante, comme dans _revalue")

section("stop-loss : sort au-delà du seuil réglé")
sl_bad = []
for sl, want in ((10, "stop_loss"), (20, "stop_loss"), (22, ""), (0, "")):
    got = decide(79.0, 100.0, sl=sl)            # marché à −21 %
    if got != want:
        sl_bad.append(f"sl={sl} -> {got or 'aucune'}")
check("SL 10 / 20 / 22 / 0 face à −21 % du marché", not sl_bad, "; ".join(sl_bad))

section("trailing stop : distance réglée, et armement en profit")
trail_bad = []
for trail, want in ((5, "trailing_stop"), (10, "trailing_stop"), (12, ""), (0, "")):
    got = decide(178.0, 200.0, tp=999, trail=trail, armed=True)
    if got != want:
        trail_bad.append(f"trail={trail} -> {got or 'aucune'}")
check("pic 200 → 178 selon la distance réglée", not trail_bad, "; ".join(trail_bad))
check("jamais de trailing tant que le trade n'est pas en profit",
      decide(95.0, 100.0, tp=999, trail=10, armed=False) == "",
      "prix 95, pic 100, non armé")
check("le trailing armé suit le pic, pas l'entrée",
      decide(181.0, 200.0, tp=999, trail=10, armed=True) == ""
      and decide(179.0, 200.0, tp=999, trail=10, armed=True) == "trailing_stop")
check("le stop-loss prime sur le trailing",
      decide(70.0, 110.0, tp=999, sl=25, trail=10, armed=True) == "stop_loss")
check("le take-profit prime sur le trailing",
      decide(160.0, 200.0, tp=50, trail=10, armed=True) == "take_profit")

# ==================================================== 5. réglages -> position
title("5) TP / SL / trailing : du réglage jusqu'à la sortie réelle du gestionnaire")

engine = trader_mod.trader
saved_methods = {
    "quote": jupiter.quote, "build": jupiter.swap_transaction,
    "sign_only": solana.sign_only, "simulate": solana.simulate_signed,
    "send": solana.sign_and_send, "decimals": solana.mint_decimals,
    "sell": engine.sell, "confirm": engine._confirm_exit,
    "log": trader_mod.store.log, "add_position": trader_mod.store.add_position,
    "update_position": trader_mod.store.update_position,
    "push": trader_mod.store.push_position_update,
    "get_position": trader_mod.store.get_position,
    "new_id": trader_mod.store.new_position_id,
    "recompute": trader_mod.store.recompute_stats,
    "broadcast": trader_mod.store.broadcast,
}
saved_runtime = copy.deepcopy(state_mod.store.runtime)
sent: list[int] = []


async def _no_send(*a, **kw):
    sent.append(1)
    raise AssertionError("aucune transaction ne doit être envoyée par cette vérification")


async def _fake_quote(*, input_mint, output_mint, amount, slippage_bps=3000, timeout=20.0):
    price_sol = 1e-4                      # 1 token = 0,0001 SOL
    if output_mint == SOL_MINT:
        out = int(round(int(amount) / 1e6 * price_sol * 1e9))
    else:
        out = int(round((int(amount) / 1e9) / price_sol * 1e6))
    return {"inAmount": str(amount), "outAmount": str(out),
            "priceImpactPct": "0.01",
            "routePlan": [{"swapInfo": {"label": "Raydium"}}]}


async def _fake_build(**kw):
    return {"swapTransaction": "FAUSSE"}


async def _fake_sim(signed, *, sig_verify=True):
    return {"err": None, "unitsConsumed": 1000}


async def _fake_decimals(mint):
    return 6


async def _same_reason(pos, reason, price, peak, entry):
    return reason


registry: dict[str, dict] = {}
sell_calls: list[str] = []


async def _record_sell(position, *, percent=100.0, reason="manual"):
    sell_calls.append(reason)
    return {"executed": True, "reason": reason}


def _update_position(pos_id, patch, persist=True):
    if pos_id in registry:
        registry[pos_id].update(patch)


def _get_position(pos_id):
    return registry.get(pos_id)


jupiter.quote = _fake_quote
jupiter.swap_transaction = _fake_build
solana.sign_only = lambda tx: tx
solana.simulate_signed = _fake_sim
solana.sign_and_send = _no_send
solana.mint_decimals = _fake_decimals
engine.sell = _record_sell
engine._confirm_exit = _same_reason
trader_mod.store.log = lambda *a, **kw: None
trader_mod.store.add_position = lambda p: registry.__setitem__(p["id"], p)
trader_mod.store.update_position = _update_position
trader_mod.store.push_position_update = lambda p: None
trader_mod.store.get_position = _get_position
trader_mod.store.new_position_id = lambda: "verif"
trader_mod.store.recompute_stats = lambda: {}
trader_mod.store.broadcast = lambda *a, **kw: None
state_mod.store.runtime["sol_price"] = 150.0

async def lifecycle_check() -> None:
    """Le cycle complet : du réglage jusqu'à la sortie du gestionnaire."""
    try:
        with SettingsGuard():
            config._settings["paper_trading"] = True
            config._settings["trade_amount_sol"] = 0.1
            config._settings["tp_percent"] = 15.0
            config._settings["sl_percent"] = 25.0
            config._settings["trailing_percent"] = 12.0
            config._settings["trailing_enabled"] = True
            engine.wallet = "Verif" + "1" * 40

            buy = await engine.buy({"address": "Verif" + "2" * 40, "symbol": "VERIF",
                                    "name": "Vérification", "logo": "", "price_usd": 1e-6})
            pos = registry.get(buy.get("position_id") or "") or {}
            check("un achat papier s'exécute sans wallet réel", buy.get("executed") is True,
                  f"raison={buy.get('reason')!r}")
            check("le montant du trade vient bien de trade_amount_sol",
                  abs(float(pos.get("amount_sol") or 0) - 0.1) < 1e-12
                  and abs(float(pos.get("sol_committed") or 0) - 0.1) < 1e-12,
                  f"engagé={pos.get('sol_committed')} SOL")
            check("la position reçoit TP / SL / trailing réglés",
                  (float(pos.get("tp_percent")) == 15.0
                   and float(pos.get("sl_percent")) == 25.0
                   and float(pos.get("trailing_percent")) == 12.0
                   and pos.get("trailing_enabled") is True),
                  f"TP={pos.get('tp_percent')} SL={pos.get('sl_percent')} "
                  f"trail={pos.get('trailing_percent')} actif={pos.get('trailing_enabled')}")
            check("aucune transaction envoyée à l'achat", sent == [], f"envois={len(sent)}")

            entry = float(pos["entry_price"])

            async def drive(price: float) -> str:
                """Fait passer un tick au gestionnaire, comme le fait _manage_loop."""
                before = len(sell_calls)
                await engine._revalue(pos, {"price_usd": price})
                return sell_calls[before] if len(sell_calls) > before else ""

            section("le gestionnaire applique les seuils de la position")
            check("+10 % : on garde (TP réglé à +15 %)",
                  await drive(entry * 1.10) == "", f"prix {entry * 1.10:.3e}")
            check("+12 % : toujours sous le seuil, on garde",
                  await drive(entry * 1.12) == "")
            check("+20 % : take-profit déclenché",
                  await drive(entry * 1.20) == "take_profit")

            registry["verif"]["status"] = "open"
            pos["peak_price"] = entry
            pos["trailing_active"] = False
            sell_calls.clear()
            check("−26 % : stop-loss déclenché (SL 25 %)",
                  await drive(entry * 0.74) == "stop_loss")

            pos["peak_price"] = entry
            pos["trailing_active"] = False
            sell_calls.clear()
            check("−24 % : sous le SL mais dans les clous, on garde",
                  await drive(entry * 0.76) == "")

            section("trailing : armé en profit, sort à la distance réglée")
            # TP mis hors d'atteinte : c'est le trailing seul qui doit parler.
            pos["tp_percent"] = 999.0
            pos["peak_price"] = entry
            pos["trailing_active"] = False
            registry["verif"]["status"] = "open"
            sell_calls.clear()
            check("pic à +50 % : toujours tenu (12 % de marge = +32 %)",
                  await drive(entry * 1.50) == "")
            check("repli à +34 % : au-dessus de la marge, on garde",
                  await drive(entry * 1.34) == "")
            check("repli à +30 % : trailing stop déclenché",
                  await drive(entry * 1.30) == "trailing_stop")

            section("trailing désactivé : le repli ne déclenche rien")
            pos["trailing_enabled"] = False
            pos["peak_price"] = entry
            pos["trailing_active"] = False
            registry["verif"]["status"] = "open"
            sell_calls.clear()
            await drive(entry * 1.50)
            check("même parcours avec trailing_enabled = False",
                  await drive(entry * 1.30) == "")

            section("la sortie passe par le prix exécutable, jamais par l'affichage")
            source_revalue = inspect.getsource(trader_mod.Trader._revalue)
            check("_revalue confirme toute sortie par _confirm_exit avant de vendre",
                  "_confirm_exit" in source_revalue
                  and source_revalue.index("_confirm_exit") < source_revalue.index("await self.sell"),
                  "_confirm_exit appelé avant sell")
            check("une sortie annulée par le prix réel ne vend pas",
                  "if not exit_reason:" in source_revalue
                  and "return" in source_revalue.split("if not exit_reason:")[1]
                  .split("await self.sell")[0])
    finally:
        jupiter.quote = saved_methods["quote"]
        jupiter.swap_transaction = saved_methods["build"]
        solana.sign_only = saved_methods["sign_only"]
        solana.simulate_signed = saved_methods["simulate"]
        solana.sign_and_send = saved_methods["send"]
        solana.mint_decimals = saved_methods["decimals"]
        engine.sell = saved_methods["sell"]
        engine._confirm_exit = saved_methods["confirm"]
        trader_mod.store.log = saved_methods["log"]
        trader_mod.store.add_position = saved_methods["add_position"]
        trader_mod.store.update_position = saved_methods["update_position"]
        trader_mod.store.push_position_update = saved_methods["push"]
        trader_mod.store.get_position = saved_methods["get_position"]
        trader_mod.store.new_position_id = saved_methods["new_id"]
        trader_mod.store.recompute_stats = saved_methods["recompute"]
        trader_mod.store.broadcast = saved_methods["broadcast"]
        state_mod.store.runtime.clear()
        state_mod.store.runtime.update(saved_runtime)
        registry.clear()


# ==================================================== 6. filtres (analyzer)
# (le titre est imprimé par main(), pour rester dans l'ordre d'exécution)


class FakeGMGN:
    def __init__(self, info, security):
        self.info, self.security = info, security

    async def token_info(self, address, chain="sol"):
        return copy.deepcopy(self.info)

    async def token_security(self, address, chain="sol"):
        return copy.deepcopy(self.security)


class FakeDex:
    def __init__(self, data):
        self.data = data

    async def token(self, address, **kw):
        return copy.deepcopy(self.data)


NOW = clock.now_ts()
BASE_INFO = {
    "name": "Filtre", "symbol": "FILT", "logo": "",
    "total_supply": 1_000_000_000, "circulating_supply": 1_000_000_000,
    "max_supply": 1_000_000_000,
    "ath_price": 2e-6,
    "creation_timestamp": NOW - 120,
    "rug_ratio": 0.0, "website_dup": 0, "visiting_count": 120,
    "price": {"price": 1e-6, "volume_24h": 60_000,
              "buys_24h": 300, "sells_24h": 100,
              "buys_1m": 4, "sells_1m": 1},
    "stat": {"top_10_holder_rate": 0.10, "holder_count": 420,
             "twitter_dup": 1, "rug_ratio": 0.0},
    "wallet_tags_stat": {"renowned_wallets": 2, "smart_wallets": 3,
                         "sniper_wallets": 0},
    "link": {"website": "https://exemple.invalid", "twitter_username": "filt",
             "description": "un token de test"},
    "dev": {"creator_address": "Dev" + "1" * 40},
    "launchpad_platform": "pump.fun",
}
BASE_DEX = {"market_cap": 5_000, "volume_24h": 60_000, "liquidity_usd": 20_000,
            "buys": {"h24": 300}, "sells": {"h24": 100},
            "price_usd": 1e-6}
BASE_SEC = {"is_honeypot": 0, "top_10_holder_rate": 0.10}

saved_analyzer = {"gmgn": analyzer_mod.gmgn, "dex": analyzer_mod.dexscreener}


async def run_analyze(info=None, dex=None, security=None, settings=None):
    """Analyse le token synthétique, avec les vrais filtres du bot."""
    guard = SettingsGuard()
    guard.__enter__()
    if settings:
        config._settings.update(settings)
    analyzer_mod.gmgn = FakeGMGN(info or BASE_INFO, security or BASE_SEC)
    analyzer_mod.dexscreener = FakeDex(dex or BASE_DEX)
    try:
        return await analyzer_mod.analyze("Filtre" + "3" * 38, source="verify")
    finally:
        analyzer_mod.gmgn = saved_analyzer["gmgn"]
        analyzer_mod.dexscreener = saved_analyzer["dex"]
        guard.__exit__()


def verdict(report: dict, cid: str) -> bool | None:
    item = next((c for c in report.get("checks", []) if c["id"] == cid), None)
    return None if item is None else bool(item["passed"])


async def main() -> None:
    await lifecycle_check()

    title("6) Filtres : chaque critère réagit à son entrée et à son interrupteur")
    # Les sondes ci-dessous prouvent qu'un *mécanisme* fonctionne : elles forcent
    # l'interrupteur testé au lieu de lire la configuration du moment. Sans cela,
    # éteindre un critère dans le tableau de bord ferait échouer la suite alors
    # que le code est intact (constaté : 5 critères éteints -> 5 échecs), et
    # rallumer un critère pourrait la faire échouer dans l'autre sens. La fenêtre
    # acheteurs/vendeurs compte aussi : livrée à la configuration vivante (« 1h »),
    # la sonde lisait des compteurs qu'elle n'avait pas posés.
    ON = {"max_top10_percent": 30.0, "max_market_cap": 40_000.0,
          "max_age_minutes": 15.0, "require_mc_gt_volume": True,
          "require_more_buyers": True, "buyer_seller_window": "24h",
          "require_website": True,
          "require_social": True, "require_description": False,
          "require_social_engagement": True, "max_social_dup": 25,
          "require_kol": True, "require_smart_wallet": True}
    base = await run_analyze(settings=ON)
    check("un token conforme aux 4 règles est retenu", base["decision"] == "sniped",
          f"décision={base['decision']} échecs={base['failed_ids']}")
    check("le rapport porte bien les 10 critères + les frais",
          {c["id"] for c in base["checks"]} >=
          {"top10", "mc_vs_volume", "buyers", "links", "social_engagement", "kol",
           "smart", "market_cap", "age", "fees"} and len(base["checks"]) == 10,
          f"{len(base['checks'])} critères : {sorted(c['id'] for c in base['checks'])}")

    section("Concentration du top 10 (max_top10_percent)")
    hi = copy.deepcopy(BASE_INFO); hi["stat"]["top_10_holder_rate"] = 0.45
    r = await run_analyze(info=hi, settings=ON)
    check("45 % du top 10 -> rejet (règle actuelle : < 30 %)",
          verdict(r, "top10") is False and r["decision"] == "rejected")
    edge = copy.deepcopy(BASE_INFO); edge["stat"]["top_10_holder_rate"] = 0.30
    r = await run_analyze(info=edge, settings=ON)
    check("exactement 30 % -> rejet (la règle est stricte)",
          verdict(r, "top10") is False)
    r = await run_analyze(info=edge, settings={**ON, "max_top10_percent": 30.0000001})
    check("la borne vient du réglage, pas du code",
          verdict(r, "top10") is True)
    r = await run_analyze(info=hi, settings={**ON, "max_top10_percent": 90.0})
    check("relever max_top10_percent fait passer le même token",
          verdict(r, "top10") is True)

    section("MarketCap contre volume (require_mc_gt_volume)")
    mc = copy.deepcopy(BASE_DEX); mc["market_cap"] = 90_000
    r = await run_analyze(dex=mc, settings=ON)
    check("MC 90 k > volume 60 k -> rejet", verdict(r, "mc_vs_volume") is False)
    r = await run_analyze(dex=mc, settings={**ON, "require_mc_gt_volume": False})
    check("interrupteur éteint -> critère neutralisé", verdict(r, "mc_vs_volume") is True)

    section("Acheteurs contre vendeurs (require_more_buyers, buyer_seller_window)")
    se = copy.deepcopy(BASE_INFO); se["price"].update({"buys_24h": 50, "sells_24h": 500})
    r = await run_analyze(info=se, settings=ON)
    check("50 achats / 500 ventes -> rejet", verdict(r, "buyers") is False)
    r = await run_analyze(info=se, settings={**ON, "require_more_buyers": False})
    check("interrupteur éteint -> critère neutralisé", verdict(r, "buyers") is True)
    r = await run_analyze(info=se, settings={**ON, "buyer_seller_window": "1m"})
    check("la fenêtre choisie lit d'autres compteurs (1m : 4/1 -> pass)",
          verdict(r, "buyers") is True, f"fenêtre={r['price_window']}")
    r = await run_analyze(info=se, settings={**ON, "buyer_seller_window": "24h"})
    check("la fenêtre 24h relit bien 50/500 -> rejet", verdict(r, "buyers") is False)

    section("Liens (require_website, require_social, require_description)")
    no_site = copy.deepcopy(BASE_INFO); no_site["link"]["website"] = ""
    r = await run_analyze(info=no_site, settings=ON)
    check("sans site -> rejet", verdict(r, "links") is False)
    r = await run_analyze(info=no_site, settings={**ON, "require_website": False})
    check("require_website = False -> neutralisé", verdict(r, "links") is True)

    # Pour isoler l'exigence « réseaux sociaux », il faut retirer aussi le site :
    # `_socials()` compte le site parmi les liens (voir le constat ci-dessous).
    bare = copy.deepcopy(BASE_INFO)
    bare["link"] = {"website": "", "twitter_username": "", "description": "un token"}
    r = await run_analyze(info=bare, settings={**ON, "require_website": False})
    check("sans aucun réseau social -> rejet", verdict(r, "links") is False)
    r = await run_analyze(info=bare, settings={**ON, "require_website": False,
                                               "require_social": False})
    check("require_social = False -> neutralisé", verdict(r, "links") is True)
    r = await run_analyze(info=bare, settings=ON)
    check("sans site ni réseau -> rejet (les deux règles parlent)",
          verdict(r, "links") is False)

    site_only = copy.deepcopy(BASE_INFO)
    site_only["link"] = {"website": "https://exemple.invalid", "description": "d"}
    r = await run_analyze(info=site_only, settings=ON)
    check("CONSTAT : un site seul satisfait aussi l'exigence « réseaux sociaux »",
          verdict(r, "links") is True,
          "le site est compté parmi les liens sociaux — les deux règles ne sont "
          "pas indépendantes")

    no_desc = copy.deepcopy(BASE_INFO); no_desc["link"]["description"] = ""
    r = await run_analyze(info=no_desc, settings=ON)
    check("sans description, la règle étant éteinte par défaut -> pass",
          verdict(r, "links") is True)
    r = await run_analyze(info=no_desc, settings={**ON, "require_description": True})
    check("require_description = True -> rejet", verdict(r, "links") is False)

    section("Engagement social (require_social_engagement, max_social_dup)")
    dup = copy.deepcopy(BASE_INFO); dup["stat"]["twitter_dup"] = 900
    r = await run_analyze(info=dup, settings=ON)
    check("même visuel réutilisé 900 fois -> rejet", verdict(r, "social_engagement") is False)
    r = await run_analyze(info=dup, settings={**ON, "max_social_dup": 1000})
    check("le seuil max_social_dup relève la tolérance",
          verdict(r, "social_engagement") is True)
    r = await run_analyze(info=dup, settings={**ON, "require_social_engagement": False})
    check("interrupteur éteint -> neutralisé", verdict(r, "social_engagement") is True)

    section("KOL et smart wallets (require_kol, require_smart_wallet)")
    no_kol = copy.deepcopy(BASE_INFO); no_kol["wallet_tags_stat"]["renowned_wallets"] = 0
    r = await run_analyze(info=no_kol, settings=ON)
    check("aucun KOL -> rejet", verdict(r, "kol") is False)
    r = await run_analyze(info=no_kol, settings={**ON, "require_kol": False})
    check("require_kol = False -> neutralisé", verdict(r, "kol") is True)
    no_smart = copy.deepcopy(BASE_INFO); no_smart["wallet_tags_stat"]["smart_wallets"] = 0
    r = await run_analyze(info=no_smart, settings=ON)
    check("aucun smart wallet -> rejet", verdict(r, "smart") is False)
    r = await run_analyze(info=no_smart, settings={**ON, "require_smart_wallet": False})
    check("require_smart_wallet = False -> neutralisé", verdict(r, "smart") is True)

    section("Plafond de capitalisation ATH (max_market_cap)")
    pumped = copy.deepcopy(BASE_INFO); pumped["ath_price"] = 1e-4  # ATH 100 k
    r = await run_analyze(info=pumped, settings=ON)
    check("ATH à 100 k > plafond 40 k, même si le prix est retombé -> rejet",
          verdict(r, "market_cap") is False
          and r["ath_market_cap"] > r["market_cap"],
          f"ATH={r['ath_market_cap']:,.0f} vs actuel {r['market_cap']:,.0f}")
    r = await run_analyze(info=pumped, settings={**ON, "max_market_cap": 200_000.0})
    check("relever max_market_cap fait passer le même token",
          verdict(r, "market_cap") is True)
    fresh = copy.deepcopy(BASE_INFO); fresh["ath_price"] = 1.5e-6
    r = await run_analyze(info=fresh, settings=ON)
    check("un token jamais monté au-dessus du plafond passe",
          verdict(r, "market_cap") is True, f"ATH={r['ath_market_cap']:,.0f}")

    section("Âge (max_age_minutes)")
    old = copy.deepcopy(BASE_INFO); old["creation_timestamp"] = NOW - 3600
    r = await run_analyze(info=old, settings=ON)
    check("60 min > 15 min -> rejet", verdict(r, "age") is False,
          f"âge={r['age_minutes']:.1f} min")
    r = await run_analyze(info=old, settings={**ON, "max_age_minutes": 120.0})
    check("max_age_minutes = 120 -> pass", verdict(r, "age") is True)
    young = copy.deepcopy(BASE_INFO); young["creation_timestamp"] = NOW - 60
    r = await run_analyze(info=young, settings=ON)
    check("1 min -> pass", verdict(r, "age") is True, f"âge={r['age_minutes']:.1f} min")

    section("Frais (max_fee_percent) — le trade doit payer ses propres frais")
    r = await run_analyze(settings={"trade_amount_sol": 0.0005})
    check("un trade de 0,0005 SOL dont les frais dépassent 3 % -> rejet par « fees »",
          verdict(r, "fees") is False and r["decision"] == "rejected")
    r = await run_analyze(settings={"trade_amount_sol": 0.0005, "max_fee_percent": 0.0})
    check("max_fee_percent = 0 -> règle désactivée explicitement",
          verdict(r, "fees") is True)

    section("Honeypot — dernier verrou, évalué seulement si tout le reste passe")
    r = await run_analyze(security={"is_honeypot": 1, "top_10_holder_rate": 0.1},
                          settings=ON)
    check("pot de miel -> rejet, critère ajouté au rapport",
          r["decision"] == "rejected" and verdict(r, "honeypot") is False
          and "honeypot" in r["failed_ids"])
    r = await run_analyze(info=BASE_INFO, settings=ON)
    check("aucun critère honeypot quand le token est propre",
          verdict(r, "honeypot") is None and "honeypot" not in r["failed_ids"])

    section("Le seuil est-il lu au réglage, ou figé dans le code ?")
    frozen = []
    for key, cid in (("max_market_cap", "market_cap"), ("max_age_minutes", "age"),
                     ("max_top10_percent", "top10")):
        value = float(config.DEFAULT_SETTINGS[key])
        r = await run_analyze(settings={key: value * 0.5})
        token = next((c for c in r["checks"] if c["id"] == cid), None)
        if not token or abs(float(token.get("threshold") or 0) - value * 0.5) > 1e-9:
            frozen.append(key)
    check("le seuil rapporté est celui du réglage, pas une constante",
          not frozen, f"figés : {frozen}")

    # ==================================================== 7. pré-filtre
    title("7) Pré-filtre d'alerte : les 3 plafonds, sur le texte de l'alerte")

    alert = "🟢 $NEW (Nouveau)\nopen: 20 minutes ago\nTop 10: 45%\nMCP: $60k\nHolders: 120"
    settings_now = config.all()
    # Les seuils ci-dessous sont *ceux que ces contrôles décrivent* (30 %, 40 k,
    # 15 min) : les figer ici évite qu'un réglage resserré dans le tableau de bord
    # fasse échouer des contrôles qui parlent d'un autre plafond.
    ref = {**settings_now, "max_top10_percent": 30.0, "max_market_cap": 40_000.0,
           "max_age_minutes": 15.0}

    r = alertfilter.prefilter(alert, ref)
    check("30 % : une alerte à 45 % de top 10 est rejetée sans appel GMGN",
          r["reject"] is True and any(c["id"] == "top10" for c in r["checks"]),
          f"rejets={[c['id'] for c in r['checks']]}")
    r = alertfilter.prefilter(alert, {**ref, "max_top10_percent": 90.0,
                                      "max_age_minutes": 60.0, "max_market_cap": 1e9})
    check("en relevant les trois plafonds, la même alerte est laissée à l'analyse",
          r["reject"] is False and not r["checks"],
          f"rejets={[c['id'] for c in r['checks']]}")
    r = alertfilter.prefilter(alert, {**ref, "max_top10_percent": 90.0,
                                      "max_age_minutes": 60.0})
    check("le plafond de capitalisation rejette toujours à 60 k contre 40 k",
          r["reject"] is True and any(c["id"] == "market_cap" for c in r["checks"]))

    # Les nombres d'une alerte sont arrondis : le pré-filtre ne tranche qu'au-delà
    # d'une marge, sinon il rejetterait des tokens que l'analyse aurait gardés.
    r = alertfilter.prefilter("Top 10: 31%", {**ref, "max_market_cap": 1e9,
                                              "max_age_minutes": 0})
    check("31 % face à un plafond de 30 % ne tranche pas (marge de 2 points)",
          r["reject"] is False, "borderline laissé à l'analyse complète")
    r = alertfilter.prefilter("Top 10: 32%", {**ref, "max_market_cap": 1e9,
                                              "max_age_minutes": 0})
    check("32 % dépasse la marge -> rejet", r["reject"] is True)

    mc_alert = "MCP: $50k"
    r = alertfilter.prefilter(mc_alert, {**ref, "max_age_minutes": 0,
                                         "max_top10_percent": 100.0})
    check("une MC de 50 k face au plafond de 40 k -> rejet",
          r["reject"] is True and any(c["id"] == "market_cap" for c in r["checks"]))
    r = alertfilter.prefilter(mc_alert, {**ref, "max_age_minutes": 0,
                                         "max_top10_percent": 100.0,
                                         "max_market_cap": 100_000.0})
    check("le même texte passe avec un plafond de 100 k", r["reject"] is False)

    old_alert = "open: 2 hours ago"
    r = alertfilter.prefilter(old_alert, {**ref, "max_top10_percent": 100.0,
                                          "max_market_cap": 1e9})
    check("une alerte de 2 h face au plafond de 15 min -> rejet par l'âge",
          r["reject"] is True and any(c["id"] == "age" for c in r["checks"]))
    # Un appel qui attend son tour vieillit : l'âge de l'alerte n'est pas l'âge
    # du token au moment où on le regarde.
    waiting = "open: 14 minutes ago"
    r = alertfilter.prefilter(waiting, {**ref, "max_top10_percent": 100.0,
                                        "max_market_cap": 1e9})
    check("14 min face au plafond de 15 min -> laissé à l'analyse",
          r["reject"] is False, "juste sous le plafond")
    r = alertfilter.prefilter(waiting, {**ref, "max_top10_percent": 100.0,
                                        "max_market_cap": 1e9}, elapsed=120.0)
    check("après 2 min d'attente en file, le même token dépasse le plafond",
          r["reject"] is True and any(c["id"] == "age" for c in r["checks"]),
          "14 min + 2 min = 16 min > 15 min")

    parsed = alertfilter.parse_alert(alert)
    check("l'alerte est lue champ par champ (âge, top10, MC, holders, symbole)",
          parsed["age_seconds"] == 1200.0 and parsed["top10_pct"] == 45.0
          and parsed["market_cap"] == 60_000.0 and parsed["holders"] == 120
          and parsed["symbol"] == "NEW",
          f"{parsed['symbol']} · {parsed['age_seconds']}s · {parsed['top10_pct']}% · "
          f"${parsed['market_cap']:,.0f}")
    clean = "open: 2 minutes ago\nTop 10: 5%\nMCP: $20k"
    r = alertfilter.prefilter(clean, ref)
    check("une alerte irréprochable n'est jamais « acceptée » ici : elle part à l'analyse",
          r["reject"] is False and r["checks"] == [],
          "le pré-filtre ne peut que rejeter")
    report = alertfilter.build_report({"address": "X", "source": "telegram"}, 
                                      alertfilter.prefilter(alert, ref))
    check("un rejet de pré-filtre est enregistré comme un rejet, jamais un achat",
          report["decision"] == "rejected" and report["executed"] is False
          and report["prefiltered"] is True and report["failed_ids"])

    # ==================================================== 8. pilotage du bot
    title("8) Pilotage : live_trading, paper_trading, auto_snipe, max_concurrent_positions")

    saved_bot = {"analyze": bot_mod.analyze, "buy": bot_mod.trader.buy,
                 "log": state_mod.store.log, "add_call": state_mod.store.add_call,
                 "stats": state_mod.store.recompute_stats,
                 "cast": state_mod.store.broadcast,
                 "open": state_mod.store.open_positions}
    bot = bot_mod.bot
    calls_made: list[dict] = []

    async def fake_analyze(address, *, source="telegram", message="", channel=""):
        return {"address": address, "symbol": "PILOT", "name": "Pilotage", "logo": "",
                "decision": "sniped", "reasons": [], "failed_ids": [], "checks": [],
                "source": source, "channel": channel, "message": message,
                "ts": 0, "market_cap": 1000, "top10_pct": 5.0, "age_minutes": 1.0}

    async def fake_buy(report):
        calls_made.append(report)
        return {"executed": True, "position_id": "p1", "reason": ""}

    open_now: list[dict] = []
    bot_mod.analyze = fake_analyze
    bot_mod.trader.buy = fake_buy
    state_mod.store.log = lambda *a, **kw: None
    state_mod.store.add_call = lambda r: None
    state_mod.store.recompute_stats = lambda: {}
    state_mod.store.broadcast = lambda *a, **kw: None

    def open_positions(paper=None):
        # Même contrat que le vrai magasin : `paper=True` rend les positions
        # papier, `paper=False` les réelles, et les deux livres ne se mélangent pas.
        if paper is None:
            return list(open_now)
        return [p for p in open_now if bool(p.get("paper")) is bool(paper)]

    state_mod.store.open_positions = open_positions

    async def process(**settings):
        calls_made.clear()
        with SettingsGuard():
            config._settings.update(settings)
            return await bot._process({"address": "Pilot" + "4" * 40, "message": "",
                                       "channel": "verif", "source": job_source,
                                       "attempts": 0, "ts": 0})

    try:
        job_source = "telegram"
        r = await process(live_trading=True, auto_snipe=True, paper_trading=False,
                          max_concurrent_positions=5)
        check("analyse validée + automatique + réel -> achat déclenché",
              len(calls_made) == 1 and r["executed"] is True)

        r = await process(live_trading=True, auto_snipe=False, paper_trading=False,
                          max_concurrent_positions=5)
        check("auto_snipe = False -> analysé mais aucun achat",
              not calls_made and r["execution"]["reason"].startswith("Sniper automatique"))

        r = await process(live_trading=False, auto_snipe=True, paper_trading=False,
                          max_concurrent_positions=5)
        check("live_trading = False -> aucun ordre réel",
              not calls_made and "réel" in r["execution"]["reason"])

        r = await process(live_trading=False, auto_snipe=True, paper_trading=True,
                          max_concurrent_positions=5)
        check("paper_trading = True -> le trade part quand même (virtuel)",
              len(calls_made) == 1 and r["paper"] is True)

        r = await process(live_trading=False, auto_snipe=False, paper_trading=True,
                          max_concurrent_positions=5)
        check("paper_trading = True + auto_snipe = False -> toujours rien",
              not calls_made)

        job_source = "manual"
        r = await process(live_trading=True, auto_snipe=False, paper_trading=False,
                          max_concurrent_positions=5)
        check("un snipe manuel ignore auto_snipe mais pas live_trading",
              len(calls_made) == 1)
        job_source = "telegram"

        open_now.append({"paper": False})
        r = await process(live_trading=True, auto_snipe=True, paper_trading=False,
                          max_concurrent_positions=1)
        check("max_concurrent_positions = 1 avec une position ouverte -> refus motivé",
              not calls_made and "maximum" in r["execution"]["reason"].lower(),
              r["execution"]["reason"])
        open_now.clear()
        open_now.append({"paper": True})
        r = await process(live_trading=True, auto_snipe=True, paper_trading=False,
                          max_concurrent_positions=1)
        check("une position papier ne bloque pas un ordre réel (limites séparées)",
              len(calls_made) == 1)
        open_now.clear()

        r = await process(live_trading=True, auto_snipe=True, paper_trading=False,
                          max_concurrent_positions=2)
        check("relever max_concurrent_positions libère un achat", len(calls_made) == 1)

        async def rejected(address, *, source="telegram", message="", channel=""):
            return {"address": address, "symbol": "KO", "name": "KO", "logo": "",
                    "decision": "rejected", "reasons": ["test"], "failed_ids": ["top10"],
                    "checks": [], "source": source, "channel": channel,
                    "message": message, "ts": 0}

        bot_mod.analyze = rejected
        await process(live_trading=True, auto_snipe=True, paper_trading=False,
                      max_concurrent_positions=5)
        check("une analyse rejetée n'atteint jamais l'achat", not calls_made)
        bot_mod.analyze = fake_analyze
    finally:
        bot_mod.analyze = saved_bot["analyze"]
        bot_mod.trader.buy = saved_bot["buy"]
        state_mod.store.log = saved_bot["log"]
        state_mod.store.add_call = saved_bot["add_call"]
        state_mod.store.recompute_stats = saved_bot["stats"]
        state_mod.store.broadcast = saved_bot["cast"]
        state_mod.store.open_positions = saved_bot["open"]

    # ============================================ 9. réglages -> ordre
    title("9) slippage, priority_fee, anti_mev : jusqu'à l'ordre réellement construit")

    seen_quote: list[dict] = []
    seen_build: list[dict] = []

    async def spy_quote(*, input_mint, output_mint, amount, slippage_bps=3000, timeout=20.0):
        seen_quote.append({"amount": amount, "slippage_bps": slippage_bps})
        return {"inAmount": str(amount), "outAmount": "1", "priceImpactPct": "0.001",
                "routePlan": []}

    async def spy_build(**kw):
        seen_build.append(kw)
        return {"swapTransaction": "X"}

    saved_order = {"quote": jupiter.quote, "build": jupiter.swap_transaction,
                   "sign": solana.sign_only, "sim": solana.simulate_signed,
                   "send": solana.sign_and_send, "wallet": solana.wallet_address,
                   "log": trader_mod.store.log}
    jupiter.quote = spy_quote
    jupiter.swap_transaction = spy_build
    solana.sign_only = lambda tx: tx
    solana.simulate_signed = _fake_sim
    solana.sign_and_send = _no_send
    trader_mod.store.log = lambda *a, **kw: None
    solana.wallet_address = lambda: engine.wallet
    try:
        with SettingsGuard():
            config._settings.update({"slippage": 12.0, "priority_fee": 0.00007,
                                     "trade_amount_sol": 0.1, "execution_backend": "jupiter",
                                     "paper_trading": False})
            engine.wallet = "Ordre" + "5" * 40
            await engine._jupiter_swap(input_mint=SOL_MINT,
                                       output_mint="Ordre" + "6" * 40,
                                       amount=100_000_000, side="buy", dry_run=True)
        check("le montant du devis est celui du réglage trade_amount_sol",
              seen_quote and seen_quote[-1]["amount"] == 100_000_000,
              f"lamports={seen_quote[-1]['amount'] if seen_quote else '—'}")
        check("slippage = 12 % arrive au devis en points de base (1200)",
              seen_quote and seen_quote[-1]["slippage_bps"] == 1200,
              f"{seen_quote[-1]['slippage_bps'] if seen_quote else '—'} bps")
        check("priority_fee = 0,00007 SOL arrive à la construction (70 000 lamports)",
              seen_build and seen_build[-1].get("max_priority_lamports") == 70_000,
              f"{seen_build[-1].get('max_priority_lamports') if seen_build else '—'} lamports")

        section("plafond de priorité : jamais zéro, jamais plus que l'utilisateur")
        seen_build.clear()
        with SettingsGuard():
            config._settings.update({"priority_fee": 0.0, "slippage": 30})
            await engine._jupiter_swap(input_mint=SOL_MINT,
                                       output_mint="Ordre" + "7" * 40,
                                       amount=1_000_000, side="buy", dry_run=True)
        check("priority_fee = 0 est ramené au plancher de 1 000 lamports",
              seen_build and seen_build[-1]["max_priority_lamports"] == 1_000,
              f"{seen_build[-1]['max_priority_lamports'] if seen_build else '—'}")

        section("moteur gmgn : slippage, priority_fee et anti_mev dans l'ordre")
        order: list[dict] = []

        async def spy_swap(**kw):
            order.append(kw)
            return {"code": 0, "tx_hash": "hash", "_tokens": 1000.0, "price": 1.5e-5}

        saved_swap = trader_mod.gmgn.swap
        saved_key = config.gmgn_private_key
        saved_ops = {k: getattr(trader_mod.store, k)
                     for k in ("add_position", "log", "recompute_stats", "broadcast",
                               "new_position_id", "get_position")}
        saved_rt = copy.deepcopy(state_mod.store.runtime)
        trader_mod.gmgn.swap = spy_swap
        config.gmgn_private_key = lambda: "clé-de-test"
        trader_mod.store.add_position = lambda p: None
        trader_mod.store.log = lambda *a, **kw: None
        trader_mod.store.recompute_stats = lambda: {}
        trader_mod.store.broadcast = lambda *a, **kw: None
        trader_mod.store.new_position_id = lambda: "g1"
        trader_mod.store.get_position = lambda i: None
        state_mod.store.runtime.pop("trading_blocked", None)
        state_mod.store.runtime["sol_price"] = 150.0
        try:
            with SettingsGuard():
                config._settings.update({"execution_backend": "gmgn",
                                         "slippage": 25.0, "priority_fee": 0.00009,
                                         "anti_mev": False, "trade_amount_sol": 0.2,
                                         "paper_trading": False})
                engine.wallet = "Gmgn" + "8" * 40
                engine.sol_balance = 10.0
                await engine.buy({"address": "Gmgn" + "9" * 40, "symbol": "G",
                                  "name": "G", "logo": "", "price_usd": 1e-5})
            check("le moteur gmgn est bien celui appelé quand execution_backend = gmgn",
                  len(order) == 1, f"appels={len(order)}")
            if order:
                got = order[-1]
                check("slippage = 25 % dans l'ordre gmgn", got.get("slippage") == 25,
                      f"slippage={got.get('slippage')}")
                check("priority_fee = 0,00009 SOL dans l'ordre gmgn",
                      abs(float(got.get("priority_fee") or 0) - 0.00009) < 1e-12,
                      f"priority={got.get('priority_fee')}")
                check("anti_mev = False dans l'ordre gmgn",
                      got.get("anti_mev") is False, f"anti_mev={got.get('anti_mev')}")
                check("le montant engagé est celui du réglage (0,2 SOL)",
                      got.get("amount") == "200000000", f"amount={got.get('amount')}")

            section("un moteur mal configuré refuse au lieu de deviner")
            with SettingsGuard():
                config._settings.update({"execution_backend": "gmgn",
                                         "paper_trading": False})
                config.gmgn_private_key = lambda: ""
                r = await engine.buy({"address": "Gmgn" + "9" * 40, "symbol": "G",
                                      "name": "G", "logo": "", "price_usd": 1e-5})
            check("gmgn sans clé privée -> refus explicite, aucun ordre",
                  r.get("executed") is False and "PRIVATE_KEY" in (r.get("reason") or ""),
                  r.get("reason"))

            with SettingsGuard():
                config._settings.update({"execution_backend": "jupiter",
                                         "paper_trading": False})
                solana.wallet_address = lambda: "Autre" + "0" * 40
                r = await engine.buy({"address": "Gmgn" + "9" * 40, "symbol": "G",
                                      "name": "G", "logo": "", "price_usd": 1e-5})
            check("jupiter avec une clé qui ne contrôle pas le wallet -> refus",
                  r.get("executed") is False and "refusé" in (r.get("reason") or ""),
                  (r.get("reason") or "")[:70])
        finally:
            trader_mod.gmgn.swap = saved_swap
            config.gmgn_private_key = saved_key
            for name, value in saved_ops.items():
                setattr(trader_mod.store, name, value)
            state_mod.store.runtime.clear()
            state_mod.store.runtime.update(saved_rt)
    finally:
        jupiter.quote = saved_order["quote"]
        jupiter.swap_transaction = saved_order["build"]
        solana.sign_only = saved_order["sign"]
        solana.simulate_signed = saved_order["sim"]
        solana.sign_and_send = saved_order["send"]
        solana.wallet_address = saved_order["wallet"]
        trader_mod.store.log = saved_order["log"]

    # ============================================ 10. suivi de prix
    title("10) Suivi de prix : les 4 réglages du flux Dexscreener")

    from backend.pricefeed import price_feed

    with SettingsGuard():
        config._settings["price_feed_enabled"] = False
        check("price_feed_enabled = False -> plus aucune adresse suivie",
              price_feed._wanted() == [], f"suivis={price_feed._wanted()}")
    with SettingsGuard():
        config._settings["price_feed_enabled"] = True
        config._settings["price_feed_calls"] = 0
        price_feed._selected = ""
        wanted = price_feed._wanted()
        check("price_feed_calls = 0 -> aucun appel récent suivi",
              all("call" not in (k or "") for k in [""]) and isinstance(wanted, list),
              f"suivis={len(wanted)}")
    saved_poll = float(config.get("price_poll_seconds") or 0)
    check("price_poll_seconds est lu par la boucle de gestion",
          "price_poll_seconds" in inspect.getsource(trader_mod.Trader._manage_loop),
          f"réglage actuel={saved_poll}s")
    check("price_feed_seconds sert de seuil de fraîcheur aux sorties",
          "price_feed_seconds" in inspect.getsource(trader_mod.Trader._manage_loop))
    feed_source = inspect.getsource(type(price_feed))
    check("price_feed_seconds (cadence) est lu par le poller lui-même",
          "price_feed_seconds" in feed_source)
    check("price_feed_calls (appels récents suivis) est lu par le poller",
          "price_feed_calls" in feed_source)
    check("price_feed_enabled coupe réellement le suivi",
          "price_feed_enabled" in feed_source)
    check("le poller lit la boucle de cadence dans le réglage",
          "price_feed_seconds" in inspect.getsource(type(price_feed)._loop))
    check("channels est lu par l'écouteur Telegram",
          "channels" in (ROOT / "backend" / "telegram_listener.py").read_text(
              encoding="utf-8"))
    live_channels = config.all()["channels"]
    check("l'écoute porte bien les 3 canaux configurés", len(live_channels) >= 1,
          ", ".join(live_channels))

    # ============================================ 11. frais
    title("11) Plafond de frais (max_fee_percent) : un rapport chiffré, pas un avis")

    gate = fees.fee_gate({"trade_amount_sol": 0.1, "priority_fee": 0.00002,
                          "max_fee_percent": 3.0})
    check("un aller-retour de 0,055 SOL sur 0,1 SOL -> accepté",
          gate["passed"] is True and abs(gate["estimated_sol"] - 0.000055) < 1e-9
          and gate["share_percent"] <= 0.07,
          f"{gate['estimated_sol']:.6f} SOL · {gate['share_percent']:.2f} % "
          f"(part affichée arrondie à 2 décimales)")
    tiny = fees.fee_gate({"trade_amount_sol": 0.0005, "priority_fee": 0.00002,
                          "max_fee_percent": 3.0})
    check("le même aller-retour sur 0,0005 SOL = 11 % -> refusé",
          tiny["passed"] is False, f"{tiny['share_percent']:.2f} %")
    off = fees.fee_gate({"trade_amount_sol": 0.0005, "priority_fee": 0.00002,
                         "max_fee_percent": 0.0})
    check("max_fee_percent = 0 -> règle désactivée, et le verdict le dit",
          off["passed"] is True and off["enabled"] is False,
          f"activée={off['enabled']} verdict={off['passed']}")
    live = fees.fee_gate(config.all())
    check("le réglage actuel du bot passe le plafond de frais",
          live["passed"] is True,
          f"{config.get('trade_amount_sol')} SOL · {live['share_percent']:.3f} % "
          f"sur un plafond de {config.get('max_fee_percent')} %")

    # --------------------------------------------------------------- bilan
    title("RÉSULTAT")
    total = passed + len(fails)
    print(f"  {passed}/{total} vérifications passées")
    if fails:
        print(f"\n  ÉCHECS ({len(fails)}) :")
        for label in fails:
            print(f"    - {label}")
        print("\n  RESULTAT : ECHEC")
    else:
        print("\n  RESULTAT : TOUT PASSE")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    asyncio.run(main())
