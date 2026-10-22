"""Data shapes shared by the CLI, the fetcher, the ledger, and the Telegram adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path


class MediaKindError(ValueError):
    """A kind name that is not in the registry."""


@dataclass(frozen=True, slots=True)
class MediaKind:
    """One registry row: how a message is detected, and where that kind lands."""

    name: str
    attr: str
    ext: str
    subdir: str

    @classmethod
    def from_name(cls, name: str) -> MediaKind:
        key = name.strip().lower()
        try:
            return _KINDS_BY_NAME[key]
        except KeyError:
            known = ", ".join(kind.name for kind in KINDS)
            raise MediaKindError(f"unknown kind {name!r}; choose from {known}") from None


# Order is detection precedence: the adapter takes the first row whose attribute a
# message carries. Stickers come before gifs because video stickers carry both
# attributes, and `document` is true for every document so it sits last.
KINDS: tuple[MediaKind, ...] = (
    MediaKind("photo", "photo", "jpg", "photos"),
    MediaKind("sticker", "sticker", "webp", "stickers"),
    MediaKind("gif", "gif", "mp4", "gifs"),
    MediaKind("video", "video", "mp4", "videos"),
    MediaKind("voice", "voice", "ogg", "voice"),
    MediaKind("audio", "audio", "mp3", "audio"),
    MediaKind("document", "document", "bin", "documents"),
)
_KINDS_BY_NAME = {kind.name: kind for kind in KINDS}

MAX_NAME_LENGTH = 160


@dataclass(frozen=True, slots=True)
class MediaItem:
    """One downloadable message, fully populated by the adapter, free of Telegram objects."""

    chat_id: int
    message_id: int
    kind: MediaKind
    size: int | None
    date: datetime
    file_name: str | None
    unique_id: str | None
    download_ref: object


@dataclass(frozen=True, slots=True)
class Selection:
    """The argv filters, parsed once at the CLI boundary."""

    kinds: frozenset[MediaKind] | None = None
    since: datetime | None = None
    until: datetime | None = None
    min_size: int | None = None
    max_size: int | None = None
    limit: int | None = None
    thread: int | None = None

    def matches(self, item: MediaItem) -> bool:
        if self.kinds is not None and item.kind not in self.kinds:
            return False
        if self.since is not None and item.date < self.since:
            return False
        if self.until is not None and item.date > self.until:
            return False
        if item.size is not None:
            if self.min_size is not None and item.size < self.min_size:
                return False
            if self.max_size is not None and item.size > self.max_size:
                return False
        return True


class ItemState(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FetchResult:
    item: MediaItem
    state: ItemState
    path: Path | None
    bytes_written: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ChatSummary:
    chat_id: int
    title: str
    username: str | None
    kind: str


# Resolving a chat and listing one return the same four fields; one shape keeps the
# port honest instead of two identical dataclasses.
ResolvedChat = ChatSummary


def chat_slug(chat: ChatSummary) -> str:
    """The preferred name for a chat's folder, before `destination` makes it safe."""
    return chat.username or chat.title or str(chat.chat_id)


_UNSAFE = re.compile(r"[^\w-]+")
_UNSAFE_EXT = re.compile(r"[^A-Za-z0-9]")


def _slug(text: str, fallback: str) -> str:
    # `.` and `/` are outside `\w`, so a slug can never traverse or name a dot dir.
    slug = _UNSAFE.sub("-", text).strip("-")
    return slug[:MAX_NAME_LENGTH] or fallback


def _extension(item: MediaItem) -> str:
    if item.file_name:
        raw = Path(item.file_name).suffix.lstrip(".")
        clean = _UNSAFE_EXT.sub("", raw).lower()[:10]
        if clean:
            return clean
    return item.kind.ext


def destination(root: Path, chat_slug: str, item: MediaItem) -> Path:
    """`<root>/<chat_slug>/<kind-subdir>/<YYYY-MM-DD>_<message_id>[_<unique_id>].<ext>`."""
    ext = _extension(item)
    stem = f"{item.date.astimezone(timezone.utc):%Y-%m-%d}_{item.message_id}"
    if item.unique_id:
        stem = f"{stem}_{_slug(item.unique_id, '0')}"
    budget = MAX_NAME_LENGTH - len(ext) - 1
    return Path(root) / _slug(chat_slug, "chat") / item.kind.subdir / f"{stem[:budget]}.{ext}"


def part_path(dest: Path) -> Path:
    """The partial file for an in-flight download; the adapter writes it, the fetcher cleans it."""
    return dest.with_name(f"{dest.name}.part")
