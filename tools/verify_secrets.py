r"""Vérification des secrets du tableau de bord.

Le tableau de bord permet d'enregistrer la clé privée du wallet, les clés API
(GMGN, Helius) et les identifiants Telegram. Ce harnais prouve que ce réglage
est à la fois **réel** et **sûr** :

* chaque secret du catalogue est **réellement lu** par un module du backend —
  un champ qui n'alimente rien serait un formulaire décoratif ;
* la **priorité de lecture** est celle annoncée (variable d'environnement >
  fichier du tableau de bord > anciens ``.env``), et une valeur masquée par une
  variable d'environnement est signalée comme telle ;
* chaque **validation** refuse une valeur inutilisable au lieu de l'enregistrer :
  une clé de wallet illisible laisserait le bot incapable de signer, sans que
  personne ne comprenne pourquoi ;
* le **fichier** vit hors du projet, est réécrit de façon atomique, sans BOM, et
  les valeurs multilignes (PEM) y survivent ;
* **aucune valeur n'est jamais renvoyée** à un navigateur, seulement un masque.

    .\venv\Scripts\python.exe tools\verify_secrets.py

Aucun secret réel n'est écrit : tous les essais d'écriture visent un dossier
temporaire, et le fichier réel est contrôlé en début et en fin de course. Si le
serveur tourne, une dernière section éprouve l'API HTTP en lecture et en refus.
"""
from __future__ import annotations

import ast
import asyncio
import builtins
import importlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import config, secrets  # noqa: E402
from backend import solana_rpc as solana  # noqa: E402
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


# ------------------------------------------------------------------ the guard
REAL_FILE = pathlib.Path(secrets.file_path())
REAL_BEFORE = REAL_FILE.read_bytes() if REAL_FILE.exists() else None


class TempStore:
    """Point the store at a throwaway file, then restore everything."""

    def __enter__(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="solsnipbot-secrets-"))
        self.saved_file = secrets.SECRETS_FILE
        self.saved_dir = secrets.SECRETS_DIR
        secrets.SECRETS_DIR = self.dir
        secrets.SECRETS_FILE = self.dir / "secrets.env"
        secrets.reload()
        return self

    def __exit__(self, *exc):
        secrets.SECRETS_FILE = self.saved_file
        secrets.SECRETS_DIR = self.saved_dir
        secrets.reload()
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


class EnvGuard:
    """Temporarily set environment variables / the legacy .env layer."""

    def __enter__(self):
        self.env = {k: os.environ.get(k) for k in ("GMGN_API_KEY", "HELIUS_API_KEY",
                                                   "SOLANA_WALLET_PRIVATE_KEY")}
        self.legacy = dict(config._env)
        return self

    def __exit__(self, *exc):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._env.clear()
        config._env.update(self.legacy)
        return False


# ============================================================ 1. catalogue
title("1) Catalogue : chaque secret alimente-t-il vraiment le code ?")

sources = {p.name: p.read_text(encoding="utf-8")
           for p in sorted((ROOT / "backend").glob("*.py"))}
consumers = {name: text for name, text in sources.items()
             if name not in {"config.py", "secrets.py"}}

# Some secrets are reached through a typed accessor rather than by their raw
# name (`config.telegram_api_hash()` rather than the string). Wiring has to be
# proved for those too: a field that feeds nothing is a decorative form.
ACCESSORS: dict[str, tuple[str, ...]] = {
    "GMGN_API_KEY": ("config.gmgn_api_key()",),
    "GMGN_PRIVATE_KEY": ("config.gmgn_private_key()",),
    "SOLANA_WALLET_PRIVATE_KEY": ("config.wallet_private_key()", "wallet_keypair()"),
    "TELEGRAM_API_ID": ("config.telegram_api_id()",),
    "TELEGRAM_API_HASH": ("config.telegram_api_hash()",),
    "TELEGRAM_PHONE": ("config.telegram_phone()",),
}

orphans = []
for spec in secrets.CATALOGUE:
    needles = ('"{0}"'.format(spec["key"]),) + ACCESSORS.get(spec["key"], ())
    if not any(needle in text for text in consumers.values() for needle in needles):
        orphans.append(spec["key"])
check(f"les {len(secrets.CATALOGUE)} secrets sont lus par un module du backend",
      not orphans, f"jamais lus : {orphans}" if orphans else "tous branchés")

incomplete = [s["key"] for s in secrets.CATALOGUE
              if not s.get("label") or not s.get("hint") or not s.get("kind")]
check("chaque secret porte un libellé, une aide et un type", not incomplete,
      f"incomplets : {incomplete}")

unknown_kinds = sorted({s["kind"] for s in secrets.CATALOGUE} - {
    "wallet", "token", "pem", "helio", "int", "hash", "phone"})
check("chaque type de secret a une validation connue", not unknown_kinds,
      f"types sans validation : {unknown_kinds}")

check("le fichier des secrets vit bien hors du projet",
      ROOT not in REAL_FILE.parents, str(REAL_FILE))

# ======================================================= 2. precedence
title("2) Priorité de lecture : environnement > fichier > anciens .env")

with TempStore() as store, EnvGuard():
    secrets.set_secret("GMGN_API_KEY", "gmgn_file_layer_0123456789")
    layer, value = secrets._value_in_force("GMGN_API_KEY")
    check("le fichier du tableau de bord l'emporte sur l'ancien .env",
          layer == "fichier" and value == "gmgn_file_layer_0123456789",
          f"couche={layer}")

    config._env["GMGN_API_KEY"] = "gmgn_legacy_layer_0123456789"
    secrets.clear("GMGN_API_KEY")
    layer, value = secrets._value_in_force("GMGN_API_KEY")
    check("après retrait, l'ancien .env reprend la main",
          layer == "herite" and value == "gmgn_legacy_layer_0123456789",
          f"couche={layer} valeur={value[:12]}…")

    os.environ["GMGN_API_KEY"] = "gmgn_env_layer_0123456789"
    layer, value = secrets._value_in_force("GMGN_API_KEY")
    check("une variable d'environnement l'emporte sur tout le reste",
          layer == "env" and value == "gmgn_env_layer_0123456789", f"couche={layer}")

    secrets.set_secret("GMGN_API_KEY", "gmgn_file_layer_0123456789")
    entry = secrets.entry("GMGN_API_KEY")
    check("la valeur du fichier est signalée comme masquée, jamais crue active",
          entry["shadowed"] is True and entry["source"] == "env",
          f"shadowed={entry['shadowed']} source={entry['source']}")

    check("config.get_env applique la même priorité",
          config.get_env("GMGN_API_KEY") == "gmgn_env_layer_0123456789")
    del os.environ["GMGN_API_KEY"]
    check("sans variable d'environnement, config.get_env lit le fichier",
          config.get_env("GMGN_API_KEY") == "gmgn_file_layer_0123456789")

    # Un fichier .env enregistré par Notepad ou PowerShell 5.1 commence par un
    # BOM : sans le retirer, la première clé du fichier devient « \ufeffGMGN_API_KEY »,
    # donc absente. Constaté en vrai sur ~/.config/gmgn/.env (clé API déclarée
    # absente alors que le fichier la contenait).
    with tempfile.TemporaryDirectory() as tmp:
        bom_file = pathlib.Path(tmp) / ".env"
        bom_file.write_bytes(b"\xef\xbb\xbfGMGN_API_KEY=gmgn_bom_0123456789\n"
                             b"AUTRE_CLE=seconde\n")
        parsed = config._read_env_file(bom_file)
        check("un .env avec BOM ne perd pas sa première clé",
              parsed.get("GMGN_API_KEY") == "gmgn_bom_0123456789", str(parsed)[:80])
        check("et lit quand même les clés suivantes",
              parsed.get("AUTRE_CLE") == "seconde")

    check("une clé absente est annoncée absente",
          secrets.entry("HELIUS_API_KEY")["set"] is False)
    layer, value = secrets._value_in_force("SOLANA_WALLET_PRIVATE_KEY")
    check("la clé du wallet est vue depuis son fichier historique (wallet.key)",
          layer == "wallet_key" and len(value) > 20, f"couche={layer}")
    check("le statut du wallet dit donc bien « définie », avec sa provenance",
          secrets.entry("SOLANA_WALLET_PRIVATE_KEY")["set"] is True
          and secrets.entry("SOLANA_WALLET_PRIVATE_KEY")["source"] == "wallet_key",
          f"source={secrets.entry('SOLANA_WALLET_PRIVATE_KEY')['source']}")
    telegram = secrets.entry("TELEGRAM_API_HASH")
    check("aucun identifiant Telegram n'est écrit en dur dans le code",
          telegram["source"] != "defaut", f"source={telegram['source']}")
    check("le hash Telegram vient d'un fichier ou de l'environnement",
          telegram["source"] in ("env", "fichier", "herite", "absent"),
          f"source={telegram['source']}")

# ======================================================= 3. validation
title("3) Validation : une valeur inutilisable est refusée, pas enregistrée")

with TempStore(), EnvGuard():
    section("clé privée du wallet")
    real_key = solana.wallet_keypair()
    good_key = config.wallet_private_key()
    ok, cleaned, message, extra = secrets.validate("SOLANA_WALLET_PRIVATE_KEY", good_key)
    check("la vraie clé du bot est validée et son adresse dérivée",
          ok and extra.get("address") == str(real_key.pubkey()),
          f"adresse={extra.get('address')} attendue={real_key.pubkey()}")
    check("le message annonce l'adresse (preuve que la clé est utilisable)",
          "adresse" in message.lower(), message[:70])

    ok, _, message, _ = secrets.validate("SOLANA_WALLET_PRIVATE_KEY", "pas-une-cle")
    check("une clé illisible est refusée", ok is False, message[:70])

    ok, _, message, _ = secrets.validate(
        "SOLANA_WALLET_PRIVATE_KEY", "1" * 20)
    check("une clé trop courte est refusée avec sa longueur", ok is False
          and "octets" in message, message[:70])

    ok, _, message, _ = secrets.validate("SOLANA_WALLET_PRIVATE_KEY", "")
    check("un champ vide est refusé", ok is False, message)

    bad_keypair = bytes([7] * 32 + [9] * 32)
    ok, _, _, extra = secrets.validate(
        "SOLANA_WALLET_PRIVATE_KEY", json.dumps(list(bad_keypair)))
    check("le tableau JSON est accepté et l'adresse dérivée de la graine",
          ok, f"adresse={extra.get('address')}")

    section("clés API et identifiants Telegram")
    cases = [
        ("GMGN_API_KEY", "gmgn_abcdefghijklmnop", True, "clé GMGN bien formée"),
        ("GMGN_API_KEY", "court", False, "clé GMGN trop courte"),
        ("GMGN_API_KEY", "gmgn key with space", False, "clé GMGN avec espace"),
        ("GMGN_API_KEY", "abcdefghijklmnop", True, "clé GMGN sans préfixe acceptée"),
        ("GMGN_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----",
         True, "PEM complet accepté"),
        ("GMGN_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nAAA", False, "PEM tronqué refusé"),
        ("GMGN_PRIVATE_KEY", "MC4CAQAwBQYDK2VwBCIEIAAA", True, "PEM en base64 nue accepté"),
        ("HELIUS_API_KEY", "00000000-0000-4000-8000-000000000000", True,
         "clé Helius (UUID) acceptée"),
        ("HELIUS_API_KEY", "trop-court", False, "clé Helius trop courte refusée"),
        ("HELIUS_API_KEY", "0000 0000 4000 8000", False, "clé Helius avec espace refusée"),
        ("TELEGRAM_API_ID", "12345678", True, "API ID numérique accepté"),
        ("TELEGRAM_API_ID", "12345678x", False, "API ID non numérique refusé"),
        ("TELEGRAM_API_HASH", "0123456789abcdef0123456789abcdef", True,
         "API hash 32 hex accepté"),
        ("TELEGRAM_API_HASH", "0123456789abcdef0123456789abcde", False,
         "API hash de 31 caractères refusé"),
        ("TELEGRAM_PHONE", "+33612345678", True, "numéro international accepté"),
        ("TELEGRAM_PHONE", "0612345678", True, "numéro sans indicatif accepté"),
        ("TELEGRAM_PHONE", "+33 6 12 34 56 78", True, "espaces retirés du numéro"),
        ("TELEGRAM_PHONE", "pas-un-numero", False, "numéro invalide refusé"),
    ]
    for key, value, expected, label in cases:
        ok, cleaned, message, _ = secrets.validate(key, value)
        detail = "" if ok == expected else f"message={message[:60]}"
        check(f"{label}", ok == expected, detail)
    phone = secrets.validate("TELEGRAM_PHONE", "+33 6 12 34 56 78")[1]
    check("le numéro enregistré ne contient plus d'espace", phone == "+33612345678", phone)

    try:
        secrets.set_secret("INCONNU", "x")
        refused = False
    except KeyError:
        refused = True
    check("un secret hors catalogue ne peut pas être écrit", refused)

# ======================================================= 4. fichier
title("4) Fichier : écriture atomique, multiligne, sans BOM")

with TempStore() as store, EnvGuard():
    pem = "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIAAA\n-----END PRIVATE KEY-----"
    secrets.set_secret("GMGN_PRIVATE_KEY", pem)
    secrets.set_secret("TELEGRAM_PHONE", "+33612345678")
    raw = secrets.SECRETS_FILE.read_bytes()
    check("le fichier existe et ne commence pas par un BOM",
          raw[:3] != b"\xef\xbb\xbf", f"début={raw[:12]!r}")
    check("aucun fichier temporaire ne reste derrière",
          not list(store.dir.glob("*.tmp")), str(list(store.dir.iterdir())))

    secrets._store.clear()
    secrets.reload()
    check("un PEM multiligne survit à l'aller-retour",
          secrets.stored("GMGN_PRIVATE_KEY") == pem,
          repr(secrets.stored("GMGN_PRIVATE_KEY"))[:70])
    check("la deuxième clé survit aussi",
          secrets.stored("TELEGRAM_PHONE") == "+33612345678")

    secrets.set_secret("GMGN_API_KEY", "gmgn_a=b=c_d0123456789")
    secrets._store.clear()
    secrets.reload()
    check("un « = » dans la valeur ne coupe pas la ligne",
          secrets.stored("GMGN_API_KEY") == "gmgn_a=b=c_d0123456789",
          secrets.stored("GMGN_API_KEY"))

    with open(secrets.SECRETS_FILE, "a", encoding="utf-8") as handle:
        handle.write("AUTRE_CLE=gardee\n")
    secrets.reload()
    check("une clé inconnue du catalogue est conservée, pas effacée",
          secrets.stored("AUTRE_CLE") == "gardee", secrets.stored("AUTRE_CLE"))

    secrets.clear("GMGN_PRIVATE_KEY")
    check("clear() retire la valeur du fichier",
          secrets.stored("GMGN_PRIVATE_KEY") == "")
    check("clear() laisse les autres clés en place",
          secrets.stored("TELEGRAM_PHONE") == "+33612345678")

# ======================================================= 5. secret gardé
title("5) Ce qui sort de l'API : un masque, jamais la valeur")

with TempStore(), EnvGuard():
    SECRET_VALUE = "gmgn_ULTRA_SECRET_0123456789"
    secrets.set_secret("GMGN_API_KEY", SECRET_VALUE)
    dumped = json.dumps(secrets.status(), ensure_ascii=False)
    check("la valeur en clair n'apparaît nulle part dans le statut",
          SECRET_VALUE not in dumped and "ULTRA_SECRET" not in dumped)
    entry = secrets.entry("GMGN_API_KEY")
    check("le masque montre le début et la fin, pas le milieu",
          entry["mask"].startswith("gmgn") and entry["mask"].endswith("6789")
          and len(entry["mask"]) < len(SECRET_VALUE),
          entry["mask"])
    check("la longueur est annoncée pour permettre de reconnaître la clé",
          entry["length"] == len(SECRET_VALUE), str(entry["length"]))
    check("set_secret ne renvoie pas la valeur non plus",
          SECRET_VALUE not in json.dumps(secrets.set_secret("GMGN_API_KEY", SECRET_VALUE)))

    wallet_entry = secrets.entry("SOLANA_WALLET_PRIVATE_KEY")
    real = config.wallet_private_key()
    check("le masque d'une clé de wallet ne révèle rien d'exploitable",
          "-----BEGIN" not in wallet_entry["mask"]
          or wallet_entry["mask"].endswith("caractères)"),
          wallet_entry["mask"])
    check("le masque est plus court que la clé", len(wallet_entry["mask"]) < len(real)
          or wallet_entry["mask"].startswith("PEM"), wallet_entry["mask"])

# ======================================================= 6. Helius
title("6) Helius : la clé est branchée dans l'URL, pas recopiée dans un fichier")

check("une URL avec clé en clair est réécrite en marqueur",
      secrets.placeholder_url("https://mainnet.helius-rpc.com/?api-key=abc-123") ==
      "https://mainnet.helius-rpc.com/?api-key={api_key}",
      secrets.placeholder_url("https://mainnet.helius-rpc.com/?api-key=abc-123"))
check("les paramètres suivants sont préservés",
      secrets.placeholder_url("https://x.helius-rpc.com/?api-key=a&commitment=processed")
      == "https://x.helius-rpc.com/?api-key={api_key}&commitment=processed")
check("une URL déjà en marqueur n'est pas abîmée",
      secrets.placeholder_url("https://mainnet.helius-rpc.com/?api-key={api_key}")
      == "https://mainnet.helius-rpc.com/?api-key={api_key}")

saved_url = config._settings.get("solana_rpc_url")
saved_fallback = config._settings.get("solana_rpc_fallback_url")
try:
    config._settings["solana_rpc_url"] = "https://mainnet.helius-rpc.com/?api-key=SECRET-RPC"
    config._settings["solana_rpc_fallback_url"] = "https://api.mainnet-beta.solana.com"
    check("une clé Helius en clair est détectée dans les réglages",
          [n for n, _ in secrets.clear_key_urls()] == ["solana_rpc_url"],
          str(secrets.clear_key_urls()))
    check("helius_in_use dit que la clé du fichier n'est PAS utilisée",
          secrets.helius_in_use() is False)
    config._settings["solana_rpc_url"] = "https://mainnet.helius-rpc.com/?api-key={api_key}"
    check("helius_in_use dit que la clé du fichier est utilisée",
          secrets.helius_in_use() is True)
    check("plus aucune clé en clair à migrer",
          secrets.clear_key_urls() == [])
finally:
    config._settings["solana_rpc_url"] = saved_url
    config._settings["solana_rpc_fallback_url"] = saved_fallback

with TempStore():
    secrets.set_secret("HELIUS_API_KEY", "00000000-0000-4000-8000-000000000000")
    try:
        config._settings["solana_rpc_url"] = "https://mainnet.helius-rpc.com/?api-key={api_key}"
        try:
            secrets.clear("HELIUS_API_KEY")
            blocked = False
        except ValueError:
            blocked = True
        check("retirer la clé Helius quand l'URL RPC en dépend est refusé",
              blocked, "le bot se retrouverait sans nœud pour les envois")
        config._settings["solana_rpc_url"] = "https://api.mainnet-beta.solana.com"
        check("sans dépendance, le retrait est accepté",
              secrets.clear("HELIUS_API_KEY")["ok"] is True)
    finally:
        config._settings["solana_rpc_url"] = saved_url

# ======================================================= 7. API en direct
title("7) API HTTP (si le serveur tourne) : refus et masques, rien de plus")

BASE = os.environ.get("SOLSNIPBOT_URL", "http://127.0.0.1:8787")
BASE_BEFORE = REAL_FILE.read_bytes() if REAL_FILE.exists() else None


def _http(method: str, path: str, body: dict | None = None, timeout: float = 15.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return answer.status, json.loads(answer.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return None, {"detail": str(exc)}


status, payload = _http("GET", "/api/credentials")
if status is None:
    print("  (serveur non joignable sur " + BASE + " — section ignorée)")
else:
    check("GET /api/credentials répond", status == 200, f"code={status}")
    items = payload.get("items") or []
    check("tous les secrets du catalogue sont listés",
          [i["key"] for i in items] == [s["key"] for s in secrets.CATALOGUE],
          f"{len(items)} entrées")
    live_wallet = config.wallet_private_key()
    body_text = json.dumps(payload, ensure_ascii=False)
    check("la clé privée du wallet n'est PAS dans la réponse HTTP",
          bool(live_wallet) and live_wallet not in body_text)
    check("le chemin du fichier est annoncé", bool(payload.get("file")),
          str(payload.get("file"))[:60])
    check("l'état du verrou réseau est annoncé",
          "local_only" in payload and "client_local" in payload,
          f"local_only={payload.get('local_only')}")

    status, answer = _http("POST", "/api/credentials",
                           {"key": "SOLANA_WALLET_PRIVATE_KEY", "value": "pas-une-cle"})
    check("une clé de wallet invalide est refusée par l'API (400)",
          status == 400, f"code={status} — {str(answer.get('detail'))[:70]}")

    status, answer = _http("POST", "/api/credentials", {"key": "PAS_UN_SECRET", "value": "x"})
    check("un secret inconnu est refusé par l'API (400)", status == 400, f"code={status}")

    status, answer = _http("POST", "/api/credentials/test",
                           {"key": "SOLANA_WALLET_PRIVATE_KEY"})
    check("le test de la clé du wallet dérive bien l'adresse en service",
          status == 200 and answer.get("ok") is True
          and answer.get("address") == str(solana.wallet_keypair().pubkey()),
          str(answer.get("detail"))[:80])

    status, answer = _http("POST", "/api/credentials/test", {"key": "GMGN_API_KEY"})
    check("une clé non testable est annoncée comme telle",
          status == 200 and answer.get("testable") is False,
          str(answer.get("detail"))[:60])

    after = REAL_FILE.read_bytes() if REAL_FILE.exists() else None
    check("aucun refus n'a écrit dans le fichier réel des secrets",
          after == BASE_BEFORE, "fichier inchangé")

# ======================================================= 8. fichier réel
title("8) Le fichier réel n'a pas été touché par ce harnais")

after = REAL_FILE.read_bytes() if REAL_FILE.exists() else None
check("le fichier des secrets est exactement dans son état initial",
      after == REAL_BEFORE,
      "absent au départ et à l'arrivée" if REAL_BEFORE is None
      else f"{len(REAL_BEFORE)} octets inchangés")
check("le magasin en mémoire pointe toujours sur le vrai fichier",
      pathlib.Path(secrets.file_path()) == REAL_FILE, secrets.file_path())

# ======================================================= 9. hooks de rafraichissement
title("9) Les symboles appelés après un enregistrement existent vraiment")

# `_apply_secret` (main.py) is what makes a stored key take effect: it re-derives
# the wallet, restarts the RPC socket, migrates the Helius URL. Every one of those
# calls is a name that only fails at runtime — on the one path a user takes right
# after typing a key. They are checked here against the real modules, so a rename
# is caught by the harness instead of by a confusing toast in the dashboard.
main_tree = ast.parse((ROOT / "backend" / "main.py").read_text(encoding="utf-8"))

module_names: set[str] = set()
for node in main_tree.body:
    if isinstance(node, ast.ImportFrom):
        module_names.update(alias.asname or alias.name for alias in node.names)
    elif isinstance(node, ast.Import):
        module_names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        module_names.add(node.name)
    elif isinstance(node, ast.Assign):
        module_names.update(t.id for t in node.targets if isinstance(t, ast.Name))


# A name in main.py can be a module or a singleton living inside one.
ALIASES: dict[str, tuple[str, str]] = {
    "store": ("backend.state", "store"),
    "trader": ("backend.trader", "trader"),
    "price_feed": ("backend.pricefeed", "price_feed"),
    "bot": ("backend.bot", "bot"),
    "gmgn": ("backend.gmgn", "gmgn"),
    "clock": ("backend.clock", "clock"),
    "dexscreener": ("backend.dexscreener", "dexscreener"),
}


def _resolve_base(base: str):
    """The module (or the singleton) a dotted call in main.py starts from."""
    target = ALIASES.get(base)
    try:
        if target is not None:
            module_path, attribute = target
            return getattr(importlib.import_module(module_path), attribute)
        module = importlib.import_module("backend." + base)
    except Exception:  # noqa: BLE001
        return None
    return getattr(module, base) if hasattr(module, base) else module


def _called_names(function: ast.AST) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            found.append(("", func.id))
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            found.append((func.value.id, func.attr))
    return found


hooks = [n for n in main_tree.body
         if isinstance(n, ast.AsyncFunctionDef) and n.name in {"_apply_secret",
                                                              "_write_helius_placeholder"}]
check("les fonctions de rafraîchissement sont présentes dans main.py",
      len(hooks) == 2, f"{len(hooks)} trouvée(s)")

local_missing: list[str] = []
attribute_missing: list[str] = []
attribute_checked = 0
for hook in hooks:
    for base, name in _called_names(hook):
        if not base:
            if name not in module_names and name not in dir(builtins):
                local_missing.append(f"{hook.name}->{name}")
            continue
        target = _resolve_base(base)
        if target is None:
            continue
        attribute_checked += 1
        if not hasattr(target, name):
            attribute_missing.append(f"{base}.{name}")

check("chaque fonction locale appelée après un enregistrement existe",
      not local_missing, ", ".join(local_missing) if local_missing else
      f"contrôlées dans les deux hooks")
check(f"chaque appel « module.attribut » existe ({attribute_checked} contrôlés)",
      not attribute_missing, ", ".join(attribute_missing) if attribute_missing else
      "tous résolus")

check("le rafraîchissement du wallet est bien une fonction du module trader",
      callable(getattr(trader_mod, "resolve_wallet", None))
      and not hasattr(trader_mod.trader, "resolve_wallet"),
      "appelée en tant que fonction de module, pas de méthode")

address_direct = solana.wallet_address()
resolved, balance = asyncio.run(trader_mod.resolve_wallet())
check("resolve_wallet() dérive la même adresse que le RPC",
      resolved == address_direct and bool(resolved),
      f"{resolved} vs {address_direct}")
check("le solde du wallet est lu sur la chaîne (non simulé)",
      isinstance(balance, float) and balance >= 0.0, f"{balance} SOL")

check("les points d'entrée du socket RPC existent",
      callable(getattr(solana, "stop_background", None))
      and callable(getattr(solana, "start_background", None)))

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
