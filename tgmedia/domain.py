"""Data shapes shared by the CLI, the fetcher, the ledger, and the Telegram adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class MediaKindError(ValueError):
    """A kind name that is not in the registry."""


class LinkError(ValueError):
    """A link tgmedia cannot turn into one message reference."""


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


_LINK_HOSTS = ("t.me", "www.t.me", "telegram.me", "telegram.dog")
_INVITE_SEGMENTS = ("joinchat", "addstickers", "share")
_NUMERIC = re.compile(r"-?\d+")


@dataclass(frozen=True, slots=True)
class MessageRef:
    """The one message the user named. A comment id never travels without its post id."""

    post_id: int
    comment_id: int | None = None

    @property
    def label(self) -> str:
        """`message 795` or `comment 11953 on message 795`, for the sentences the CLI prints."""
        if self.comment_id is None:
            return f"message {self.post_id}"
        return f"comment {self.comment_id} on message {self.post_id}"


@dataclass(frozen=True, slots=True)
class ChatQuery:
    """One parsed chat argument: what to resolve, plus the message it names when it names one."""

    target: str | int | None
    ref: MessageRef | None = None


def _link_parts(text: str) -> tuple[list[str], dict[str, list[str]]] | None:
    lowered = text.lower()
    for scheme in ("http://", "https://"):
        if lowered.startswith(scheme):
            split = urlsplit(text)
            break
    else:
        split = urlsplit(f"https://{text}")
    if split.netloc.lower() not in _LINK_HOSTS:
        return None
    path = [part for part in split.path.split("/") if part]
    return path, parse_qs(split.query, keep_blank_values=True)


def _message_ref(post_id: int | None, params: dict[str, list[str]]) -> MessageRef | None:
    """Build the one message a link names; this is where a comment id gets validated."""
    raw = params.get("comment")
    if raw is None:
        return MessageRef(post_id) if post_id is not None else None
    if post_id is None:
        raise LinkError("a comment link needs a post id, as in t.me/<name>/<post>?comment=<id>")
    value = raw[-1].strip()
    if not value.isdigit() or int(value) < 1:
        raise LinkError(f"comment id {value!r} is not a message id")
    return MessageRef(post_id, int(value))


def parse_chat_query(text: str) -> ChatQuery:
    """Map `@name`, `name`, a t.me link, a numeric id, or a title to one typed query."""
    stripped = text.strip()
    if not stripped:
        return ChatQuery(None)
    if stripped.startswith("@"):
        return ChatQuery(stripped)
    link = _link_parts(stripped)
    if link is None:
        return ChatQuery(int(stripped) if _NUMERIC.fullmatch(stripped) else None)
    parts, params = link
    if parts and parts[0] == "s" and len(parts) > 1:
        parts = parts[1:]
    if not parts:
        return ChatQuery(None, _message_ref(None, params))
    if parts[0] == "c" and len(parts) > 1 and parts[1].isdigit():
        post_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        return ChatQuery(int(f"-100{parts[1]}"), _message_ref(post_id, params))
    if parts[0].startswith("+") or parts[0] in _INVITE_SEGMENTS:
        raise ValueError(
            "invite links are not supported; use @username, a t.me/<name> link, or an id"
        )
    post_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    return ChatQuery(f"@{parts[0]}", _message_ref(post_id, params))


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
