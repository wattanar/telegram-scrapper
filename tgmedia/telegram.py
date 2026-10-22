"""The Telethon adapter: the only module that knows about MTProto."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from urllib.parse import urlsplit

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message
from telethon.tl.types import Channel, MessageReplyHeader, User

from .domain import KINDS, ChatSummary, MediaItem, MediaKind, ResolvedChat, Selection, part_path
from .ports import AmbiguousChat, ChatNotFound, MediaGone, NotAuthorized

log = logging.getLogger(__name__)

_PROGRESS_INTERVAL = 0.25
_LINK_HOSTS = ("t.me", "www.t.me", "telegram.me", "telegram.dog")
_INVITE_SEGMENTS = ("joinchat", "addstickers", "share")
_NUMERIC = re.compile(r"-?\d+")


@dataclass(frozen=True, slots=True)
class LoginResult:
    session: str
    name: str


class _Throttle:
    """Telethon fires the progress callback per chunk; the UI only needs a few per second."""

    def __init__(
        self,
        on_progress: Callable[[int, int | None], None],
        interval: float = _PROGRESS_INTERVAL,
    ) -> None:
        self._on_progress = on_progress
        self._interval = interval
        self._last = 0.0

    def __call__(self, current: int, total: int | None) -> None:
        now = time.monotonic()
        if now - self._last >= self._interval:
            self._last = now
            self._on_progress(current, total)


def detect_kind(message: Message) -> MediaKind | None:
    """First registry row whose attribute the message carries; `document` is last."""
    for kind in KINDS:
        if getattr(message, kind.attr, None):
            return kind
    return None


def topic_id(message: Message) -> int | None:
    reply = message.reply_to
    if not isinstance(reply, MessageReplyHeader) or not reply.forum_topic:
        return None
    return reply.reply_to_top_id or reply.reply_to_msg_id


def _unique_id(message: Message) -> str | None:
    document = message.document or message.photo
    return str(document.id) if document is not None else None


def _haystack(entity: object) -> str:
    title = utils.get_display_name(entity) or ""
    username = getattr(entity, "username", None) or ""
    return f"{title} {username} {utils.get_peer_id(entity)}".lower()


def _chat_kind(entity: object) -> str:
    if isinstance(entity, User):
        return "user"
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast else "group"
    return "group"


def _summary(entity: object) -> ChatSummary:
    chat_id = utils.get_peer_id(entity)
    return ChatSummary(
        chat_id=chat_id,
        title=utils.get_display_name(entity) or str(chat_id),
        username=getattr(entity, "username", None),
        kind=_chat_kind(entity),
    )


def _link_parts(text: str) -> list[str] | None:
    lowered = text.lower()
    for scheme in ("http://", "https://"):
        if lowered.startswith(scheme):
            split = urlsplit(text)
            break
    else:
        split = urlsplit(f"https://{text}")
    if split.netloc.lower() not in _LINK_HOSTS:
        return None
    return [part for part in split.path.split("/") if part]


def _direct_target(query: str) -> str | int | None:
    """Map a username, numeric id, or t.me link to something `get_entity` accepts."""
    text = query.strip()
    if not text:
        return None
    if text.startswith("@"):
        return text
    parts = _link_parts(text)
    if parts is not None:
        if parts and parts[0] == "s" and len(parts) > 1:
            parts = parts[1:]
        if not parts:
            return None
        if parts[0] == "c" and len(parts) > 1 and parts[1].isdigit():
            return int(f"-100{parts[1]}")
        if parts[0].startswith("+") or parts[0] in _INVITE_SEGMENTS:
            raise ChatNotFound(
                f"{query}: invite links are not supported; use @username, a t.me/<name> link, or an id"
            )
        return f"@{parts[0]}"
    if _NUMERIC.fullmatch(text):
        return int(text)
    return None


class TelethonSource:
    def __init__(self, api_id: int, api_hash: str, session: str = "", *, retry: int = 3) -> None:
        self._client = TelegramClient(StringSession(session), api_id, api_hash, connection_retries=5)
        self._retry = max(retry, 0)
        self._entities: dict[str, object] = {}
        self._by_id: dict[int, object] = {}

    async def close(self) -> None:
        await self._client.disconnect()

    async def _authorized_client(self) -> TelegramClient:
        if not self._client.is_connected():
            await self._client.connect()
        if not await self._client.is_user_authorized():
            raise NotAuthorized("the saved session is missing or expired")
        return self._client

    async def _entity(self, client: TelegramClient, query: str) -> object:
        key = query.strip()
        if not key:
            raise ChatNotFound("empty chat query")
        cached = self._entities.get(key)
        if cached is not None:
            return cached
        target = _direct_target(key)
        if target is not None:
            try:
                entity = await client.get_entity(target)
            except (ValueError, RPCError) as exc:
                raise ChatNotFound(f"{query}: {exc}") from None
        else:
            needle = key.lower()
            matches = [dialog.entity for dialog in await client.get_dialogs() if needle in _haystack(dialog.entity)]
            if not matches:
                raise ChatNotFound(f"no chat matches {query!r}")
            if len(matches) > 1:
                raise AmbiguousChat(query, [_summary(entity) for entity in matches])
            entity = matches[0]
        self._entities[key] = entity
        self._by_id[utils.get_peer_id(entity)] = entity
        return entity

    async def resolve_chat(self, query: str) -> ResolvedChat:
        client = await self._authorized_client()
        return _summary(await self._entity(client, query))

    async def list_chats(self, search: str | None) -> list[ChatSummary]:
        client = await self._authorized_client()
        needle = (search or "").strip().lower()
        rows = [
            _summary(dialog.entity)
            for dialog in await client.get_dialogs()
            if not needle or needle in _haystack(dialog.entity)
        ]
        return sorted(rows, key=lambda chat: chat.title.lower())

    async def iter_media(self, chat: str, selection: Selection) -> AsyncIterator[MediaItem]:
        client = await self._authorized_client()
        entity = await self._entity(client, chat)
        chat_id = utils.get_peer_id(entity)
        async for message in client.iter_messages(
            entity,
            offset_date=selection.until,
            reverse=False,
            wait_time=1,
        ):
            item = _to_item(message, selection, chat_id)
            if item is not None:
                yield item

    async def download(
        self,
        item: MediaItem,
        dest: Path,
        on_progress: Callable[[int, int | None], None],
    ) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = part_path(dest)
        entity = self._by_id.get(item.chat_id)
        if entity is None:
            entity = await self._client.get_entity(item.chat_id)
            self._by_id[item.chat_id] = entity
        message = await self._client.get_messages(entity, ids=item.download_ref)
        if message is None or detect_kind(message) is None:
            raise MediaGone(f"message {item.message_id} is no longer available")
        attempts = 0
        while True:
            try:
                await self._client.download_media(
                    message,
                    file=str(part),
                    progress_callback=_Throttle(on_progress),
                )
                os.replace(part, dest)
                return dest
            except FloodWaitError as exc:
                log.warning("flood wait of %ss on message %s", exc.seconds, item.message_id)
                await asyncio.sleep(exc.seconds + 1)
            except (OSError, RPCError, TimeoutError) as exc:
                attempts += 1
                if attempts > self._retry:
                    part.unlink(missing_ok=True)
                    raise
                log.warning(
                    "retry %d/%d for message %s after %s", attempts, self._retry, item.message_id, exc
                )
                await asyncio.sleep(2 ** (attempts - 1))


def _to_item(message: Message, selection: Selection, chat_id: int) -> MediaItem | None:
    if message.action is not None:
        return None
    if message.web_preview is not None:
        return None
    kind = detect_kind(message)
    if kind is None:
        return None
    if selection.thread is not None and topic_id(message) != selection.thread:
        return None
    file = message.file
    date = message.date
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return MediaItem(
        chat_id=chat_id,
        message_id=message.id,
        kind=kind,
        size=file.size if file is not None else None,
        date=date,
        file_name=file.name if file is not None else None,
        unique_id=_unique_id(message),
        download_ref=message.id,
    )


async def interactive_login(api_id: int, api_hash: str) -> LoginResult:
    client = TelegramClient(StringSession(), api_id, api_hash, connection_retries=5)
    try:
        await client.start()
        me = await client.get_me()
        name = utils.get_display_name(me) or getattr(me, "username", None) or "unknown account"
        return LoginResult(session=StringSession.save(client.session), name=name)
    finally:
        await client.disconnect()
