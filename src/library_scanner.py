"""Scanning a library folder into its database, on a background thread."""

import hashlib
import threading

from PySide6.QtCore import QBuffer, QIODevice, QObject, QRunnable, Qt, Signal
from PySide6.QtGui import QImage

from src import library_db
from src.library_db import LibraryInfo, ScannedTrack
from src.music_library import find_audio_files
from src.track_metadata import TrackInfo, read_track_info

THUMBNAIL_SIZE = 96  # pixels; drawn smaller, so it stays sharp on HiDPI screens


def make_thumbnail(data: bytes) -> bytes | None:
    """Turn cover image bytes into a square JPEG thumbnail, or None on failure."""
    image = QImage.fromData(data)
    if image.isNull():
        return None
    scaled = image.scaled(
        THUMBNAIL_SIZE,
        THUMBNAIL_SIZE,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = (scaled.width() - THUMBNAIL_SIZE) // 2
    y = (scaled.height() - THUMBNAIL_SIZE) // 2
    square = scaled.copy(x, y, THUMBNAIL_SIZE, THUMBNAIL_SIZE)

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not square.save(buffer, "JPEG", 90):
        return None
    return bytes(buffer.data())


class ScanSignals(QObject):
    progress = Signal(str, int, int)  # library id, tracks done, total tracks
    finished = Signal(str, int)  # library id, number of tracks saved
    failed = Signal(str, str)  # library id, message
    cancelled = Signal(str)  # library id


class ScanTask(QRunnable):
    """Reads every track of a library and saves the result to its database."""

    def __init__(self, library: LibraryInfo, signals: ScanSignals) -> None:
        super().__init__()
        self.setAutoDelete(False)  # Python keeps ownership of this object
        self._library = library
        self._signals = signals
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        library = self._library
        signals = self._signals
        try:
            if not library.root.is_dir():
                raise RuntimeError(f"The library folder was not found:\n{library.root}")

            files = find_audio_files(library.root)
            total = len(files)
            if total == 0 and library.track_count > 0:
                raise RuntimeError(
                    "No audio files were found, although this library had "
                    f"{library.track_count} tracks before. The drive may be "
                    "disconnected, so the saved library was kept unchanged."
                )
            signals.progress.emit(library.id, 0, total)

            tracks: list[ScannedTrack] = []
            covers: dict[str, bytes] = {}
            undecodable: set[str] = set()

            for done, path in enumerate(files, start=1):
                if self._cancel.is_set():
                    signals.cancelled.emit(library.id)
                    return

                try:
                    info, cover_bytes = read_track_info(path)
                except Exception:
                    info, cover_bytes = TrackInfo(title=path.stem), None

                if cover_bytes:
                    key = hashlib.sha1(cover_bytes).hexdigest()
                    if key not in covers and key not in undecodable:
                        thumbnail = make_thumbnail(cover_bytes)
                        if thumbnail is None:
                            undecodable.add(key)
                        else:
                            covers[key] = thumbnail
                    if key in covers:
                        info.cover_key = key

                try:
                    stat = path.stat()
                    size, modified = stat.st_size, stat.st_mtime
                except OSError:
                    size = modified = None

                tracks.append(
                    ScannedTrack(
                        rel_path=path.relative_to(library.root).as_posix(),
                        info=info,
                        file_size=size,
                        modified=modified,
                    )
                )
                if done % 10 == 0 or done == total:
                    signals.progress.emit(library.id, done, total)

            if self._cancel.is_set():
                signals.cancelled.emit(library.id)
                return

            library_db.save_scan(library.db_path, tracks, covers)
            signals.finished.emit(library.id, len(tracks))
        except Exception as error:
            signals.failed.emit(library.id, str(error))