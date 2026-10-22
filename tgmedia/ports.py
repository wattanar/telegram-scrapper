"""The boundary the fetcher talks to: a Telegram source and a progress sink."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Protocol

from .domain import ChatSummary, FetchResult, MediaItem, ResolvedChat, Selection


class ChatLookupError(LookupError):
    """A chat query that does not name exactly one chat."""


class ChatNotFound(ChatLookupError):
    """No known chat matches the query."""


class AmbiguousChat(ChatLookupError):
    """Several chats match the query; the caller decides what to do with the list."""

    def __init__(self, query: str, candidates: list[ChatSummary]) -> None:
        super().__init__(f"{query!r} matches {len(candidates)} chats")
        self.query = query
        self.candidates = candidates


class NotAuthorized(RuntimeError):
    """The saved session is missing, expired, or revoked."""


class TelegramSource(Protocol):
    async def close(self) -> None: ...

    def iter_media(self, chat: str, selection: Selection) -> AsyncIterator[MediaItem]: ...

    async def download(
        self,
        item: MediaItem,
        dest: Path,
        on_progress: Callable[[int, int | None], None],
    ) -> Path: ...

    async def resolve_chat(self, query: str) -> ResolvedChat: ...

    async def list_chats(self, search: str | None) -> list[ChatSummary]: ...


class ProgressSink(Protocol):
    def run_started(self, total_items: int, total_bytes: int) -> None: ...

    def item_started(self, item: MediaItem) -> None: ...

    def item_progress(self, item: MediaItem, written: int, total: int | None) -> None: ...

    def item_finished(self, result: FetchResult) -> None: ...


class NullSink:
    """The sink for dry runs, `--json`, `--quiet`, and tests."""

    def run_started(self, total_items: int, total_bytes: int) -> None:
        pass

    def item_started(self, item: MediaItem) -> None:
        pass

    def item_progress(self, item: MediaItem, written: int, total: int | None) -> None:
        pass

    def item_finished(self, result: FetchResult) -> None:
        pass
