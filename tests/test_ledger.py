from __future__ import annotations

import json
from pathlib import Path

from fakes import fake_item
from tgmedia.ledger import BATCH, Ledger, LedgerEntry


def ledger_path(root: Path) -> Path:
    return root / ".tgmedia" / "ledger.json"


def test_record_survives_a_reload(tmp_path: Path) -> None:
    root = tmp_path / "out"
    item = fake_item(message_id=11, unique_id="4455")
    dest = root / "photos" / "2024-05-01_11_4455.jpg"

    ledger = Ledger.load(ledger_path(root))
    ledger.record(item, dest, 1234)
    ledger.flush()

    reloaded = Ledger.load(ledger_path(root))
    entry = reloaded.get(-1001234567890, 11)
    assert entry == LedgerEntry(path=str(dest), size=1234, unique="4455")
    assert reloaded.has_unique(-1001234567890, "4455") is True
    assert reloaded.get(-1001234567890, 12) is None


def test_batch_boundary_saves_and_flush_finalizes_the_tail(tmp_path: Path) -> None:
    root = tmp_path / "out"
    ledger = Ledger.load(ledger_path(root))
    for message_id in range(1, BATCH + 1):
        ledger.record(fake_item(message_id=message_id), root / f"{message_id}.jpg", message_id)

    reloaded = Ledger.load(ledger_path(root))
    assert all(reloaded.get(-1001234567890, message_id) is not None for message_id in range(1, BATCH + 1))

    for message_id in range(BATCH + 1, BATCH + 6):
        ledger.record(fake_item(message_id=message_id), root / f"{message_id}.jpg", message_id)
    assert len(json.loads(ledger_path(root).read_text())["items"]) == BATCH

    ledger.flush()
    ledger.flush()

    raw = json.loads(ledger_path(root).read_text())
    assert raw["version"] == 1
    assert len(raw["items"]) == BATCH + 5

    ledger.record(fake_item(message_id=BATCH + 6), root / f"{BATCH + 6}.jpg", 0)
    ledger.flush()
    ledger.flush()

    assert len(json.loads(ledger_path(root).read_text())["items"]) == BATCH + 6


def test_save_is_atomic_and_leaves_no_tmp_file(tmp_path: Path) -> None:
    root = tmp_path / "out"
    ledger = Ledger.load(ledger_path(root))

    for message_id in range(BATCH):
        ledger.record(fake_item(message_id=message_id), root / f"{message_id}.jpg", 3)

    assert ledger_path(root).exists()
    assert list((root / ".tgmedia").glob("*.tmp")) == []


def test_corrupt_ledger_starts_empty(tmp_path: Path) -> None:
    root = tmp_path / "out"
    ledger_path(root).parent.mkdir(parents=True)
    ledger_path(root).write_text("{ this is not json")

    ledger = Ledger.load(ledger_path(root))

    assert ledger.get(-1001234567890, 1) is None
    assert ledger.has_unique(-1001234567890, "4455") is False


def test_missing_ledger_starts_empty(tmp_path: Path) -> None:
    ledger = Ledger.load(ledger_path(tmp_path / "nothing-here"))

    assert ledger.get(-1001234567890, 1) is None


def test_resave_is_byte_stable(tmp_path: Path) -> None:
    root = tmp_path / "out"
    ledger = Ledger.load(ledger_path(root))
    ledger.record(fake_item(message_id=2, unique_id="77"), root / "b.jpg", 9)
    ledger.record(fake_item(message_id=1), root / "a.jpg", 4)
    ledger.flush()
    first = ledger_path(root).read_bytes()

    Ledger.load(ledger_path(root)).save()

    assert ledger_path(root).read_bytes() == first


def test_ledger_file_shape_is_versioned_json(tmp_path: Path) -> None:
    root = tmp_path / "out"
    ledger = Ledger.load(ledger_path(root))
    ledger.record(fake_item(message_id=5), root / "c.jpg", 2)
    ledger.flush()

    raw = json.loads(ledger_path(root).read_text())

    assert raw["version"] == 1
    assert raw["items"]["-1001234567890:5"] == {"path": str(root / "c.jpg"), "size": 2, "unique": None}
    assert raw["unique_documents"] == {}
