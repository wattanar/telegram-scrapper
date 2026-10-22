"""The JSON ledger of what has already been captured, saved atomically after every write."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .domain import MediaItem

log = logging.getLogger(__name__)

LEDGER_VERSION = 1
LEDGER_RELPATH = Path(".tgmedia") / "ledger.json"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    path: str
    size: int | None
    unique: str | None

    @classmethod
    def from_json(cls, raw: object) -> LedgerEntry | None:
        if not isinstance(raw, dict):
            return None
        path = raw.get("path")
        if not isinstance(path, str):
            return None
        size = raw.get("size")
        unique = raw.get("unique")
        return cls(
            path=path,
            size=size if isinstance(size, int) else None,
            unique=unique if isinstance(unique, str) else None,
        )

    def to_json(self) -> dict[str, object]:
        return {"path": self.path, "size": self.size, "unique": self.unique}


def item_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def unique_key(chat_id: int, unique_id: str) -> str:
    return f"{chat_id}:{unique_id}"


class Ledger:
    """One writer, the orchestrator; every mutation is followed by an atomic save."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: dict[str, LedgerEntry] = {}
        self._unique: dict[str, str] = {}

    @classmethod
    def load(cls, path: Path) -> Ledger:
        ledger = cls(path)
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return ledger
        except (OSError, ValueError) as exc:
            log.warning("starting with an empty ledger: %s is unreadable (%s)", path, exc)
            return ledger
        if not isinstance(raw, dict) or raw.get("version") != LEDGER_VERSION:
            log.warning("starting with an empty ledger: %s has an unexpected shape", path)
            return ledger
        items = raw.get("items")
        if isinstance(items, dict):
            for key, value in items.items():
                entry = LedgerEntry.from_json(value)
                if entry is not None:
                    ledger._items[str(key)] = entry
        unique = raw.get("unique_documents")
        if isinstance(unique, dict):
            ledger._unique.update({str(k): v for k, v in unique.items() if isinstance(v, str)})
        return ledger

    def get(self, chat_id: int, message_id: int) -> LedgerEntry | None:
        return self._items.get(item_key(chat_id, message_id))

    def has_unique(self, chat_id: int, unique_id: str) -> bool:
        return unique_key(chat_id, unique_id) in self._unique

    def record(self, item: MediaItem, path: Path, size: int | None) -> None:
        self._items[item_key(item.chat_id, item.message_id)] = LedgerEntry(str(path), size, item.unique_id)
        if item.unique_id:
            self._unique[unique_key(item.chat_id, item.unique_id)] = str(path)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": LEDGER_VERSION,
            "items": {key: entry.to_json() for key, entry in self._items.items()},
            "unique_documents": dict(self._unique),
        }
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.path)
