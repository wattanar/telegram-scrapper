"""The `tgmedia` command line: parse argv, render with rich, stay thin."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Callable
from datetime import date, datetime, time, timezone
from pathlib import Path

from rich.console import Console
from rich.filesize import decimal
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from . import config
from .domain import (
    KINDS,
    FetchResult,
    ItemState,
    MediaItem,
    MediaKind,
    MediaKindError,
    Selection,
)
from .fetcher import Plan, RunSummary, plan, run
from .ledger import LEDGER_RELPATH, Ledger
from .ports import AmbiguousChat, ChatNotFound, NotAuthorized, NullSink, TelegramSource

DEFAULT_OUT = "downloads"
DEFAULT_CONCURRENCY = 4
DEFAULT_RETRY = 3
EXIT_OK = 0
EXIT_FAILED_ITEMS = 1
EXIT_USAGE = 2
EXIT_NOT_LOGGED_IN = 3
EXIT_INTERRUPTED = 130

SourceFactory = Callable[[int], TelegramSource]

_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
}
_SIZE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]*)")


class RichSink:
    def __init__(self, console: Console) -> None:
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=console,
        )
        self._overall: TaskID | None = None
        self._files: dict[int, TaskID] = {}
        self._total_items = 0
        self._items_done = 0
        self._bytes = 0

    def run_started(self, total_items: int, total_bytes: int) -> None:
        self._total_items = total_items
        self._progress.start()
        self._overall = self._progress.add_task(f"0/{total_items} items", total=total_bytes or None)

    def item_started(self, item: MediaItem) -> None:
        self._files[item.message_id] = self._progress.add_task(
            f"{item.kind.name} {item.message_id}", total=item.size
        )

    def item_progress(self, item: MediaItem, written: int, total: int | None) -> None:
        task = self._files.get(item.message_id)
        if task is None:
            return
        if total:
            self._progress.update(task, total=total)
        self._progress.update(task, completed=written)

    def item_finished(self, result: FetchResult) -> None:
        task = self._files.pop(result.item.message_id, None)
        if task is not None:
            self._progress.remove_task(task)
        self._bytes += result.bytes_written
        self._items_done += 1
        if self._overall is None:
            return
        self._progress.advance(self._overall, result.bytes_written)
        self._progress.update(
            self._overall,
            description=f"{self._items_done}/{self._total_items} items, {decimal(self._bytes)}",
        )

    def stop(self) -> None:
        self._progress.stop()


def _parse_size(value: str) -> int:
    match = _SIZE_PATTERN.fullmatch(value.strip().lower())
    if match is None or match.group(2) not in _SIZE_UNITS:
        raise argparse.ArgumentTypeError(f"invalid size {value!r}; use bytes or a k/m/g suffix such as 10k")
    number, unit = match.groups()
    return int(float(number) * _SIZE_UNITS[unit])


def _parse_instant(value: str) -> tuple[datetime, bool]:
    text = value.strip()
    try:
        day = date.fromisoformat(text)
    except ValueError:
        day = None
    if day is not None:
        return datetime.combine(day, time.min, tzinfo=timezone.utc), True
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; use YYYY-MM-DD or an ISO 8601 timestamp"
        ) from None
    return (moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment), False


def _parse_since(value: str) -> datetime:
    moment, _ = _parse_instant(value)
    return moment


def _parse_until(value: str) -> datetime:
    moment, date_only = _parse_instant(value)
    return datetime.combine(moment.date(), time.max, tzinfo=moment.tzinfo) if date_only else moment


def _parse_kinds(value: str) -> frozenset[MediaKind]:
    names = [name for name in value.replace(",", " ").split() if name]
    if not names:
        raise argparse.ArgumentTypeError("--kind needs at least one kind")
    try:
        return frozenset(MediaKind.from_name(name) for name in names)
    except MediaKindError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tgmedia", description="Download media from a Telegram chat.")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("login", help="sign in with a user account and save the session")

    chats = commands.add_parser("chats", help="list your chats")
    chats.add_argument("--search", help="case-insensitive filter on title, username, or id")

    fetch = commands.add_parser("fetch", help="download media from one chat")
    fetch.add_argument("chat", help="@username, t.me link, numeric id, or a title substring")
    fetch.add_argument(
        "--kind",
        type=_parse_kinds,
        default=None,
        help=f"comma-separated kinds: {', '.join(kind.name for kind in KINDS)}",
    )
    fetch.add_argument("--since", type=_parse_since, default=None, help="YYYY-MM-DD or ISO timestamp")
    fetch.add_argument("--until", type=_parse_until, default=None, help="YYYY-MM-DD or ISO timestamp")
    fetch.add_argument("--min-size", type=_parse_size, default=None, help="smallest file, e.g. 10k")
    fetch.add_argument("--max-size", type=_parse_size, default=None, help="largest file, e.g. 2g")
    fetch.add_argument("--limit", type=int, default=None, help="stop after this many new items")
    fetch.add_argument("--thread", type=int, default=None, help="forum topic id")
    fetch.add_argument("--out", default=DEFAULT_OUT, help=f"destination root (default {DEFAULT_OUT})")
    fetch.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="parallel downloads")
    fetch.add_argument("--retry", type=int, default=DEFAULT_RETRY, help="retries per failed item")
    fetch.add_argument("--no-dedupe", action="store_true", help="download reposts of a known file")
    fetch.add_argument("--dry-run", action="store_true", help="print the plan and download nothing")
    fetch.add_argument("--json", action="store_true", help="print the plan or summary as JSON on stdout")
    fetch.add_argument("--quiet", action="store_true", help="no progress bars, no summary")

    return parser


def _plan_json(prepared: Plan) -> dict[str, object]:
    chat = prepared.chat
    return {
        "mode": "plan",
        "chat": {"id": chat.chat_id, "title": chat.title, "username": chat.username, "kind": chat.kind},
        "root": str(prepared.root),
        "counts": {kind.name: count for kind, count in prepared.counts.items()},
        "total_items": len(prepared.items),
        "total_bytes": prepared.total_bytes,
        "skipped": len(prepared.skipped),
    }


def _summary_json(summary: RunSummary) -> dict[str, object]:
    chat = summary.plan.chat
    return {
        "mode": "summary",
        "chat": {"id": chat.chat_id, "title": chat.title, "username": chat.username, "kind": chat.kind},
        "root": str(summary.plan.root),
        "downloaded": summary.count(ItemState.DONE),
        "skipped": summary.count(ItemState.SKIPPED),
        "failed": summary.count(ItemState.FAILED),
        "total_bytes": summary.total_bytes,
        "elapsed_seconds": round(summary.elapsed, 3),
        "items": [
            {
                "message_id": result.item.message_id,
                "kind": result.item.kind.name,
                "state": str(result.state),
                "path": str(result.path) if result.path is not None else None,
                "bytes": result.bytes_written,
                "error": result.error,
            }
            for result in sorted(summary.results, key=lambda result: result.item.message_id)
        ],
    }


def _print_plan(console: Console, prepared: Plan) -> None:
    chat = prepared.chat
    console.print(f"[bold]{chat.title}[/bold] ({chat.kind}) -> {prepared.root}")
    table = Table(box=None, show_header=True, header_style="bold")
    table.add_column("kind")
    table.add_column("items", justify="right")
    for kind in KINDS:
        count = prepared.counts.get(kind)
        if count:
            table.add_row(kind.name, str(count))
    console.print(table)
    console.print(
        f"[bold]total[/bold] {len(prepared.items)} items, {decimal(prepared.total_bytes)}"
        f"; {len(prepared.skipped)} already captured"
    )


def _print_summary(console: Console, summary: RunSummary) -> None:
    chat = summary.plan.chat
    console.print(f"[bold]{chat.title}[/bold] -> {summary.plan.root}")
    table = Table(box=None, show_header=True, header_style="bold")
    table.add_column("state")
    table.add_column("items", justify="right")
    for state in (ItemState.DONE, ItemState.SKIPPED, ItemState.FAILED):
        table.add_row(state.value, str(summary.count(state)))
    console.print(table)
    console.print(f"[bold]{decimal(summary.total_bytes)}[/bold] in {summary.elapsed:.1f}s")
    for result in summary.results:
        if result.state is ItemState.FAILED:
            console.print(f"[red]failed[/red] message {result.item.message_id}: {result.error}")


def _print_chats(console: Console, rows: list) -> None:
    table = Table(box=None, header_style="bold")
    table.add_column("id")
    table.add_column("kind")
    table.add_column("username")
    table.add_column("title")
    for chat in rows:
        table.add_row(str(chat.chat_id), chat.kind, chat.username or "", chat.title)
    console.print(table)


def _print_candidates(console: Console, error: AmbiguousChat) -> None:
    console.print(f"[red]{error.query!r} matches {len(error.candidates)} chats[/red]; pass one of:")
    _print_chats(console, error.candidates)


def default_source_factory(retry: int) -> TelegramSource:
    # Imported here so `--help` and argument errors never load Telethon.
    from .telegram import TelethonSource

    credentials = config.read_credentials()
    if credentials is None:
        raise NotAuthorized("no api_id/api_hash configured")
    return TelethonSource(credentials.api_id, credentials.api_hash, config.read_session(), retry=retry)


async def _login() -> int:
    console = Console(stderr=True)
    credentials = config.read_credentials()
    if credentials is None:
        raw_id = input("api_id: ").strip()
        raw_hash = input("api_hash: ").strip()
        if not raw_hash:
            console.print("[red]api_hash is required[/red]")
            return EXIT_USAGE
        try:
            credentials = config.Credentials(api_id=int(raw_id), api_hash=raw_hash)
        except ValueError:
            console.print("[red]api_id must be a number[/red]")
            return EXIT_USAGE
        if input(f"write these to {config.config_path()}? [y/N] ").strip().lower() in ("y", "yes"):
            console.print(f"wrote {config.write_credentials(credentials)}")

    from .telegram import interactive_login

    result = await interactive_login(credentials.api_id, credentials.api_hash)
    config.write_session(result.session)
    console.print(f"logged in as [bold]{result.name}[/bold]; session saved to {config.session_path()}")
    return EXIT_OK


async def _chats(args: argparse.Namespace, factory: SourceFactory) -> int:
    console = Console(stderr=True)
    source = factory(DEFAULT_RETRY)
    try:
        rows = await source.list_chats(args.search)
    except NotAuthorized as exc:
        return _report_not_logged_in(console, exc)
    finally:
        await source.close()
    _print_chats(Console(), rows)
    return EXIT_OK


async def _fetch(args: argparse.Namespace, factory: SourceFactory) -> int:
    errors = Console(stderr=True)
    output = Console()
    selection = Selection(
        kinds=args.kind,
        since=args.since,
        until=args.until,
        min_size=args.min_size,
        max_size=args.max_size,
        limit=args.limit,
        thread=args.thread,
    )
    root = Path(args.out).expanduser()
    ledger = Ledger.load(root / LEDGER_RELPATH)
    source: TelegramSource | None = None
    sink: NullSink | RichSink = NullSink()
    try:
        source = factory(args.retry)
        prepared = await plan(source, args.chat, selection, root, ledger, dedupe=not args.no_dedupe)
        if args.dry_run:
            if args.json:
                print(json.dumps(_plan_json(prepared)))
            elif not args.quiet:
                _print_plan(output, prepared)
            return EXIT_OK
        if prepared.items and not args.quiet and not args.json:
            sink = RichSink(errors)
        summary = await run(source, prepared, ledger, concurrency=args.concurrency, sink=sink)
    except AmbiguousChat as exc:
        _print_candidates(errors, exc)
        return EXIT_USAGE
    except ChatNotFound as exc:
        errors.print(f"[red]unknown chat[/red]: {exc}")
        return EXIT_USAGE
    except NotAuthorized as exc:
        return _report_not_logged_in(errors, exc)
    finally:
        ledger.flush()
        if isinstance(sink, RichSink):
            sink.stop()
        if source is not None:
            await source.close()

    if args.json:
        print(json.dumps(_summary_json(summary)))
    elif not args.quiet:
        _print_summary(output, summary)
    return EXIT_FAILED_ITEMS if summary.count(ItemState.FAILED) else EXIT_OK


def _report_not_logged_in(console: Console, error: NotAuthorized) -> int:
    console.print(f"[red]not logged in[/red]: {error}. Run `tgmedia login` first")
    return EXIT_NOT_LOGGED_IN


def main(argv: list[str] | None = None, *, source_factory: SourceFactory | None = None) -> int:
    args = _build_parser().parse_args(argv)
    factory = source_factory or default_source_factory
    try:
        if args.command == "login":
            return asyncio.run(_login())
        if args.command == "chats":
            return asyncio.run(_chats(args, factory))
        return asyncio.run(_fetch(args, factory))
    except KeyboardInterrupt:
        Console(stderr=True).print("[yellow]interrupted[/yellow]")
        return EXIT_INTERRUPTED
    except config.ConfigError as exc:
        Console(stderr=True).print(f"[red]config error[/red]: {exc}")
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
