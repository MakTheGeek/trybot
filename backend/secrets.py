"""Secrets entered from the dashboard, kept outside the project.

The keys this bot runs on — the Solana wallet key, the GMGN API pair, the Helius
key, the Telegram credentials — used to live in whichever file happened to hold
them first (``~/.config/gmgn/.env``, ``~/.config/solsnipbot/wallet.key``, or
embedded in the RPC URL inside ``data/settings.json``). Changing one meant
editing a file by hand, and a Helius key could end up sitting in a project file.

This module gives them one explicit home — ``~/.config/solsnipbot/secrets.env``,
*outside* the bot folder, so a copy or an archive of the project can never carry
a private key — and one rule for reading them::

    variable d'environnement  >  ce fichier  >  anciens .env

An operator who exports a real environment variable therefore always keeps
control, and a value saved here can never be silently ignored: when an
environment variable shadows it, :func:`status` says so.

Nothing in this module ever returns a stored secret. :func:`status` reports
whether a key is set, which layer the value in force comes from, and a mask with
a few leading and trailing characters — enough to recognise a key, not enough to
use one. Values are never logged either; callers log the *label* only.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import threading
import urllib.parse
from typing import Any

from .paths import SECRETS_DIR, SECRETS_FILE

# ------------------------------------------------------------------ catalogue
# ``kind`` drives validation and masking, ``legacy`` names the historical home
# of the value (shown in the UI), and ``testable`` marks the keys that can be
# checked for real without spending anything or touching the money path.
CATALOGUE: tuple[dict[str, Any], ...] = (
    {
        "key": "SOLANA_WALLET_PRIVATE_KEY",
        "label": "Clé privée du wallet Solana",
        "hint": "base58 ou tableau JSON [1,2,…]. Signe chaque achat et chaque vente "
                "avec le moteur Jupiter.",
        "kind": "wallet",
        "testable": True,
        "legacy": "~/.config/solsnipbot/wallet.key",
        # The wallet key has one extra historical home, a file on its own. It has
        # to be part of the reported precedence: the bot signs with it, so
        # calling the key "absent" while it works would be a plain lie.
        "legacy_path": "~/.config/solsnipbot/wallet.key",
    },
    {
        "key": "GMGN_API_KEY",
        "label": "Clé API GMGN",
        "hint": "Commence par « gmgn_ ». Analyses, holders, et les ordres si le moteur "
                "d'exécution GMGN est choisi.",
        "kind": "token",
        "testable": False,
        "legacy": "~/.config/gmgn/.env",
    },
    {
        "key": "GMGN_PRIVATE_KEY",
        "label": "Clé privée GMGN (signature des requêtes)",
        "hint": "Fournie par GMGN. Signe les requêtes authentifiées ; les retours à la "
                "ligne d'un PEM sont conservés.",
        "kind": "pem",
        "testable": False,
        "legacy": "~/.config/gmgn/.env",
    },
    {
        "key": "HELIUS_API_KEY",
        "label": "Clé API Helius (nœud Solana)",
        "hint": "Utilisée via le marqueur {api_key} de l'URL RPC. L'enregistrer réécrit "
                "l'URL RPC si elle contient encore la clé en clair.",
        "kind": "helio",
        "testable": True,
        "legacy": "écrite en clair dans l'URL RPC",
    },
    {
        "key": "TELEGRAM_API_ID",
        "label": "Telegram — API ID",
        "hint": "Nombre entier, sur my.telegram.org/apps.",
        "kind": "int",
        "testable": False,
        "legacy": "valeur d'une version antérieure, écrite dans config.py",
    },
    {
        "key": "TELEGRAM_API_HASH",
        "label": "Telegram — API hash",
        "hint": "32 caractères hexadécimaux, sur my.telegram.org/apps.",
        "kind": "hash",
        "testable": False,
        "legacy": "valeur d'une version antérieure, écrite dans config.py",
    },
    {
        "key": "TELEGRAM_PHONE",
        "label": "Telegram — numéro de téléphone",
        "hint": "Format international, par exemple +33 6 12 34 56 78.",
        "kind": "phone",
        "testable": False,
        "legacy": "valeur d'une version antérieure, écrite dans config.py",
    },
)

BY_KEY: dict[str, dict[str, Any]] = {spec["key"]: spec for spec in CATALOGUE}

SOURCE_LABELS: dict[str, str] = {
    "env": "variable d'environnement",
    "fichier": "fichier du tableau de bord",
    "herite": "ancien fichier .env",
    "wallet_key": "fichier wallet.key",
    "defaut": "valeur en dur dans le code",
    "absent": "",
}

_NOT_SECRET_KINDS = {"int", "phone"}

_lock = threading.RLock()
_store: dict[str, str] = {}


# --------------------------------------------------------------------- file IO
def _unescape(value: str) -> str:
    """Turn the file's ``\\n`` back into a newline (a PEM needs its line breaks)."""
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            if nxt == "n":
                out.append("\n")
                index += 2
                continue
            if nxt == "\\":
                out.append("\\")
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _escape(value: str) -> str:
    """One secret must fit on one line, whatever it contains."""
    return value.replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n")


def _parse(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        found[name.strip()] = _unescape(value.strip())
    return found


def _harden() -> None:
    """Owner-only file: 0600 where that means something, an ACL on Windows."""
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except OSError:
        pass
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(SECRETS_FILE), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True, timeout=20, check=False,
        )
    except Exception:  # noqa: BLE001  (a harden failure must never lose a key)
        pass


def _write() -> None:
    """Rewrite the whole file, atomically, from the in-memory store."""
    with _lock:
        known = set(BY_KEY)
        lines = [
            "# SolSnipBot - secrets enregistres depuis le tableau de bord.",
            "# Ce fichier vit HORS du dossier du bot : ne le partagez pas, ne le",
            "# versionnez pas. Format : une ligne KEY=VALUE par secret, les retours",
            "# a la ligne d'un PEM etant ecrits \\n.",
            "",
        ]
        for spec in CATALOGUE:
            name = spec["key"]
            if _store.get(name):
                lines.append(f"{name}={_escape(_store[name])}")
        # A key the catalogue does not know is preserved rather than dropped.
        for name, value in _store.items():
            if name not in known and value:
                lines.append(f"{name}={_escape(value)}")
        body = "\n".join(lines) + "\n"
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SECRETS_FILE.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(SECRETS_FILE)
    _harden()


def reload() -> dict[str, str]:
    """Re-read the file (used at import and by tests)."""
    text = ""
    if SECRETS_FILE.exists():
        try:
            text = SECRETS_FILE.read_text(encoding="utf-8")
        except OSError:
            text = ""
    parsed = _parse(text)
    with _lock:
        _store.clear()
        _store.update(parsed)
    return dict(parsed)


def stored(key: str) -> str:
    """Only what *this* file holds — no environment, no legacy file."""
    with _lock:
        return _store.get(key, "")


def file_path() -> str:
    return str(SECRETS_FILE)


# ----------------------------------------------------------------- validation
def _derive_wallet_address(raw: str) -> str:
    """Prove a private key is usable by deriving its address (nothing is sent)."""
    import base58
    from solders.keypair import Keypair

    text = (raw or "").strip()
    if not text:
        raise ValueError("valeur vide")
    if text.startswith("["):
        data = bytes(json.loads(text))
    else:
        try:
            data = base58.b58decode(text)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"ce n'est pas du base58 valide ({exc})") from exc
    if len(data) == 64:
        try:
            return str(Keypair.from_bytes(data).pubkey())
        except Exception:  # noqa: BLE001
            # Solana secret keys are seed(32) + pubkey(32); some tools write a
            # correct seed with a stale trailing half, so the seed is what counts.
            return str(Keypair.from_seed(data[:32]).pubkey())
    if len(data) == 32:
        return str(Keypair.from_seed(data).pubkey())
    raise ValueError(f"{len(data)} octets décodés (32 ou 64 attendus)")


def validate(key: str, raw: str) -> tuple[bool, str, str, dict[str, Any]]:
    """Check a value before it is stored.

    Returns ``(ok, cleaned, message, extra)``. A value is refused rather than
    stored broken: a wallet key that cannot derive an address would leave the
    bot unable to sign, and the user with no idea why.
    """
    spec = BY_KEY.get(key)
    if spec is None:
        return False, "", f"Secret inconnu : {key}", {}

    value = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    kind = spec["kind"]
    if kind != "pem":
        value = value.strip()
    if not value:
        return False, "", "La valeur est vide.", {}

    if kind == "wallet":
        try:
            address = _derive_wallet_address(value)
        except Exception as exc:  # noqa: BLE001
            return False, "", f"Clé privée illisible ou invalide : {exc}.", {}
        return True, value, f"Clé valide — adresse dérivée : {address}", {"address": address}

    if kind in {"token", "helio", "int", "hash"} and any(c.isspace() for c in value):
        return False, "", "La valeur ne doit contenir ni espace ni retour à la ligne.", {}

    if kind == "token":
        if len(value) < 10:
            return False, "", "Trop court pour une clé API GMGN.", {}
        note = "" if value.startswith("gmgn_") else " Elle ne commence pas par « gmgn_ »."
        return True, value, f"Enregistrée.{note}", {}

    if kind == "pem":
        if "-----BEGIN" in value and "-----END" not in value:
            return False, "", "Clé PEM tronquée : la ligne « -----END » manque.", {}
        return True, value, "Enregistrée.", {}

    if kind == "helio":
        if len(value) < 20:
            return False, "", "Trop court pour une clé Helius (UUID de 36 caractères attendu).", {}
        note = ""
        if not re.fullmatch(r"[0-9a-fA-F-]{32,40}", value):
            note = " Elle ne ressemble pas à un UUID Helius, mais elle est acceptée telle quelle."
        return True, value, f"Enregistrée.{note}", {}

    if kind == "int":
        if not value.isdigit():
            return False, "", "L'API ID doit être un nombre entier.", {}
        return True, value, "Enregistré.", {}

    if kind == "hash":
        if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
            return False, "", "L'API hash doit faire 32 caractères hexadécimaux.", {}
        return True, value, "Enregistré.", {}

    if kind == "phone":
        digits = value.replace(" ", "").replace("-", "")
        if not re.fullmatch(r"\+?[0-9]{6,20}", digits):
            return False, "", "Numéro invalide : format international attendu (ex. +33612345678).", {}
        return True, digits, "Enregistré.", {}

    return True, value, "Enregistré.", {}


def mask(value: str) -> str:
    """A value a user can recognise — never one they can use."""
    text = (value or "").strip()
    if not text:
        return ""
    if "-----BEGIN" in text:
        return f"PEM ({len(text)} caractères)"
    if text.startswith("["):
        return f"tableau JSON ({len(text)} caractères)"
    if len(text) <= 8:
        return text[0] + "…" + text[-1]
    return text[:4] + "…" + text[-4:]


# ------------------------------------------------------------------- the store
def _value_in_force(key: str) -> tuple[str, str]:
    """``(layer, value)`` for the value the bot would actually use."""
    from . import config  # deferred: config imports this module

    environment = (os.environ.get(key) or "").strip()
    if environment:
        return "env", environment

    own = stored(key)
    if own:
        return "fichier", own

    legacy = (getattr(config, "_env", {}) or {}).get(key, "")
    if isinstance(legacy, str) and legacy.strip():
        return "herite", legacy.strip()

    # A secret may also have a file of its own from an earlier version.
    legacy_path = BY_KEY[key].get("legacy_path")
    if legacy_path:
        path = pathlib.Path(os.path.expanduser(legacy_path))
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            if text:
                return "wallet_key", text

    if BY_KEY[key].get("code_default"):
        literal = {
            "TELEGRAM_API_ID": str(config.telegram_api_id()),
            "TELEGRAM_API_HASH": config.telegram_api_hash(),
            "TELEGRAM_PHONE": config.telegram_phone(),
        }.get(key, "")
        if literal:
            return "defaut", literal
    return "absent", ""


def entry(key: str) -> dict[str, Any]:
    """Everything the UI may know about one secret: never the value itself."""
    spec = BY_KEY[key]
    source, value = _value_in_force(key)
    environment = (os.environ.get(key) or "").strip()
    own = stored(key)
    return {
        "key": key,
        "label": spec["label"],
        "hint": spec["hint"],
        "kind": spec["kind"],
        "testable": bool(spec.get("testable")),
        "legacy": spec.get("legacy", ""),
        "multiline": spec["kind"] == "pem",
        "type": "text" if spec["kind"] in _NOT_SECRET_KINDS else "password",
        "set": bool(value),
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
        "mask": mask(value),
        "length": len(value),
        "in_file": bool(own),
        # An environment variable wins over this file: say it, so a value saved
        # from the dashboard is never believed to be in use when it is not.
        "shadowed": bool(environment and own and environment != own),
    }


def status() -> list[dict[str, Any]]:
    return [entry(spec["key"]) for spec in CATALOGUE]


def set_secret(key: str, raw: str) -> dict[str, Any]:
    """Validate, store, and report — without ever echoing the value back."""
    if key not in BY_KEY:
        raise KeyError(key)
    ok, value, message, extra = validate(key, raw)
    if not ok:
        raise ValueError(message)
    with _lock:
        _store[key] = value
        _write()
    return {"ok": True, "message": message, **extra, "entry": entry(key)}


def clear(key: str) -> dict[str, Any]:
    """Forget this file's value; whatever layer sat below it takes over again."""
    if key not in BY_KEY:
        raise KeyError(key)
    # The RPC URL can be *made of* this key. Removing it then leaves
    # "?api-key={api_key}" behind: reads would fall back, but `sendTransaction`
    # never does, so a real buy would fail. Refuse, and say what to do first.
    if key == "HELIUS_API_KEY" and helius_in_use():
        raise ValueError(
            "L'URL RPC utilise la clé Helius (« {api_key} ») : la retirer laisserait "
            "le bot sans nœud pour les envois de transaction. Remplacez d'abord "
            "l'URL RPC par une URL complète."
        )
    with _lock:
        _store.pop(key, None)
        _write()
    return {
        "ok": True,
        "message": "Valeur retirée : la source précédente reprend la main.",
        "entry": entry(key),
    }


# ------------------------------------------------------------------- the RPC
def helius_in_use() -> bool:
    """Is the Helius key actually wired into the RPC URL in force?

    ``data/settings.json`` used to carry the key inline. Saving a key here does
    nothing while that URL stays as it is, so the UI has to be able to say so.
    """
    from . import config

    url = str(config.get_env("SOLANA_RPC_URL") or config.get("solana_rpc_url") or "")
    return "{api_key}" in url or "${HELIUS_API_KEY}" in url


def clear_key_urls() -> list[tuple[str, str]]:
    """Settings whose Helius URL still carries a key in the clear."""
    from . import config

    found: list[tuple[str, str]] = []
    for name in ("solana_rpc_url", "solana_rpc_fallback_url"):
        url = str(config.get(name) or "")
        if "helius" in url.lower() and re.search(r"[?&]api-key=[^&{]", url):
            found.append((name, url))
    return found


def placeholder_url(url: str) -> str:
    """Rewrite an inline API key into the ``{api_key}`` placeholder."""
    return re.sub(r"([?&]api-key=)[^&]*", r"\1{api_key}", url)


# ---------------------------------------------------------------------- tests
async def _test_helius(key: str) -> dict[str, Any]:
    import asyncio

    from . import net

    url = "https://mainnet.helius-rpc.com/?api-key=" + urllib.parse.quote(key, safe="")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []}
    try:
        data = await asyncio.to_thread(net.post_json, url, payload, 12.0)
    except Exception as exc:  # noqa: BLE001
        # The key sits in the URL, so it must never survive into a message.
        return {"ok": False, "detail": "Le nœud n'a pas répondu : " + _scrub(str(exc), key)}
    if not isinstance(data, dict):
        return {"ok": False, "detail": "Réponse illisible du nœud."}
    slot = data.get("result")
    if slot is None:
        error = data.get("error") or {}
        reason = error.get("message") if isinstance(error, dict) else str(error)
        return {"ok": False, "detail": f"Clé refusée par Helius : {reason or data}"}
    return {"ok": True, "detail": f"Le nœud Helius répond — slot {slot}."}


def _scrub(text: str, secret: str) -> str:
    if secret and secret in text:
        text = text.replace(secret, "***")
    return text[:200]


async def test(key: str) -> dict[str, Any]:
    """Check the value in force for real, without spending anything."""
    spec = BY_KEY.get(key)
    if spec is None:
        return {"ok": False, "detail": f"Secret inconnu : {key}"}

    source, value = _value_in_force(key)
    if not value:
        return {"ok": False, "detail": "Aucune valeur à tester."}

    if key == "SOLANA_WALLET_PRIVATE_KEY":
        try:
            address = _derive_wallet_address(value)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"Clé illisible : {exc}."}
        return {
            "ok": True,
            "detail": f"Clé valide — le bot signerait avec l'adresse {address}.",
            "address": address,
        }

    if key == "HELIUS_API_KEY":
        return await _test_helius(value)

    return {
        "ok": False,
        "testable": False,
        "detail": "Pas de test direct sans effet de bord : le vrai test est le "
                  "fonctionnement de Telegram (ou de l'analyse GMGN). Le statut "
                  "ci-dessus montre la valeur réellement en vigueur.",
    }


reload()
