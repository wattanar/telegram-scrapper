"""Plan a download pass over a source, then run it through a bounded task pool."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

from .domain import (
    ChatSummary,
    FetchResult,
    ItemState,
    MediaItem,
    MediaKind,
    Selection,
    chat_slug,
    destination,
    part_path,
)
from .ledger import Ledger
from .ports import NullSink, ProgressSink, TelegramSource


@dataclass(frozen=True, slots=True)
class Plan:
    chat: ChatSummary
    root: Path
    items: tuple[MediaItem, ...]
    skipped: tuple[MediaItem, ...]
    counts: dict[MediaKind, int]
    total_bytes: int


@dataclass(frozen=True, slots=True)
class RunSummary:
    plan: Plan
    results: tuple[FetchResult, ...]
    total_bytes: int
    elapsed: float

    def count(self, state: ItemState) -> int:
        return sum(1 for result in self.results if result.state is state)


async def plan(
    source: TelegramSource,
    chat: str,
    selection: Selection,
    root: Path,
    ledger: Ledger,
    *,
    dedupe: bool = True,
) -> Plan:
    resolved = await source.resolve_chat(chat)
    items: list[MediaItem] = []
    skipped: list[MediaItem] = []
    counts: Counter[MediaKind] = Counter()
    seen_unique: set[str] = set()
    total_bytes = 0

    async with aclosing(source.iter_media(chat, selection)) as stream:
        async for item in stream:
            if ledger.get(item.chat_id, item.message_id) is not None:
                skipped.append(item)
                continue
            if dedupe and item.unique_id:
                # A repost inside the same pass has no ledger entry yet, so the
                # pass tracks what it has already planned.
                if ledger.has_unique(item.chat_id, item.unique_id) or item.unique_id in seen_unique:
                    skipped.append(item)
                    continue
                seen_unique.add(item.unique_id)
            items.append(item)
            counts[item.kind] += 1
            total_bytes += item.size or 0
            if selection.limit is not None and len(items) >= selection.limit:
                break

    return Plan(
        chat=resolved,
        root=Path(root),
        items=tuple(items),
        skipped=tuple(skipped),
        counts=dict(counts),
        total_bytes=total_bytes,
    )


async def run(
    source: TelegramSource,
    prepared: Plan,
    ledger: Ledger,
    *,
    concurrency: int = 4,
    sink: ProgressSink | None = None,
) -> RunSummary:
    progress = sink or NullSink()
    started = time.monotonic()
    slug = chat_slug(prepared.chat)
    results: list[FetchResult] = []

    for item in prepared.skipped:
        entry = ledger.get(item.chat_id, item.message_id)
        path = Path(entry.path) if entry is not None else destination(prepared.root, slug, item)
        results.append(FetchResult(item, ItemState.SKIPPED, path, 0))
        _clear_part_beside(path)

    progress.run_started(len(prepared.items), prepared.total_bytes)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    completed: list[FetchResult] = []

    async def capture(item: MediaItem) -> None:
        async with semaphore:
            progress.item_started(item)
            result = await _download_one(
                source, item, destination(prepared.root, slug, item), ledger, progress
            )
            completed.append(result)
            progress.item_finished(result)

    async with asyncio.TaskGroup() as group:
        for item in prepared.items:
            group.create_task(capture(item))

    results.extend(completed)
    return RunSummary(
        plan=prepared,
        results=tuple(results),
        total_bytes=sum(result.bytes_written for result in completed),
        elapsed=time.monotonic() - started,
    )


def _clear_part_beside(dest: Path) -> None:
    # A partial file next to a finished one can only be a leftover from a crash
    # after the rename; keeping it would grow the output tree forever.
    part = part_path(dest)
    if dest.exists() and part.exists():
        part.unlink()


async def _download_one(
    source: TelegramSource,
    item: MediaItem,
    dest: Path,
    ledger: Ledger,
    sink: ProgressSink,
) -> FetchResult:
    part = part_path(dest)

    def on_progress(written: int, total: int | None) -> None:
        sink.item_progress(item, written, total)

    try:
        written_to = await source.download(item, dest, on_progress)
    except asyncio.CancelledError:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:  # one bad item never aborts the run
        return FetchResult(item, ItemState.FAILED, dest, 0, f"{type(exc).__name__}: {exc}")

    size = written_to.stat().st_size if written_to.exists() else (item.size or 0)
    ledger.record(item, written_to, size)
    return FetchResult(item, ItemState.DONE, written_to, size)
