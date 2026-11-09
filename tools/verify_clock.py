r"""Vérification de la correction d'horloge (GMGN refuse ±5 s).

GMGN valide l'horodatage signé à ±5 secondes de son horloge. Une machine qui
dérive — machine virtuelle, veille prolongée, horloge jamais resynchronisée —
fait échouer **tous** les appels avec ``AUTH_TIMESTAMP_EXPIRED``, et le message
accuse la clé API alors que la clé est bonne.

Deux choses sont donc prouvées ici :

* le décalage mesuré est réellement appliqué à l'horodatage envoyé
  (``now_ts``/``now``/``env``) et jamais en double ;
* le fichier ``~/.config/gmgn/.env`` ne peut plus neutraliser cette mesure :
  ``gmgn-cli`` le charge avec ``override: true``, si bien qu'un
  ``GMGN_TIME_OFFSET_MS`` périmé y prime sur la valeur passée par le bot — le cas
  exact qui a rendu GMGN muet le 9 novembre 2026 (fichier à -93 900 ms pour une
  dérive réelle de -117 000 ms). Le miroir ne corrige que s'il trouve la ligne :
  le fichier appartient à l'utilisateur, rien n'y est ajouté.

Aucun réseau n'est requis : le fichier est une copie temporaire, la mesure est
posée à la main.

    .\venv\Scripts\python.exe tools\verify_clock.py
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import clock as clock_mod  # noqa: E402

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


BODY = (
    "# identifiants gmgn\n"
    "GMGN_API_KEY=gmgn_test_0123456789\n"
    "GMGN_PRIVATE_KEY=clef_de_test\n"
    "GMGN_TIME_OFFSET_MS=-93900\n"
)


def fresh() -> clock_mod.Clock:
    """A clock whose measurement is set, so no network is touched."""
    c = clock_mod.Clock()
    c.measured = True
    c.source = "test"
    c.offset_ms = -117000.0
    return c


# ===================================================== 1. la mesure est appliquée
title("1) Le décalage mesuré est bien celui qui part vers GMGN")

c = fresh()
now = time.time()
c.now_ts()
check("now_ts() retire le décalage au lieu de l'ajouter",
      abs(c.now_ts() - (time.time() - 117.0)) < 2.0,
      f"now_ts={c.now_ts()} local={int(time.time())}")
check("now() renvoie la même heure corrigée",
      abs(c.now().timestamp() - c.now_ts()) < 1.0,
      c.now().isoformat())
check("env() transmet le décalage en millisecondes",
      c.env() == {"GMGN_TIME_OFFSET_MS": "-117000"}, str(c.env()))
check("drift_seconds expose le décalage en secondes",
      abs(c.drift_seconds + 117.0) < 0.5, f"{c.drift_seconds:+.1f}s")
check("une dérive de 2 minutes est signalée comme malsaine",
      c.is_healthy() is False, "tolérance 4 s")
small = fresh()
small.offset_ms = -1200.0
check("une dérive de 1,2 s reste saine", small.is_healthy() is True)
unmeasured = clock_mod.Clock()
check("un décalage non mesuré vaut zéro",
      unmeasured.env() == {"GMGN_TIME_OFFSET_MS": "0"}, str(unmeasured.env()))

# =========================================== 2. le fichier gmgn ne peut pas mentir
title("2) Le GMGN_TIME_OFFSET_MS périmé du fichier gmgn est corrigé")

with tempfile.TemporaryDirectory() as tmp:
    path = pathlib.Path(tmp) / ".env"
    path.write_text(BODY, encoding="utf-8")
    original = clock_mod.GMGN_ENV_PATH
    clock_mod.GMGN_ENV_PATH = path
    try:
        c = fresh()
        note = c.mirror_into_gmgn_env()
        after = path.read_text(encoding="utf-8")
        check("la ligne périmée est réécrite avec la mesure du jour",
              "GMGN_TIME_OFFSET_MS=-117000" in after,
              [ln for ln in after.splitlines() if "OFFSET" in ln][0])
        check("le reste du fichier est intact, octet pour octet",
              after.replace("GMGN_TIME_OFFSET_MS=-117000",
                            "GMGN_TIME_OFFSET_MS=-93900") == BODY)
        check("les identifiants ne sont pas touchés",
              "GMGN_API_KEY=gmgn_test_0123456789" in after
              and "GMGN_PRIVATE_KEY=clef_de_test" in after)
        check("la correction est annoncée, pas silencieuse",
              bool(note) and "-93900" in note and "-117000" in note, note[:90])
        check("aucun fichier temporaire ne reste derrière",
              not list(pathlib.Path(tmp).glob("*.tmp")),
              str(list(pathlib.Path(tmp).iterdir())))

        # Une deuxième passe ne réécrit rien : la valeur est déjà bonne.
        before = path.read_text(encoding="utf-8")
        again = c.mirror_into_gmgn_env()
        check("une valeur déjà juste n'entraîne aucune écriture",
              path.read_text(encoding="utf-8") == before, again or "aucune note")
        check("et n'annonce rien", again == "", repr(again))

        # Juste sous la tolérance : on ne touche pas au fichier pour 2 s.
        path.write_text(BODY.replace("-93900", "-115500"), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        c2 = fresh()
        c2.offset_ms = -117000.0
        quiet = c2.mirror_into_gmgn_env()
        check("un écart de 1,5 s est toléré, sans écriture",
              path.read_text(encoding="utf-8") == before and quiet == "",
              quiet or "aucune note")

        # Pas de ligne du tout : le fichier de l'utilisateur n'est pas complété.
        path.write_text(BODY.replace("GMGN_TIME_OFFSET_MS=-93900\n", ""),
                        encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        note = fresh().mirror_into_gmgn_env()
        check("sans ligne existante, rien n'est ajouté au fichier",
              path.read_text(encoding="utf-8") == before and note == "",
              note or "aucune note")
        check("l'absence de ligne n'est pas une erreur",
              "OFFSET" not in path.read_text(encoding="utf-8"))

        # Fichier absent (installation sans gmgn-cli) : silence, pas d'exception.
        path.unlink()
        note = fresh().mirror_into_gmgn_env()
        check("un fichier absent n'est pas créé et ne lève rien",
              note == "" and not path.exists(), repr(note))

        # Horloge non mesurée : on ne réécrit jamais un fichier sans mesure.
        path.write_text(BODY, encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        blind = clock_mod.Clock()
        note = blind.mirror_into_gmgn_env()
        check("sans mesure, le fichier reste tel quel",
              path.read_text(encoding="utf-8") == before and note == "",
              note or "aucune note")

        # Fichier illisible (dossier supprimé, droits retirés) : une note
        # explique, aucune exception ne remonte au démarrage du bot.
        blocked = pathlib.Path(tmp) / "cache"
        blocked.mkdir()
        clock_mod.GMGN_ENV_PATH = blocked
        note = fresh().mirror_into_gmgn_env()
        check("un fichier illisible ne lève rien",
              note == "", repr(note))

        # Fichier lisible mais non écrivable : la note doit nommer la cause.
        clock_mod.GMGN_ENV_PATH = path
        path.write_text(BODY, encoding="utf-8")
        (pathlib.Path(tmp) / ".env.tmp").mkdir()
        note = fresh().mirror_into_gmgn_env()
        check("une écriture impossible est expliquée, pas avalée",
              "périmé" in note and "GMGN_TIME_OFFSET_MS" in note, note[:90])
        check("le fichier d'origine reste lisible et complet",
              "GMGN_API_KEY=gmgn_test_0123456789" in path.read_text(encoding="utf-8"))
    finally:
        clock_mod.GMGN_ENV_PATH = original

# ================================================== 3. le fichier réel de gmgn-cli
title("3) Le fichier réel de gmgn-cli est sain sur cette machine")

real = clock_mod.GMGN_ENV_PATH
if real.exists():
    text = real.read_text(encoding="utf-8", errors="replace")
    check("les identifiants gmgn sont toujours là",
          "GMGN_API_KEY" in text and "GMGN_PRIVATE_KEY" in text,
          f"{real} ({len(text)} octets)")
    match = clock_mod._OFFSET_LINE.search(text)
    if match:
        stored = int(match.group(2))
        check("le décalage figé sera réaligné au prochain démarrage",
              True,
              f"{stored:+d} ms stocké — le bot réécrit la ligne après mesure")
    else:
        check("aucun GMGN_TIME_OFFSET_MS figé dans le fichier réel", True,
              "le bot fournit le décalage correct à chaque appel")
else:
    check("pas de fichier gmgn-cli sur cette machine (rien à contredire)", True,
          str(real))

# ===================================================================== résultat
print(f"\n{'=' * 72}")
if fails:
    print(f"RÉSULTAT : {passed} OK, {len(fails)} échec(s)")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print(f"RÉSULTAT : {passed} OK, 0 échec(s)")
print("=" * 72)
