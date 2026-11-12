"""Putting the playing track's cover on the internet, so Discord can show it.

Discord only shows pictures from a public web address, so the cover is taken
from the file and uploaded to a file host that needs no account: Litterbox
first, then Catbox or Uguu when it refuses. Tracks with the same cover share
one upload, and links are remembered between runs until they are close to
expiring. A host that refuses is left alone for a while, longer each time.
"""

import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
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
# Litterbox deletes files after about 48 hours, whatever KEEP_TIME says, so a link
# is only used for 36 (and it is checked once per session as well).
REUSE_SECONDS = 36 * 3600
RETRY_SECONDS = 600  # how long to wait after a failed upload; it doubles with each failure
MAX_RETRY_SECONDS = 4 * 3600
UPLOAD_TIMEOUT = 30  # seconds
CHECK_TIMEOUT = 15  # seconds
MAX_SIDE = 128  # pixels
SETTINGS_KEY = f"discordCoverLinks{MAX_SIDE}v2"  # changing the size starts afresh
OLD_SETTINGS_KEY = f"discordCoverLinks{MAX_SIDE}"  # links kept for 70 hours: too long
OLD_REUSE_SECONDS = 70 * 3600


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


def _curl(png: bytes, url: str, *fields: str) -> str:
    """Send the PNG as a form to `url`; returns the server's answer."""
    command = ["curl", "--silent", "--show-error", "--max-time", str(UPLOAD_TIMEOUT)]
    for form_field in fields:
        command += ["-F", form_field]
    result = subprocess.run([*command, url], input=png, capture_output=True)
    if result.returncode != 0:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout.decode("utf-8", errors="replace").strip()


def _plain_link(answer: str, png: bytes) -> str:
    if not answer.startswith("https://"):
        raise ValueError(f"unexpected answer ({len(png)} bytes sent): {answer[:120]}")
    return answer


def _upload_litterbox(png: bytes) -> str:
    return _plain_link(_curl(
        png, UPLOAD_URL, "reqtype=fileupload", f"time={KEEP_TIME}",
        "fileToUpload=@-;filename=mh.png;type=image/png",  # Litterbox refused cover.jpg
    ), png)


def _upload_catbox(png: bytes) -> str:
    return _plain_link(_curl(
        png, "https://catbox.moe/user/api.php", "reqtype=fileupload",
        "fileToUpload=@-;filename=mh.png;type=image/png",
    ), png)


def _upload_uguu(png: bytes) -> str:
    answer = _curl(png, "https://uguu.se/upload", "files[]=@-;filename=mh.png;type=image/png")
    try:
        data = json.loads(answer)
        link = data["files"][0]["url"] if data.get("success") else ""
    except (ValueError, KeyError, IndexError, TypeError):
        link = ""
    return _plain_link(link or answer, png)


@dataclass(frozen=True)
class _Host:
    name: str
    upload: Callable[[bytes], str]
    reuse_seconds: int  # how long a link is used: well before the host deletes the file


# In the order they are tried.
HOSTS = (
    _Host("Litterbox", _upload_litterbox, REUSE_SECONDS),  # deletes after about 48 hours
    _Host("Catbox", _upload_catbox, 30 * 24 * 3600),  # keeps files for good
    _Host("Uguu", _upload_uguu, 150 * 60),  # deletes after 3 hours
)


class _Signals(QObject):
    # cover key, address ("" if every host refused), the host used (-1 for none),
    # and [(host, error)] for the hosts that refused
    finished = Signal(str, str, int, object)
    checked = Signal(str, str, bool)  # cover key, address, whether it is gone


def _is_gone(address: str) -> bool:
    """Whether Litterbox has deleted the file. Doubtful answers count as still there."""
    result = subprocess.run(
        ["curl", "--silent", "--head", "--output", "/dev/null", "--write-out", "%{http_code}",
         "--max-time", str(CHECK_TIMEOUT), address],
        capture_output=True,
    )
    return result.stdout.decode("ascii", errors="replace").strip() in ("404", "410")


class _CheckTask(QRunnable):
    def __init__(self, cover_key: str, address: str, signals: _Signals) -> None:
        super().__init__()
        self._cover_key = cover_key
        self._address = address
        self._signals = signals

    def run(self) -> None:
        try:
            gone = _is_gone(self._address)
        except Exception:  # no curl, no network: keep using the link
            gone = False
        self._signals.checked.emit(self._cover_key, self._address, gone)


class _UploadTask(QRunnable):
    def __init__(self, cover_key: str, path: Path, signals: _Signals, is_image: bool,
                 hosts_to_try: Callable[[], list[int]]) -> None:
        super().__init__()
        self._cover_key = cover_key
        self._path = path
        self._signals = signals
        self._is_image = is_image  # the picture itself, not a song with one inside
        self._hosts_to_try = hosts_to_try  # asked when it runs: it may have waited its turn

    def run(self) -> None:
        refused: list[tuple[int, str]] = []
        try:
            if self._is_image:
                cover = self._path.read_bytes()
            else:
                _info, cover = read_track_info(self._path)
            if not cover:
                raise ValueError("the song no longer has a cover")
            png = _as_png(cover)
        except Exception as error:  # the cover itself is the problem, not a host
            self._signals.finished.emit(self._cover_key, "", -1, [(-1, str(error))])
            return
        for host in self._hosts_to_try():
            try:
                address = HOSTS[host].upload(png)
            except Exception as error:  # never let a thread die silently
                refused.append((host, str(error)))
                continue
            self._signals.finished.emit(self._cover_key, address, host, refused)
            return
        self._signals.finished.emit(self._cover_key, "", -1, refused)


class DiscordCovers(QObject):
    """Hands out public addresses for covers, uploading them in the background."""

    uploaded = Signal(str)  # the cover key of a newly uploaded cover

    def __init__(self, settings: QSettings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._links: dict[str, list] = self._load()  # cover key -> [address, expiry time]
        self._uploading: set[str] = set()
        self._retry_at: dict[str, float] = {}  # cover key -> when to try again
        # A host that refused is left alone for a while, longer each time: trying it
        # again with every new song would only get the connection blocked.
        self._host_paused_until = [0.0] * len(HOSTS)
        self._host_failures = [0] * len(HOSTS)  # in a row
        self._checked: set[str] = set()  # links found to work (or being checked) this session
        self._uploads: dict[str, tuple[Path, bool]] = {}  # what to upload again if a link is gone
        self._signals = _Signals(self)
        self._signals.finished.connect(self._on_finished)
        self._signals.checked.connect(self._on_checked)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._check_pool = QThreadPool(self)  # checks do not wait behind uploads
        self._check_pool.setMaxThreadCount(2)

    def link_for(self, path: Path, cover_key: str | None, is_image: bool = False) -> str:
        """The cover's public address, or "" for now if it is still being uploaded.

        `path` is a song whose cover it is, or with `is_image` the picture itself
        (a podcast's cover).
        """
        if not cover_key:
            return ""
        self._uploads[cover_key] = (path, is_image)
        link = self._links.get(cover_key)
        if link and link[1] > time.time():
            if cover_key not in self._checked:  # once per session: is it still there?
                self._checked.add(cover_key)
                self._check_pool.start(_CheckTask(cover_key, link[0], self._signals))
            return link[0]
        self._upload(cover_key)
        return ""

    def hosts_to_try(self) -> list[int]:
        """The hosts not waiting after refusing, in order (read from the upload thread)."""
        now = time.monotonic()
        return [i for i, until in enumerate(self._host_paused_until) if now >= until]

    def _upload(self, cover_key: str) -> None:
        now = time.monotonic()
        if cover_key in self._uploading or now < self._retry_at.get(cover_key, 0):
            return
        if not self.hosts_to_try():
            return  # every host is waiting after refusing
        path, is_image = self._uploads[cover_key]
        self._uploading.add(cover_key)
        self._pool.start(_UploadTask(cover_key, path, self._signals, is_image, self.hosts_to_try))

    def _on_checked(self, cover_key: str, address: str, gone: bool) -> None:
        """A saved link turned out to be deleted: upload the cover again."""
        link = self._links.get(cover_key)
        if not gone or link is None or link[0] != address:
            return
        del self._links[cover_key]
        self._save()
        self._upload(cover_key)  # `uploaded` then lets Discord show the new link

    def shutdown(self) -> None:
        self._check_pool.clear()
        self._pool.waitForDone(3000)

    def _on_finished(self, cover_key: str, address: str, host: int, refused: list) -> None:
        self._uploading.discard(cover_key)
        now = time.monotonic()
        for refusing, error in refused:
            if refusing < 0:  # the cover could not be read: try that one again later
                self._retry_at[cover_key] = now + RETRY_SECONDS
                print(f"MusicHive: no cover to upload for Discord: {error}", file=sys.stderr)
                continue
            self._host_failures[refusing] += 1
            wait = min(RETRY_SECONDS * 2 ** (self._host_failures[refusing] - 1), MAX_RETRY_SECONDS)
            self._host_paused_until[refusing] = now + wait
            print(
                f"MusicHive: {HOSTS[refusing].name} refused the cover for Discord: {error[:160]} "
                f"(left alone for {wait // 60} minutes)",
                file=sys.stderr,
            )
        if not address:
            if host < 0 and refused and refused[0][0] >= 0:  # every host refused
                self._retry_at[cover_key] = min(self._host_paused_until)
            return
        self._host_failures[host] = 0
        self._links[cover_key] = [address, time.time() + HOSTS[host].reuse_seconds]
        self._checked.add(cover_key)  # just uploaded: no need to check it
        self._save()
        self.uploaded.emit(cover_key)

    def _load(self) -> dict[str, list]:
        try:
            links = json.loads(self._settings.value(SETTINGS_KEY, "{}", type=str))
        except ValueError:
            links = {}
        if not isinstance(links, dict):
            links = {}
        if self._settings.contains(OLD_SETTINGS_KEY):
            # Links from before were trusted for 70 hours; now 36 hours after uploading.
            try:
                old = json.loads(self._settings.value(OLD_SETTINGS_KEY, "{}", type=str))
            except ValueError:
                old = {}
            if isinstance(old, dict):
                shorter = OLD_REUSE_SECONDS - REUSE_SECONDS
                for key, link in old.items():
                    links.setdefault(key, [link[0], link[1] - shorter])
            self._settings.remove(OLD_SETTINGS_KEY)
        return links

    def _save(self) -> None:
        now = time.time()
        self._links = {key: link for key, link in self._links.items() if link[1] > now}
        self._settings.setValue(SETTINGS_KEY, json.dumps(self._links))
