"""Writing tags into audio files, on a background thread.

The file being changed may be the one playing. So a copy is tagged and then
swapped in with one atomic rename: the player keeps reading the file it has
open, and a crash or a full disk can never leave a damaged song behind.
"""

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from src.library_db import ScannedTrack
from src.library_scanner import scan_file
from src.track_metadata import TagChanges, write_rating, write_tag_changes


def edit_file_safely(path: Path, edit: Callable[[Path], None]) -> None:
    """Run `edit` on a hidden copy of a file, then replace the file. Raises on failure."""
    target = path.resolve()  # a symbolic link must stay a link to the same file
    # Hidden, so a scan skips it; it keeps the suffix mutagen goes by.
    temp = target.with_name(f".{target.stem}.musichive-edit{target.suffix}")
    try:
        shutil.copy2(target, temp)
        edit(temp)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _write_lrc(song: Path, text: str) -> None:
    """Save lyrics next to a song. Empty text removes the file."""
    target = song.with_suffix(".lrc")
    if not text.strip():
        target.unlink(missing_ok=True)
        return
    temp = target.with_name(f".{target.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, target)


@dataclass
class TagJob:
    """What to change about one song."""

    path: Path
    changes: TagChanges
    lrc_text: str | None = None  # new text for the .lrc file; None leaves it alone


@dataclass
class SaveResult:
    db_path: Path
    jobs: list[TagJob]
    tracks: list[ScannedTrack] = field(default_factory=list)  # as the files are now
    covers: dict[str, bytes] = field(default_factory=dict)  # cover_key -> thumbnail
    failures: list[tuple[str, str]] = field(default_factory=list)  # file name, reason


class _Signals(QObject):
    rating_failed = Signal(str, str)  # file name, what went wrong
    tags_saved = Signal(object)  # a SaveResult


class _RatingTask(QRunnable):
    def __init__(self, path: Path, stars: int, signals: _Signals) -> None:
        super().__init__()
        self._path = path
        self._stars = stars
        self._signals = signals

    def run(self) -> None:
        try:
            edit_file_safely(self._path, lambda temp: write_rating(temp, self._stars))
        except Exception as error:  # never let a thread die silently
            self._signals.rating_failed.emit(self._path.name, str(error))


class _SaveTask(QRunnable):
    def __init__(self, result: SaveResult, root: Path, signals: _Signals) -> None:
        super().__init__()
        self._result = result
        self._root = root
        self._signals = signals

    def run(self) -> None:
        result = self._result
        undecodable: set[str] = set()
        for job in result.jobs:
            try:
                if not job.changes.is_empty:
                    edit_file_safely(job.path, lambda temp: write_tag_changes(temp, job.changes))
                if job.lrc_text is not None:
                    _write_lrc(job.path, job.lrc_text)
                result.tracks.append(scan_file(job.path, self._root, result.covers, undecodable))
            except Exception as error:  # one bad file must not stop the others
                result.failures.append((job.path.name, str(error)))
        self._signals.tags_saved.emit(result)


class TagWriter(QObject):
    """Writes to audio files one job at a time, in the order they were given."""

    rating_failed = Signal(str, str)  # file name, what went wrong
    tags_saved = Signal(object)  # a SaveResult

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._signals = _Signals(self)
        self._signals.rating_failed.connect(self.rating_failed)
        self._signals.tags_saved.connect(self.tags_saved)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

    def write_rating(self, path: Path, stars: int) -> None:
        self._pool.start(_RatingTask(path, stars, self._signals))

    def save_tags(self, jobs: list[TagJob], root: Path, db_path: Path) -> None:
        self._pool.start(_SaveTask(SaveResult(db_path, jobs), root, self._signals))

    def shutdown(self) -> None:
        """Let the work still waiting finish."""
        self._pool.waitForDone(30000)
