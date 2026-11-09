"""Writing star ratings into audio files, on a background thread.

The file being rated is usually the one playing. So a copy is tagged and then
swapped in with one atomic rename: the player keeps reading the file it has
open, and a crash or a full disk can never leave a damaged song behind.
"""

import os
import shutil
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from src.track_metadata import write_rating


def write_rating_safely(path: Path, stars: int) -> None:
    """Store a rating in a file via a hidden copy next to it. Raises on failure."""
    target = path.resolve()  # a symbolic link must stay a link to the same file
    # Hidden, so a scan skips it; it keeps the suffix mutagen goes by.
    temp = target.with_name(f".{target.stem}.musichive-rating{target.suffix}")
    try:
        shutil.copy2(target, temp)
        write_rating(temp, stars)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


class _Signals(QObject):
    failed = Signal(str, str)  # file name, what went wrong


class _WriteTask(QRunnable):
    def __init__(self, path: Path, stars: int, signals: _Signals) -> None:
        super().__init__()
        self._path = path
        self._stars = stars
        self._signals = signals

    def run(self) -> None:
        try:
            write_rating_safely(self._path, self._stars)
        except Exception as error:  # never let a thread die silently
            self._signals.failed.emit(self._path.name, str(error))


class RatingWriter(QObject):
    """Writes ratings one at a time, in the order they were given."""

    failed = Signal(str, str)  # file name, what went wrong

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._signals = _Signals(self)
        self._signals.failed.connect(self.failed)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

    def write(self, path: Path, stars: int) -> None:
        self._pool.start(_WriteTask(path, stars, self._signals))

    def shutdown(self) -> None:
        """Let the ratings still waiting finish."""
        self._pool.waitForDone(10000)
