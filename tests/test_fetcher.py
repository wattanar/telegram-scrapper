from __future__ import annotations

from pathlib import Path

from fakes import DEMO_CHAT, FakeSource, fake_item
from tgmedia.domain import ItemState, MediaKind, Selection, destination, part_path
from tgmedia.fetcher import plan, run
from tgmedia.ledger import Ledger


def open_ledger(root: Path) -> Ledger:
    return Ledger.load(root / ".tgmedia" / "ledger.json")


def states(summary) -> dict[int, ItemState]:
    return {result.item.message_id: result.state for result in summary.results}


async def fetch(source: FakeSource, root: Path, selection: Selection | None = None, **kwargs):
    ledger = open_ledger(root)
    prepared = await plan(source, "@demo", selection or Selection(), root, ledger)
    summary = await run(source, prepared, ledger, **kwargs)
    return prepared, summary


async def test_run_writes_every_matching_item_to_its_destination(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    source = FakeSource(
        (
            fake_item(message_id=1, data=b"one", file_name="first.jpg"),
            fake_item(message_id=2, data=b"two-two", kind="video", file_name="second.MP4"),
            fake_item(message_id=3, data=b"three", kind="document"),
        )
    )
    selection = Selection()

    prepared, summary = await fetch(source, root, selection)

    assert prepared.counts == {
        MediaKind.from_name("photo"): 1,
        MediaKind.from_name("video"): 1,
        MediaKind.from_name("document"): 1,
    }
    assert prepared.total_bytes == len(b"one") + len(b"two-two") + len(b"three")
    assert states(summary) == {1: ItemState.DONE, 2: ItemState.DONE, 3: ItemState.DONE}

    first = root / "demo" / "photos" / "2024-05-01_1.jpg"
    second = root / "demo" / "videos" / "2024-05-01_2.mp4"
    third = root / "demo" / "documents" / "2024-05-01_3.bin"
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two-two"
    assert third.read_bytes() == b"three"


async def test_second_run_downloads_nothing(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    source = FakeSource((fake_item(message_id=1, data=b"one"), fake_item(message_id=2, data=b"two")))
    await fetch(source, root)
    calls_after_first = len(source.download_calls)

    prepared, summary = await fetch(source, root)

    assert len(source.download_calls) == calls_after_first == 2
    assert prepared.items == ()
    assert states(summary) == {1: ItemState.SKIPPED, 2: ItemState.SKIPPED}
    assert summary.total_bytes == 0


async def test_one_failure_does_not_stop_the_others(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    source = FakeSource(
        (
            fake_item(message_id=1, data=b"one"),
            fake_item(message_id=2, data=b"two"),
            fake_item(message_id=3, data=b"three"),
        ),
        failures={2: RuntimeError("telegram said no")},
    )

    _, summary = await fetch(source, root)

    assert states(summary) == {1: ItemState.DONE, 2: ItemState.FAILED, 3: ItemState.DONE}
    failed = next(result for result in summary.results if result.item.message_id == 2)
    assert failed.error is not None and "telegram said no" in failed.error
    assert (root / "demo" / "photos" / "2024-05-01_2.jpg").exists() is False
    assert (root / "demo" / "photos" / "2024-05-01_1.jpg").read_bytes() == b"one"
    assert (root / "demo" / "photos" / "2024-05-01_3.jpg").read_bytes() == b"three"


async def test_a_failed_item_still_leaves_the_three_done_items_in_the_disk_ledger(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    source = FakeSource(
        tuple(fake_item(message_id=message_id, data=b"x") for message_id in range(1, 5)),
        failures={2: RuntimeError("telegram said no")},
    )

    await fetch(source, root)

    ledger = open_ledger(root)
    assert ledger.get(DEMO_CHAT.chat_id, 1) is not None
    assert ledger.get(DEMO_CHAT.chat_id, 2) is None
    assert ledger.get(DEMO_CHAT.chat_id, 3) is not None
    assert ledger.get(DEMO_CHAT.chat_id, 4) is not None


async def test_failed_item_is_retried_on_the_next_run(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    item = fake_item(message_id=1, data=b"one")
    source = FakeSource((item,), failures={1: OSError("network died")})
    await fetch(source, root)

    source.failures.clear()
    _, summary = await fetch(source, root)

    assert states(summary) == {1: ItemState.DONE}
    assert (root / "demo" / "photos" / "2024-05-01_1.jpg").read_bytes() == b"one"


async def test_leftover_part_file_without_a_final_file_is_fetched_again(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    item = fake_item(message_id=4, data=b"fresh")
    dest = destination(root, "demo", item)
    part = part_path(dest)
    part.parent.mkdir(parents=True)
    part.write_bytes(b"half a fil")

    source = FakeSource((item,))
    _, summary = await fetch(source, root)

    assert states(summary) == {4: ItemState.DONE}
    assert dest.read_bytes() == b"fresh"
    assert part.exists() is False


async def test_part_file_beside_a_finished_file_is_removed(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    item = fake_item(message_id=5, data=b"kept")
    await fetch(FakeSource((item,)), root)
    dest = destination(root, "demo", item)
    part = part_path(dest)
    part.write_bytes(b"stale")

    _, summary = await fetch(FakeSource((item,)), root)

    assert states(summary) == {5: ItemState.SKIPPED}
    assert dest.read_bytes() == b"kept"
    assert part.exists() is False


async def test_dedupe_skips_a_repost_with_the_same_unique_id_in_one_pass(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    source = FakeSource(
        (
            fake_item(message_id=1, data=b"original", unique_id="900"),
            fake_item(message_id=2, data=b"repost", unique_id="900"),
        )
    )

    _, summary = await fetch(source, root)

    assert states(summary) == {1: ItemState.DONE, 2: ItemState.SKIPPED}
    assert len(source.download_calls) == 1


async def test_dedupe_skips_a_repost_seen_in_an_earlier_run(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    await fetch(FakeSource((fake_item(message_id=1, data=b"original", unique_id="900"),)), root)

    source = FakeSource((fake_item(message_id=2, data=b"repost", unique_id="900"),))
    prepared, summary = await fetch(source, root)

    assert prepared.items == ()
    assert states(summary) == {2: ItemState.SKIPPED}
    assert source.download_calls == []


async def test_no_dedupe_downloads_the_repost(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    await fetch(FakeSource((fake_item(message_id=1, data=b"original", unique_id="900"),)), root)
    source = FakeSource((fake_item(message_id=2, data=b"repost", unique_id="900"),))
    ledger = open_ledger(root)

    prepared = await plan(source, "@demo", Selection(), root, ledger, dedupe=False)
    summary = await run(source, prepared, ledger)

    assert states(summary) == {2: ItemState.DONE}
    assert len(source.download_calls) == 1


async def test_limit_caps_the_run(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    source = FakeSource(tuple(fake_item(message_id=index) for index in range(1, 6)))

    prepared, summary = await fetch(source, root, Selection(limit=2))

    assert len(prepared.items) == 2
    assert len(source.download_calls) == 2
    assert states(summary) == {1: ItemState.DONE, 2: ItemState.DONE}


async def test_writes_land_in_the_kind_subdirectory_of_the_chat_slug(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    item = fake_item(message_id=8, data=b"gif", kind="gif", file_name="loop.gif")
    await fetch(FakeSource((item,), chat=DEMO_CHAT), root)

    assert (root / "demo" / "gifs" / "2024-05-01_8.gif").read_bytes() == b"gif"
