"""Run one interrupted `tgmedia fetch` in its own interpreter.

The defect this guards against only shows up in a process's stderr at shutdown,
when the event loop finalizes a task whose exception nobody retrieved.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from fakes import FakeSource, fake_item
from tgmedia.cli import main
from tgmedia.domain import MediaItem


class InterruptingSource(FakeSource):
    async def download(
        self,
        item: MediaItem,
        dest: Path,
        on_progress: Callable[[int, int | None], None],
    ) -> Path:
        if item.message_id == 2:
            raise KeyboardInterrupt
        return await super().download(item, dest, on_progress)


def fetch_until_interrupted(root: Path) -> int:
    source = InterruptingSource(
        (fake_item(message_id=1, data=b"first"), fake_item(message_id=2, data=b"second")),
    )
    return main(
        ["fetch", "@demo", "--out", str(root), "--concurrency", "1"],
        source_factory=lambda retry: source,
    )


if __name__ == "__main__":
    sys.exit(fetch_until_interrupted(Path(sys.argv[1])))
