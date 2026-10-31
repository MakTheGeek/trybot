"""Real-time Telegram listener built on Telethon.

Connects with the configured API ID / hash + phone number, listens to the
sniper channels and forwards every Solana contract address it sees to the
pipeline. Supports both a already-saved session and a web-driven login flow
(phone -> code -> optional 2FA password).
"""
from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable

from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.types import PeerChannel, PeerChat, PeerUser

from . import config
from .paths import TELEGRAM_SESSION
from .state import store

# Solana base58 mint addresses: 32-44 chars, no 0/O/I/l.
_CA_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")

# EVM (BSC/ETH) addresses never appear on Solana: drop them before matching so
# a "0x665114…" is not mistaken for a base58 mint starting with "x". A bare
# 40-char hexadecimal string is also an EVM address (a real Solana mint is
# base58, so an all-hex 40-char match is effectively impossible by accident).
_EVM_RE = re.compile(r"0[xX][0-9a-fA-F]{40}\b")
_HEX40_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# Words that look like base58 but are not addresses.
_STOPWORDS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "11111111111111111111111111111111",
}

CallHandler = Callable[[str, str, str, str], Awaitable[None]]


def extract_addresses(text: str) -> list[str]:
    """Return the unique Solana addresses contained in a message."""
    if not text:
        return []
    text = _EVM_RE.sub(" ", text)
    found: list[str] = []
    for match in _CA_RE.findall(text):
        if match in _STOPWORDS:
            continue
        if _HEX40_RE.match(match):  # EVM address, not a Solana mint
            continue
        if len(set(match)) < 8:  # reject low-entropy strings
            continue
        if match not in found:
            found.append(match)
    return found


class TelegramListener:
    def __init__(self, on_call: CallHandler) -> None:
        self.on_call = on_call
        self.client: TelegramClient | None = None
        self._phone: str = ""
        self._code_hash: str = ""
        self._pending_phone: str = ""
        self._entities: dict[str, object] = {}
        self._seen: dict[str, float] = {}
        self.status: str = "disconnected"
        self.last_error: str = ""
        self.me: str = ""
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle
    def _new_client(self) -> TelegramClient:
        api_id = config.telegram_api_id()
        api_hash = config.telegram_api_hash()
        return TelegramClient(
            str(TELEGRAM_SESSION),
            api_id,
            api_hash,
            device_model="SolSnipBot",
            system_version="1.0.0",
            app_version="1.0.0",
            lang_code="fr",
        )

    async def start(self) -> None:
        """Connect and, if a valid session exists, begin listening."""
        async with self._lock:
            if self.client is None:
                self.client = self._new_client()
            try:
                await self.client.connect()
            except Exception as exc:  # noqa: BLE001
                self.status = "error"
                self.last_error = f"Connexion impossible: {exc}"
                store.runtime["telegram"] = "error"
                return
            if await self.client.is_user_authorized():
                await self._begin_listening()
            else:
                self.status = "need_login"
                store.runtime["telegram"] = "need_login"

    async def stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.status = "disconnected"
        store.runtime["telegram"] = "disconnected"

    # ----------------------------------------------------------------- login
    async def send_code(self, phone: str) -> dict:
        async with self._lock:
            if self.client is None:
                self.client = self._new_client()
            if not self.client.is_connected():
                await self.client.connect()
            try:
                sent = await self.client.send_code_request(phone)
            except errors.PhoneNumberInvalidError:
                return {"ok": False, "error": "Numéro de téléphone invalide."}
            except errors.FloodWaitError as exc:
                return {"ok": False, "error": f"Trop de tentatives, réessayez dans {exc.seconds}s."}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}
            self._pending_phone = phone
            self._code_hash = sent.phone_code_hash
            self.status = "awaiting_code"
            store.runtime["telegram"] = "awaiting_code"
            return {"ok": True, "type": type(sent).__name__}

    async def sign_in_code(self, code: str) -> dict:
        if self.client is None or not self._pending_phone:
            return {"ok": False, "error": "Aucune demande de connexion en cours."}
        try:
            await self.client.sign_in(
                phone=self._pending_phone, code=code, phone_code_hash=self._code_hash
            )
        except errors.SessionPasswordNeededError:
            self.status = "awaiting_password"
            store.runtime["telegram"] = "awaiting_password"
            return {"ok": True, "need_password": True}
        except errors.PhoneCodeInvalidError:
            return {"ok": False, "error": "Code invalide."}
        except errors.PhoneCodeExpiredError:
            return {"ok": False, "error": "Code expiré, recommencez."}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        await self._begin_listening()
        return {"ok": True}

    async def sign_in_password(self, password: str) -> dict:
        if self.client is None:
            return {"ok": False, "error": "Client non initialisé."}
        try:
            await self.client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            return {"ok": False, "error": "Mot de passe (2FA) invalide."}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        await self._begin_listening()
        return {"ok": True}

    # -------------------------------------------------------------- listening
    async def _begin_listening(self) -> None:
        assert self.client is not None
        try:
            me = await self.client.get_me()
            self.me = (me.username and f"@{me.username}") or (me.first_name or "") or "connecté"
        except Exception:  # noqa: BLE001
            self.me = "connecté"

        channels = config.get("channels", [])
        resolved: list[str] = []
        for name in channels:
            try:
                entity = await self.client.get_entity(name)
                self._entities[name] = entity
                resolved.append(name)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"Canal introuvable: {name} ({exc.__class__.__name__})"
                store.log(f"Canal Telegram introuvable: {name}", level="warn", action="telegram")

        self.client.add_event_handler(self._on_message, events.NewMessage(chats=resolved or None))
        if not resolved:
            store.log(
                "Aucun canal Telegram résolu — l'écoute est désactivée",
                level="error",
                action="telegram",
            )
        self.status = "listening"
        store.runtime["telegram"] = "listening"
        store.runtime["telegram_user"] = self.me
        store.runtime["telegram_channels"] = resolved
        store.runtime["telegram_listening"] = bool(resolved)
        store.log(
            f"Telegram connecté ({self.me}) — {len(resolved)}/{len(channels)} canaux surveillés",
            level="success",
            action="telegram",
        )
        store.broadcast({"type": "status"})

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        try:
            chat = await event.get_chat()
        except Exception:  # noqa: BLE001
            chat = None

        channel_name = ""
        for name, entity in self._entities.items():
            eid = getattr(entity, "id", None)
            cid = getattr(chat, "id", None)
            if eid and cid and eid == cid:
                channel_name = name
                break
        if not channel_name:
            # Defensive: never analyse messages coming from a chat that is not
            # one of the configured sniper channels.
            return

        text = event.message.message or ""
        if not text and event.message.entities:
            text = event.message.raw_text or ""

        addresses = extract_addresses(text)
        if not addresses:
            return

        import time
        now = time.time()
        # Drop duplicates seen across channels within 20 minutes.
        fresh: list[str] = []
        for addr in addresses:
            if now - self._seen.get(addr, 0) > 1200:
                self._seen[addr] = now
                fresh.append(addr)

        for addr in fresh:
            store.log(f"Nouveau call détecté sur @{channel_name}: {addr[:8]}…",
                      level="info", token=addr, action="telegram")
            try:
                await self.on_call(addr, text, channel_name, "telegram")
            except Exception as exc:  # noqa: BLE001
                store.log(f"Erreur de traitement du call {addr[:8]}…: {exc}",
                          level="error", token=addr, action="pipeline")

    def resolved_channels(self) -> list[str]:
        return list(self._entities.keys())


listener: TelegramListener | None = None


def get_listener(on_call: CallHandler) -> TelegramListener:
    global listener
    if listener is None:
        listener = TelegramListener(on_call)
    else:
        listener.on_call = on_call
    return listener
