"""Putting the playing track's cover on the internet, so Discord can show it.

Discord only shows pictures from a public web address, so the cover is taken
from the file and uploaded to Litterbox, which needs no account and deletes
files by itself after a few days. Tracks with the same cover share one upload,
and links are remembered between runs until they are close to expiring.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import (
    QBuffer,
    QIODevice,
    QObject,
    QRunnable,
    QSettings,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import QImage

from src.track_metadata import read_track_info

UPLOAD_URL = "https://litterbox.catbox.moe/resources/internals/api.php"
KEEP_TIME = "72h"  # how long Litterbox keeps a file: 1h, 12h, 24h or 72h
REUSE_SECONDS = 70 * 3600  # stop using a link before Litterbox deletes its file
RETRY_SECONDS = 600  # how long to wait after a failed upload
UPLOAD_TIMEOUT = 30  # seconds
MAX_SIDE = 128  # pixels
SETTINGS_KEY = f"discordCoverLinks{MAX_SIDE}"  # changing the size starts afresh


def _as_png(data: bytes) -> bytes:
    """The cover as a PNG of at most MAX_SIDE pixels, without the original's metadata."""
    image = QImage.fromData(data)
    if image.isNull():
        raise ValueError("the cover image could not be read")
    if max(image.width(), image.height()) > MAX_SIDE:
        image = image.scaled(
            MAX_SIDE,
            MAX_SIDE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise ValueError("the cover image could not be converted")
    return bytes(buffer.data())


def _upload(png: bytes) -> str:
    """Upload a PNG and return its public address."""
    result = subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            str(UPLOAD_TIMEOUT),
            "-F",
            "reqtype=fileupload",
            "-F",
            f"time={KEEP_TIME}",
            "-F",
            "fileToUpload=@-;filename=mh.png;type=image/png",  # Litterbox refused cover.jpg
            UPLOAD_URL,
        ],
        input=png,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    answer = result.stdout.decode("utf-8", errors="replace").strip()
    if not answer.startswith("https://"):
        raise ValueError(f"unexpected answer from the server ({len(png)} bytes sent): {answer[:200]}")
    return answer


class _Signals(QObject):
    finished = Signal(str, str, str)  # cover key, address (empty on failure), error


class _UploadTask(QRunnable):
    def __init__(self, cover_key: str, path: Path, signals: _Signals) -> None:
        super().__init__()
        self._cover_key = cover_key
        self._path = path
        self._signals = signals

    def run(self) -> None:
        try:
            _info, cover = read_track_info(self._path)
            if not cover:
                raise ValueError("the track no longer has a cover")
            address = _upload(_as_png(cover))
            self._signals.finished.emit(self._cover_key, address, "")
        except Exception as error:  # never let a thread die silently
            self._signals.finished.emit(self._cover_key, "", str(error))


class DiscordCovers(QObject):
    """Hands out public addresses for covers, uploading them in the background."""

    uploaded = Signal(str)  # the cover key of a newly uploaded cover

    def __init__(self, settings: QSettings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._links: dict[str, list] = self._load()  # cover key -> [address, expiry time]
        self._uploading: set[str] = set()
        self._retry_at: dict[str, float] = {}  # cover key -> when to try again
        self._signals = _Signals(self)
        self._signals.finished.connect(self._on_finished)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

    def link_for(self, path: Path, cover_key: str | None) -> str:
        """The cover's public address, or "" for now if it is still being uploaded."""
        if not cover_key:
            return ""
        link = self._links.get(cover_key)
        if link and link[1] > time.time():
            return link[0]
        if cover_key not in self._uploading and time.monotonic() >= self._retry_at.get(cover_key, 0):
            self._uploading.add(cover_key)
            self._pool.start(_UploadTask(cover_key, path, self._signals))
        return ""

    def shutdown(self) -> None:
        self._pool.waitForDone(3000)

    def _on_finished(self, cover_key: str, address: str, error: str) -> None:
        self._uploading.discard(cover_key)
        if not address:
            self._retry_at[cover_key] = time.monotonic() + RETRY_SECONDS
            print(f"MusicHive: could not upload the cover for Discord: {error}", file=sys.stderr)
            return
        self._links[cover_key] = [address, time.time() + REUSE_SECONDS]
        self._save()
        self.uploaded.emit(cover_key)

    def _load(self) -> dict[str, list]:
        try:
            links = json.loads(self._settings.value(SETTINGS_KEY, "{}", type=str))
        except ValueError:
            return {}
        return links if isinstance(links, dict) else {}

    def _save(self) -> None:
        now = time.time()
        self._links = {key: link for key, link in self._links.items() if link[1] > now}
        self._settings.setValue(SETTINGS_KEY, json.dumps(self._links))
