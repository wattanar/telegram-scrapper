"""An in-memory TelegramSource so every test runs offline."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tgmedia.domain import ChatSummary, MediaItem, MediaKind, Selection, part_path
from tgmedia.ports import ChatNotFound

DEMO_CHAT = ChatSummary(chat_id=-1001234567890, title="Demo Group", username="demo", kind="group")


@dataclass(frozen=True, slots=True)
class FakeRef:
    data: bytes


def fake_item(
    *,
    message_id: int = 1,
    data: bytes = b"payload",
    kind: str | MediaKind = "photo",
    chat_id: int = DEMO_CHAT.chat_id,
    size: int | None = -1,
    date: datetime | None = None,
    file_name: str | None = None,
    unique_id: str | None = None,
) -> MediaItem:
    resolved_kind = MediaKind.from_name(kind) if isinstance(kind, str) else kind
    return MediaItem(
        chat_id=chat_id,
        message_id=message_id,
        kind=resolved_kind,
        size=len(data) if size == -1 else size,
        date=date or datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc),
        file_name=file_name,
        unique_id=unique_id,
        download_ref=FakeRef(data),
    )


class FakeSource:
    def __init__(
        self,
        items: tuple[MediaItem, ...] = (),
        *,
        chat: ChatSummary = DEMO_CHAT,
        failures: dict[int, Exception] | None = None,
    ) -> None:
        self.items = tuple(items)
        self.chat = chat
        self.failures = dict(failures or {})
        self.download_calls: list[MediaItem] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def iter_media(self, chat: str, selection: Selection) -> AsyncIterator[MediaItem]:
        for item in self.items:
            if selection.matches(item):
                yield item

    async def download(
        self,
        item: MediaItem,
        dest: Path,
        on_progress: Callable[[int, int | None], None],
    ) -> Path:
        self.download_calls.append(item)
        failure = self.failures.get(item.message_id)
        if failure is not None:
            raise failure
        data = item.download_ref.data  # type: ignore[attr-defined]
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = part_path(dest)
        part.write_bytes(data)
        on_progress(len(data), len(data))
        os.replace(part, dest)
        return dest

    async def resolve_chat(self, query: str) -> ChatSummary:
        known = {"@demo", "demo", self.chat.title, str(self.chat.chat_id), "https://t.me/demo"}
        if query.strip() in known:
            return self.chat
        raise ChatNotFound(f"no chat matches {query!r}")

    async def list_chats(self, search: str | None) -> list[ChatSummary]:
        needle = (search or "").strip().lower()
        if needle and needle not in f"{self.chat.title} {self.chat.username} {self.chat.chat_id}".lower():
            return []
        return [self.chat]
