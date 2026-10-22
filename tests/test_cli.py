from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fakes import FakeSource, fake_item
from tgmedia.cli import main
from tgmedia.ports import NotAuthorized, SourceError


def factory(source: FakeSource):
    return lambda retry: source


def test_fetch_dry_run_json_reports_the_plan_and_downloads_nothing(tmp_path: Path, capsys) -> None:
    source = FakeSource(
        (
            fake_item(message_id=1, data=b"a" * 10),
            fake_item(message_id=2, data=b"b" * 20, kind="video"),
            fake_item(message_id=3, data=b"c" * 5),
        )
    )
    out = tmp_path / "downloads"

    code = main(["fetch", "@demo", "--out", str(out), "--dry-run", "--json"], source_factory=factory(source))

    plan = json.loads(capsys.readouterr().out)
    assert code == 0
    assert plan["mode"] == "plan"
    assert plan["counts"] == {"photo": 2, "video": 1}
    assert plan["total_items"] == 3
    assert plan["total_bytes"] == 35
    assert plan["skipped"] == 0
    assert plan["root"] == str(out)
    assert source.download_calls == []
    assert out.exists() is False


def test_fetch_unknown_chat_exits_2(tmp_path: Path, capsys) -> None:
    source = FakeSource(())

    code = main(["fetch", "@nowhere", "--out", str(tmp_path)], source_factory=factory(source))

    assert code == 2
    assert "unknown chat" in capsys.readouterr().err


def test_fetch_not_logged_in_exits_3(tmp_path: Path) -> None:
    def factory_that_is_not_logged_in(retry: int) -> FakeSource:
        raise NotAuthorized("the saved session is missing or expired")

    code = main(["fetch", "@demo", "--out", str(tmp_path)], source_factory=factory_that_is_not_logged_in)

    assert code == 3


@pytest.mark.parametrize(
    ("flag", "value", "expected"),
    [
        ("--min-size", "10x", "invalid size"),
        ("--max-size", "2 slices", "invalid size"),
        ("--since", "2024-13-45", "invalid date"),
        ("--until", "yesterday", "invalid date"),
        ("--kind", "hologram", "unknown kind"),
    ],
)
def test_bad_option_values_exit_2_with_a_message(
    flag: str, value: str, expected: str, tmp_path: Path, capsys
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["fetch", "@demo", "--out", str(tmp_path), flag, value], source_factory=factory(FakeSource(())))

    assert exit_info.value.code == 2
    assert expected in capsys.readouterr().err


def test_fetch_end_to_end_writes_files_with_expected_names_and_bytes(tmp_path: Path) -> None:
    source = FakeSource(
        (
            fake_item(message_id=101, data=b"\x89PNG-bytes", file_name="beach.PNG"),
            fake_item(message_id=102, data=b"video-bytes", kind="video", file_name="clip.mov"),
            fake_item(message_id=103, data=b"notes-bytes", kind="document", file_name="notes.txt"),
            fake_item(message_id=104, data=b"voice-bytes", kind="voice"),
            fake_item(message_id=105, data=b"audio-bytes", kind="audio", file_name="song.flac"),
            fake_item(message_id=106, data=b"sticker-bytes", kind="sticker"),
        )
    )
    root = tmp_path / "downloads"
    argv = ["fetch", "@demo", "--out", str(root)]

    assert main(argv, source_factory=factory(source)) == 0

    expected = {
        root / "demo" / "photos" / "2024-05-01_101.png": b"\x89PNG-bytes",
        root / "demo" / "videos" / "2024-05-01_102.mov": b"video-bytes",
        root / "demo" / "documents" / "2024-05-01_103.txt": b"notes-bytes",
        root / "demo" / "voice" / "2024-05-01_104.ogg": b"voice-bytes",
        root / "demo" / "audio" / "2024-05-01_105.flac": b"audio-bytes",
        root / "demo" / "stickers" / "2024-05-01_106.webp": b"sticker-bytes",
    }
    for path, data in expected.items():
        assert path.read_bytes() == data, path
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()) == sorted(
        [str(path.relative_to(root)) for path in expected] + [".tgmedia/ledger.json"]
    )
    assert list(root.rglob("*.part")) == []
    assert len(source.download_calls) == 6

    assert main(argv, source_factory=factory(source)) == 0
    assert len(source.download_calls) == 6
    for path, data in expected.items():
        assert path.read_bytes() == data, path


def test_interrupt_exits_130_and_keeps_completed_items_on_disk(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    child = Path(__file__).parent / "interrupt_child.py"

    finished = subprocess.run(
        [sys.executable, str(child), str(root)],
        cwd=Path(__file__).parent.parent,
        env={**os.environ, "PYTHONPATH": str(child.parent)},
        capture_output=True,
        text=True,
    )

    assert finished.returncode == 130
    first = root / "demo" / "photos" / "2024-05-01_1.jpg"
    assert first.read_bytes() == b"first"
    raw = json.loads((root / ".tgmedia" / "ledger.json").read_text())
    assert raw["items"] == {"-1001234567890:1": {"path": str(first), "size": 5, "unique": None}}
    assert "Task exception was never retrieved" not in finished.stderr
    assert "Traceback" not in finished.stderr


def test_fetch_json_summary_reports_states_and_exit_code(tmp_path: Path, capsys) -> None:
    source = FakeSource(
        (fake_item(message_id=1, data=b"ok"), fake_item(message_id=2, data=b"bad")),
        failures={2: OSError("disk full")},
    )

    code = main(
        ["fetch", "@demo", "--out", str(tmp_path / "out"), "--json"], source_factory=factory(source)
    )

    summary = json.loads(capsys.readouterr().out)
    assert code == 1
    assert summary["mode"] == "summary"
    assert summary["downloaded"] == 1
    assert summary["failed"] == 1
    assert summary["total_bytes"] == 2
    failed = next(item for item in summary["items"] if item["state"] == "failed")
    assert "disk full" in failed["error"]


def test_fetch_message_writes_only_the_named_message(tmp_path: Path) -> None:
    source = FakeSource(
        (
            fake_item(message_id=1, data=b"ignored"),
            fake_item(message_id=2, data=b"chosen", file_name="chosen.jpg"),
            fake_item(message_id=3, data=b"ignored too"),
        )
    )
    root = tmp_path / "downloads"

    code = main(["fetch", "@demo", "--message", "2", "--out", str(root)], source_factory=factory(source))

    written = root / "demo" / "photos" / "2024-05-01_2.jpg"
    assert code == 0
    assert written.read_bytes() == b"chosen"
    assert sorted(path.name for path in root.rglob("*") if path.is_file()) == [
        "2024-05-01_2.jpg",
        "ledger.json",
    ]
    assert [item.message_id for item in source.download_calls] == [2]


def test_fetch_message_link_writes_the_same_file(tmp_path: Path) -> None:
    source = FakeSource((fake_item(message_id=2, data=b"chosen", file_name="chosen.jpg"),))
    root = tmp_path / "downloads"

    code = main(["fetch", "https://t.me/demo/2", "--out", str(root)], source_factory=factory(source))

    assert code == 0
    assert (root / "demo" / "photos" / "2024-05-01_2.jpg").read_bytes() == b"chosen"
    assert [item.message_id for item in source.download_calls] == [2]


def test_message_flag_wins_over_the_link_id(tmp_path: Path) -> None:
    source = FakeSource((fake_item(message_id=2, data=b"flag"), fake_item(message_id=5, data=b"link")))

    code = main(
        ["fetch", "https://t.me/demo/5", "--message", "2", "--out", str(tmp_path)],
        source_factory=factory(source),
    )

    assert code == 0
    assert [item.message_id for item in source.download_calls] == [2]


def test_fetch_message_twice_downloads_it_once(tmp_path: Path, capsys) -> None:
    source = FakeSource((fake_item(message_id=2, data=b"chosen"),))
    root = tmp_path / "downloads"
    argv = ["fetch", "@demo", "--message", "2", "--out", str(root)]
    assert main(argv, source_factory=factory(source)) == 0
    capsys.readouterr()

    code = main([*argv, "--json"], source_factory=factory(source))

    summary = json.loads(capsys.readouterr().out)
    assert code == 0
    assert summary["downloaded"] == 0
    assert summary["skipped"] == 1
    assert len(source.download_calls) == 1


def test_fetch_message_rejected_by_a_kind_filter_exits_2(tmp_path: Path, capsys) -> None:
    source = FakeSource((fake_item(message_id=2, data=b"chosen"),))

    code = main(
        ["fetch", "@demo", "--message", "2", "--kind", "video", "--out", str(tmp_path)],
        source_factory=factory(source),
    )

    reported = capsys.readouterr().err
    assert code == 2
    assert "@demo" in reported
    assert "message 2" in reported
    assert "--kind" in reported
    assert source.download_calls == []


def test_fetch_message_dry_run_json_plans_one_item(tmp_path: Path, capsys) -> None:
    source = FakeSource((fake_item(message_id=2, data=b"chosen"), fake_item(message_id=3)))
    root = tmp_path / "downloads"

    code = main(
        ["fetch", "@demo", "--message", "2", "--dry-run", "--json", "--out", str(root)],
        source_factory=factory(source),
    )

    plan = json.loads(capsys.readouterr().out)
    assert code == 0
    assert plan["mode"] == "plan"
    assert plan["total_items"] == 1
    assert plan["counts"] == {"photo": 1}
    assert source.download_calls == []
    assert root.exists() is False


def raising_factory(error: Exception):
    def build(retry: int):
        raise error

    return build


def test_chats_without_credentials_reports_not_logged_in_instead_of_raising(capsys) -> None:
    code = main(
        ["chats"],
        source_factory=raising_factory(NotAuthorized("no api_id/api_hash configured")),
    )

    captured = capsys.readouterr()
    assert code == 3
    assert "no api_id/api_hash configured" in captured.err
    assert "tgmedia login" in captured.err


def test_chats_lists_dialogs(capsys) -> None:
    source = FakeSource((fake_item(),))

    code = main(["chats"], source_factory=factory(source))

    assert code == 0
    assert source.closed is True
    printed = capsys.readouterr().out
    assert "Demo Group" in printed
    assert "demo" in printed


def test_message_id_must_be_positive(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["fetch", "@demo", "--message", "0"])

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "message ids start at 1" in captured.err
    assert "Traceback" not in captured.err


def test_telegram_refusal_reports_the_reason_and_exits_1(capsys) -> None:
    source = FakeSource((fake_item(),))

    async def refuse(chat: str, selection):
        raise SourceError("Telegram asked to wait 120s before reading the history of @demo")
        yield  # never reached; the raise above makes this an async generator

    source.iter_media = refuse  # type: ignore[method-assign]

    code = main(["fetch", "@demo", "--out", "downloads"], source_factory=factory(source))

    captured = capsys.readouterr()
    assert code == 1
    assert "asked to wait 120s" in captured.err
    assert "Traceback" not in captured.err
