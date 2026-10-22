from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tgmedia.domain import (
    KINDS,
    MediaItem,
    MediaKind,
    MediaKindError,
    Selection,
    chat_slug,
    destination,
    part_path,
)


def make_item(**overrides: object) -> MediaItem:
    fields: dict[str, object] = {
        "chat_id": -1001234567890,
        "message_id": 7,
        "kind": MediaKind.from_name("photo"),
        "size": 1024,
        "date": datetime(2024, 5, 1, 12, 30, tzinfo=timezone.utc),
        "file_name": None,
        "unique_id": None,
        "download_ref": None,
    }
    fields.update(overrides)
    return MediaItem(**fields)  # type: ignore[arg-type]


def test_registry_names_are_unique_and_resolvable() -> None:
    names = [kind.name for kind in KINDS]

    assert len(names) == len(set(names))
    assert MediaKind.from_name("PHOTO").attr == "photo"
    assert MediaKind.from_name(" video ").subdir == "videos"


def test_media_kind_from_name_rejects_junk() -> None:
    with pytest.raises(MediaKindError):
        MediaKind.from_name("hologram")

    with pytest.raises(MediaKindError):
        MediaKind.from_name("")


def test_selection_matches_kinds() -> None:
    photos_only = Selection(kinds=frozenset({MediaKind.from_name("photo")}))

    assert photos_only.matches(make_item()) is True
    assert photos_only.matches(make_item(kind=MediaKind.from_name("video"))) is False
    assert Selection().matches(make_item(kind=MediaKind.from_name("video"))) is True


def test_selection_date_bounds_are_inclusive() -> None:
    since = datetime(2024, 5, 1, tzinfo=timezone.utc)
    until = datetime(2024, 5, 2, tzinfo=timezone.utc)
    selection = Selection(since=since, until=until)

    assert selection.matches(make_item(date=since)) is True
    assert selection.matches(make_item(date=until)) is True
    assert selection.matches(make_item(date=datetime(2024, 4, 30, 23, 59, tzinfo=timezone.utc))) is False
    assert selection.matches(make_item(date=datetime(2024, 5, 2, 0, 1, tzinfo=timezone.utc))) is False


def test_selection_size_bounds_and_unknown_size() -> None:
    selection = Selection(min_size=1000, max_size=2000)

    assert selection.matches(make_item(size=1000)) is True
    assert selection.matches(make_item(size=2000)) is True
    assert selection.matches(make_item(size=999)) is False
    assert selection.matches(make_item(size=2001)) is False
    assert selection.matches(make_item(size=None)) is True


def test_destination_uses_file_name_extension() -> None:
    item = make_item(file_name="Holiday Photo.JPG")

    assert destination(Path("/root"), "photo", item) == Path("/root/photo/photos/2024-05-01_7.jpg")


def test_destination_falls_back_to_kind_extension() -> None:
    item = make_item(kind=MediaKind.from_name("voice"), file_name=None)

    assert destination(Path("/root"), "photo", item) == Path("/root/photo/voice/2024-05-01_7.ogg")


def test_destination_appends_unique_id() -> None:
    item = make_item(unique_id="4455", file_name="report.pdf")

    assert destination(Path("/root"), "photo", item).name == "2024-05-01_7_4455.pdf"


def test_destination_sanitizes_chat_slug() -> None:
    result = destination(Path("/root"), "Café 日本 / ../..", make_item())

    assert result == Path("/root/Café-日本/photos/2024-05-01_7.jpg")


def test_destination_blocks_traversal_from_slug_and_file_name() -> None:
    result = destination(Path("/root"), "../evil", make_item(file_name="../../etc/passwd"))

    assert result == Path("/root/evil/photos/2024-05-01_7.jpg")
    assert ".." not in result.parts


def test_destination_keeps_names_under_the_length_budget() -> None:
    item = make_item(unique_id="9" * 400, file_name="x" * 400 + ".jpeg")

    name = destination(Path("/root"), "t" * 400, item).name

    assert len(name) <= 160
    assert name.endswith(".jpeg")


def test_chat_slug_prefers_username_then_title() -> None:
    from tgmedia.domain import ChatSummary

    named = ChatSummary(chat_id=-1001, title="T", username="chan", kind="channel")
    titled = ChatSummary(chat_id=-1002, title="Group Title", username=None, kind="group")

    assert chat_slug(named) == "chan"
    assert chat_slug(titled) == "Group Title"


def test_part_path_is_a_sibling_of_the_final_file() -> None:
    assert part_path(Path("/root/photos/2024-05-01_7.jpg")) == Path("/root/photos/2024-05-01_7.jpg.part")
