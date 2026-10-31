r"""Vérification du basculement papier / réel du tableau de bord.

Le tableau de bord permet de passer du carnet papier au carnet réel d'un clic.
C'est le seul réglage où l'écart entre « enregistré » et « en vigueur » vaut de
l'argent, donc ce harnais prouve trois choses :

* les **trois modes** sont nommés correctement, y compris celui qui se confond
  avec un autre — mode papier éteint *et* « Trading réel » éteint signifie
  OBSERVATION, aucun ordre ne part, et l'appeler « RÉEL » serait un mensonge que
  le wallet paierait ;
* **aucun clic ne peut armer le trading réel tout seul** : le passage en réel
  n'écrit ``live_trading`` que si la case a été cochée, ce qui est vérifié sur
  le patch réellement construit, pas sur le texte de l'interface ;
* **changer de mode ne change jamais la nature d'une position déjà ouverte** :
  une position papier reste virtuelle, une position réelle reste vendue pour de
  vrai, quel que soit le mode en vigueur au moment de la sortie.

    .\venv\Scripts\python.exe tools\verify_mode.py

Aucun réglage n'est écrit : l'endpoint est éprouvé avec des doubles (``update``,
``log`` et ``broadcast`` sont remplacés), et ``data/settings.json`` est contrôlé
octet pour octet en début et en fin de course. Si le serveur tourne, une dernière
section l'interroge en lecture et en refus, jamais sur un succès — une bascule
réussie écrirait un vrai changement de mode sur le bot en service.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402

OK = "  [OK]"
KO = "  [ECHEC]"
fails: list[str] = []
passed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
    else:
        fails.append(label)
    print(f"{OK if condition else KO} {label}" + (f"  — {detail}" if detail else ""))


def title(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


# ============================================================ 1. les trois modes
title("1) Les trois modes : ce que le serveur répond pour un état donné")

COMBOS = [
    (True, True, True, "paper"),
    (True, False, False, "paper"),
    (False, True, True, "live"),
    (False, True, False, "live"),
    (False, False, False, "observation"),
    (False, False, True, "observation"),
]


def _state(paper: bool, live: bool, auto: bool) -> dict:
    return config.mode_state({"paper_trading": paper, "live_trading": live,
                              "auto_snipe": auto})


for paper, live, auto, expected in COMBOS:
    got = _state(paper, live, auto)["key"]
    check(f"papier={int(paper)} réel={int(live)} auto={int(auto)} → {expected.upper()}",
          got == expected, f"obtenu={got}")

can_send_ok = all(_state(p, l, a)["can_send_orders"] == ((not p) and l)
                  for p, l, a, _ in COMBOS)
check("« can_send_orders » n'est vrai que hors papier ET avec le trading réel",
      can_send_ok,
      ", ".join(f"{int(p)}{int(l)}→{_state(p, l, a)['can_send_orders']}"
                for p, l, a, _ in COMBOS))

auto_ok = all(_state(p, l, a)["auto_orders"] == ((not p) and l and a)
              for p, l, a, _ in COMBOS)
check("« auto_orders » exige en plus le sniper automatique", auto_ok)

check("le mode papier ne peut jamais envoyer un ordre",
      all(not _state(True, l, a)["can_send_orders"] for l in (True, False) for a in (True, False)))

labels = {
    "paper": _state(True, False, False)["label"],
    "live": _state(False, True, True)["label"],
    "live_sans_auto": _state(False, True, False)["label"],
    "observation": _state(False, False, False)["label"],
}
check("les quatre libellés sont tous distincts", len(set(labels.values())) == 4,
      " / ".join(labels.values()))
check("le libellé PAPIER dit qu'aucun ordre n'est envoyé",
      "aucun ordre" in labels["paper"], labels["paper"])
check("le libellé RÉEL ne promet jamais qu'aucun ordre ne part",
      "aucun ordre" not in labels["live"], labels["live"])
check("le libellé OBSERVATION ne se fait pas passer pour RÉEL",
      "OBSERVATION" in labels["observation"] and "RÉEL" not in labels["observation"],
      labels["observation"])
check("RÉEL distingue le sniper automatique activé du désactivé",
      labels["live"] != labels["live_sans_auto"],
      f"{labels['live']} / {labels['live_sans_auto']}")

check("le nom court est cohérent avec le libellé",
      all(_state(p, l, a)["name"] in _state(p, l, a)["label"].upper()
          for p, l, a, _ in COMBOS))

# ============================================== 2. le tableau de bord dit pareil
title("2) Le tableau de bord nomme exactement les mêmes modes")

app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
index_html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

tous_les_libelles = {_state(p, l, a)["label"]
                     for p in (True, False) for l in (True, False) for a in (True, False)}
manquants = sorted(lib for lib in tous_les_libelles if lib not in app_js)
check("chaque libellé du serveur existe tel quel dans le tableau de bord",
      not manquants, "; ".join(manquants)[:140])

noms_manquants = [n for n in config.MODE_NAMES.values() if f"'{n}'" not in app_js]
check("les trois noms de mode sont connus du tableau de bord",
      not noms_manquants, ", ".join(noms_manquants))

check("l'interrupteur est dans la barre du haut, toujours visible",
      'id="mode-switch"' in index_html and 'class="topbar-right"' in index_html)
check("il propose les deux modes",
      'data-mode="paper"' in index_html and 'data-mode="live"' in index_html)
check("la passerelle de bascule existe dans le script",
      "async function switchMode" in app_js and "async function postMode" in app_js)
check("le mode est dérivé en un seul endroit côté navigateur",
      app_js.count("function modeInfo()") == 1)
check("la fenêtre de confirmation précède le passage en réel",
      "Passer en mode RÉEL ?" in app_js and "mode-arm-live" in app_js)
check("elle montre le solde réel du wallet",
      "wallet_balance_sol" in app_js and "fonds insuffisants" in app_js)
check("aucun passage en réel sans case cochée explicitement",
      "body.live_trading = !!arm.checked" in app_js)
check("le retour au papier est immédiat (un frein doit rester atteignable)",
      "await postMode({ mode: 'paper' })" in app_js)

# ======================================================= 3. le serveur, un seul point
title("3) Côté serveur : un seul endroit décide du mode")

main_src = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")

check("POST /api/mode existe", '@app.post("/api/mode")' in main_src)
check("le badge de mode vient de config.mode_state",
      'store.runtime["trading_mode"] = mode["key"]' in main_src)
check("l'ancien calcul en ligne du badge a bien disparu",
      '"papier" if' not in main_src and '"réel" if' not in main_src)
check("/api/state annonce le mode",
      'snap["mode"] = config.mode_state(settings)' in main_src)
check("l'instantané WebSocket l'annonce aussi",
      'snap["mode"] = config.mode_state(snap["settings"])' in main_src)
check("/api/health annonce la capacité d'envoyer un ordre",
      '"can_send_orders": mode["can_send_orders"]' in main_src)
check("le mode est validé avant d'être appliqué (aucune valeur libre)",
      "if wanted not in {config.MODE_PAPER, config.MODE_LIVE}:" in main_src)
check("un changement de mode est tracé, avec son origine",
      'origin=_client_ip(request)' in main_src)
check("config.mode_state() est la seule source du mode",
      main_src.count("config.mode_state(") >= 4)

# ============================================== 4. l'endpoint, éprouvé sans écrire
title("4) L'endpoint, éprouvé avec des doubles : refus et portée du changement")

try:
    from backend import main as main_mod  # noqa: PLC0415  (import lourd, volontairement tardif)

    importe = True
except Exception as exc:  # noqa: BLE001
    importe = False
    check("le module de l'API s'importe", False, str(exc)[:90])

if importe:
    class FakeRequest:
        headers: dict = {}
        client = None

    # A controlled base, so the checks do not depend on the user's live settings
    # in any way: what is verified is the patch the endpoint builds, not the file.
    base = {"paper_trading": True, "live_trading": False, "auto_snipe": True}
    captured: dict = {}
    sauve = {"update": config.update, "log": main_mod.store.log,
             "broadcast": main_mod.store.broadcast}

    def fake_update(patch):
        captured.clear()
        captured.update(patch)
        return dict(base, **patch)

    def avale(*a, **k):
        return None

    config.update = fake_update
    main_mod.store.log = avale
    main_mod.store.broadcast = avale

    def appelle(mode, live=None):
        change = (main_mod.ModeRequest(mode=mode) if live is None
                  else main_mod.ModeRequest(mode=mode, live_trading=live))
        return asyncio.run(main_mod.api_set_mode(change, FakeRequest()))

    def refuse(mode):
        """True when the API refused without touching anything."""
        captured.clear()
        try:
            appelle(mode)
            return False
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "status_code", None)
            return code == 400 and not captured

    try:
        for mauvais in ("", "réel", "REAL", "live2", " ", "observation"):
            check(f"le mode « {mauvais} » est refusé sans rien changer (400)",
                  refuse(mauvais))

        appelle("paper")
        check("« paper » n'écrit que paper_trading=true",
              captured == {"paper_trading": True}, str(captured))
        check("et n'arme jamais « Trading réel » au passage",
              "live_trading" not in captured)

        reponse = appelle("live")
        check("« live » n'éteint que le mode papier",
              captured == {"paper_trading": False}, str(captured))
        check("et laisse « Trading réel » tel quel",
              "live_trading" not in captured)
        check("la réponse avoue que rien ne partira tant que le réel est éteint",
              reponse["mode"]["key"] == "observation"
              and reponse["mode"]["can_send_orders"] is False
              and any("Trading réel" in n for n in reponse["notes"]),
              str(reponse["notes"])[:110])
        check("elle nomme le mode qu'elle a réellement atteint",
              reponse["mode"]["name"] == "OBSERVATION", reponse["mode"]["name"])

        reponse = appelle("live", live=True)
        check("en réel, la case cochée arme bien le trading réel",
              captured == {"paper_trading": False, "live_trading": True},
              str(captured))
        check("et la réponse dit que les snipes engagent de vrais SOL",
              reponse["mode"]["can_send_orders"] is True
              and any("vrais SOL" in n for n in reponse["notes"]),
              str(reponse["notes"])[:110])

        montant = float(config.get("trade_amount_sol") or 0.0)
        if montant > 0:
            main_mod.store.runtime["wallet_balance_sol"] = montant / 4.0
            reponse = appelle("live", live=True)
            check("un solde insuffisant est annoncé, pas découvert au premier achat",
                  any("fonds insuffisants" in n for n in reponse["notes"]),
                  str(reponse["notes"])[:130])
            del main_mod.store.runtime["wallet_balance_sol"]

        reponse = appelle("paper")
        check("la réponse en papier rappelle que les positions réelles restent réelles",
              any("positions réelles" in n for n in reponse["notes"]),
              str(reponse["notes"])[:110])
        check("la réponse transporte les réglages appliqués",
              reponse["settings"]["paper_trading"] is True)
    finally:
        config.update = sauve["update"]
        main_mod.store.log = sauve["log"]
        main_mod.store.broadcast = sauve["broadcast"]

    check("config.update est bien rendu intact après ces essais",
          config.update is sauve["update"])

# ============================== 5. une position ne change pas de nature au basculement
title("5) Basculer le mode ne change jamais la nature d'une position ouverte")

trader_src = (ROOT / "backend" / "trader.py").read_text(encoding="utf-8")
self_src = (ROOT / "tools" / "selfcheck.py").read_text(encoding="utf-8")

check("une position papier est fermée en virtuel, jamais vendue pour de vrai",
      'if position.get("paper"):' in trader_src
      and "return await self._paper_sell(position, percent=percent, reason=reason,"
      in trader_src
      and "manual=manual)" in trader_src)
check("une position réelle est soldée pour de vrai, même en mode papier",
      'paper = bool(position.get("paper"))' in trader_src
      and "await self._recover_rent(position)" in trader_src)
check("l'auto-contrôle couvre déjà ce cas de bout en bout",
      "une position PAPIER est fermée en virtuel même mode papier éteint" in self_src)
check("le mode papier sépare les deux carnets à l'achat",
      'if bool(settings.get("paper_trading")):' in trader_src)

# ================================================ 6. API HTTP : lecture et refus
title("6) API HTTP (si le serveur tourne) : lecture et refus, jamais un succès")

BASE = os.environ.get("SOLSNIPBOT_URL", "http://127.0.0.1:8787")
SETTINGS_FILE = ROOT / "data" / "settings.json"
SETTINGS_BEFORE = SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None
MODE_BEFORE = config.mode_state()["key"]


def _http(method: str, path: str, body: dict | None = None, timeout: float = 15.0):
    donnees = json.dumps(body).encode("utf-8") if body is not None else None
    requete = urllib.request.Request(
        BASE + path, data=donnees, method=method,
        headers={"Content-Type": "application/json"} if donnees else {},
    )
    try:
        with urllib.request.urlopen(requete, timeout=timeout) as reponse:
            return reponse.status, json.loads(reponse.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return None, {"detail": str(exc)}


status, etat = _http("GET", "/api/state")
if status is None:
    print("  (serveur non joignable sur " + BASE + " — section ignorée)")
else:
    annonce = etat.get("mode") or {}
    local = config.mode_state()
    check("GET /api/state annonce le mode",
          annonce.get("key") in {"paper", "live", "observation"},
          str(annonce.get("key")))
    check("et il correspond aux réglages enregistrés",
          annonce.get("key") == local["key"],
          f"serveur={annonce.get('key')} réglages={local['key']}")
    check("le libellé annoncé est celui du serveur",
          annonce.get("label") == local["label"], str(annonce.get("label"))[:80])
    check("la capacité d'envoyer un ordre est annoncée",
          annonce.get("can_send_orders") == local["can_send_orders"],
          f"serveur={annonce.get('can_send_orders')} réglages={local['can_send_orders']}")

    status, sante = _http("GET", "/api/health")
    trading = (sante or {}).get("trading") or {}
    check("GET /api/health dit exactement la même chose",
          status == 200 and trading.get("mode") == local["key"]
          and trading.get("can_send_orders") == local["can_send_orders"],
          f"code={status} mode={trading.get('mode')}")

    for mauvais in ("", "réel", "REAL"):
        status, reponse = _http("POST", "/api/mode", {"mode": mauvais})
        check(f"POST /api/mode refuse « {mauvais} » (400)",
              status == 400, f"code={status} — {str(reponse.get('detail'))[:70]}")

    status, reponse = _http("POST", "/api/mode", {})
    check("POST /api/mode refuse une requête sans mode (422)", status in (400, 422),
          f"code={status}")

# ================================================ 7. rien n'a bougé sur le disque
title("7) Rien n'a été écrit : ni les réglages, ni le mode en vigueur")

apres = SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None
check("data/settings.json est identique au début et à la fin",
      apres == SETTINGS_BEFORE,
      f"{len(SETTINGS_BEFORE or b'')} → {len(apres or b'')} octets")
check("le mode en vigueur est inchangé",
      config.mode_state()["key"] == MODE_BEFORE,
      f"{MODE_BEFORE} → {config.mode_state()['key']}")

# ------------------------------------------------------------------- bilan
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
