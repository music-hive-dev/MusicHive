"""Artist pictures and descriptions from the internet, kept on disk.

Pictures come from Deezer, which needs no account. Descriptions come from
Last.fm, whose API no longer serves real artist pictures. Artists are looked
up one at a time, and only once something asks for them, so scrolling through
the side list is what fetches the rows on screen.

Everything found is saved in one folder shared by all libraries: for each
artist a .json file (the description) and a .jpg file (the picture).
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage

from src.lastfm import USER_AGENT, LastFmError, fetch_artist_bio
from src.library_db import track_key
from src.playlist_models import make_square_icon

DEEZER_SEARCH_URL = "https://api.deezer.com/search/artist"
DEEZER_NO_DATA = 800  # Deezer's error code for "nothing found"
REQUEST_TIMEOUT = 10  # seconds
LOOKUP_PAUSE_SECONDS = 0.2  # between artists, to stay well inside the rate limits
OFFLINE_RETRY_SECONDS = 300  # after a failed lookup, wait this long before the next
RETRY_MISSING_SECONDS = 30 * 24 * 3600  # look again for what was not found
THUMBNAIL_SIDE = 96  # pixels, for the rows of the side list

# Last.fm's answers that mean "try again later": could not connect, unexpected
# answer, operation failed, service offline, temporary error, rate limit.
LASTFM_RETRY_CODES = (-1, 0, 8, 11, 16, 29)


class LookupFailed(Exception):
    """The internet could not be asked. Nothing is known about the artist yet."""


@dataclass
class ArtistRecord:
    description: str
    has_image: bool
    bio_checked: bool  # False if Last.fm has not been asked yet
    fetched: float  # when this was looked up, in seconds since 1970


# ----- the web services (these run on a background thread) -----


def _download(url: str) -> bytes | None:
    """The contents of `url`, or None if it does not exist."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if 400 <= error.code < 500 and error.code != 429:
            return None
        raise LookupFailed(f"HTTP {error.code} {error.reason}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise LookupFailed(str(error)) from error


def _same_artist(first: str, second: str) -> bool:
    return track_key(first, "") == track_key(second, "")


def find_deezer_picture(name: str) -> str | None:
    """The address of the artist's picture, or None without an exact name match.

    A search also returns similar names; showing nothing beats showing
    somebody else's face.
    """
    query = urllib.parse.urlencode({"q": name, "limit": "10"})
    body = _download(f"{DEEZER_SEARCH_URL}?{query}")
    if body is None:
        return None
    try:
        payload = json.loads(body)
    except ValueError as error:
        raise LookupFailed("unexpected answer from Deezer") from error

    error = payload.get("error")
    if error:
        if isinstance(error, dict) and error.get("code") == DEEZER_NO_DATA:
            return None
        raise LookupFailed(f"Deezer: {error}")

    for artist in payload.get("data", []):
        if _same_artist(artist.get("name", ""), name):
            url = artist.get("picture_big") or ""
            # Artists without a picture get an address with an empty image id.
            return url if url and "/artist//" not in url else None
    return None


def _write_file(path: Path, data: bytes) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


def lookup_artist(name: str, api_key: str, record_path: Path, image_path: Path) -> ArtistRecord:
    """Ask the internet about an artist and save what it says."""
    picture_url = find_deezer_picture(name)
    picture = _download(picture_url) if picture_url else None

    description = ""
    bio_checked = False
    if api_key:
        try:
            description = fetch_artist_bio(api_key, name)
            bio_checked = True
        except LastFmError as error:
            if error.code in LASTFM_RETRY_CODES:
                raise LookupFailed(error.message) from error
            print(f"MusicHive: Last.fm artist lookup failed: {error.message}", file=sys.stderr)

    record_path.parent.mkdir(parents=True, exist_ok=True)
    if picture:
        _write_file(image_path, picture)
    record = ArtistRecord(description, image_path.exists(), bio_checked, time.time())
    _write_file(record_path, json.dumps(asdict(record)).encode("utf-8"))
    return record


def _read_record(path: Path) -> ArtistRecord | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ArtistRecord(
            str(data["description"]),
            bool(data["has_image"]),
            bool(data["bio_checked"]),
            float(data["fetched"]),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None


class _Signals(QObject):
    done = Signal(str, object)  # artist name, ArtistRecord
    failed = Signal(str, str)  # artist name, what went wrong


class _LookupTask(QRunnable):
    def __init__(
        self, name: str, api_key: str, record_path: Path, image_path: Path, signals: _Signals
    ) -> None:
        super().__init__()
        self._name = name
        self._api_key = api_key
        self._record_path = record_path
        self._image_path = image_path
        self._signals = signals

    def run(self) -> None:
        try:
            record = lookup_artist(
                self._name, self._api_key, self._record_path, self._image_path
            )
            time.sleep(LOOKUP_PAUSE_SECONDS)
        except Exception as error:  # never let a thread die silently
            self._signals.failed.emit(self._name, str(error))
        else:
            self._signals.done.emit(self._name, record)


# ----- what the rest of the program uses -----


class ArtistInfoService(QObject):
    """Answers with what is known about an artist, and looks up what is not.

    Asking never waits for the internet: when a lookup finishes,
    `artist_updated` says that there is more to show for that artist.
    """

    artist_updated = Signal(str)  # the artist's name

    def __init__(self, api_key: str, folder: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._api_key = api_key
        self._folder = folder
        self._records: dict[str, ArtistRecord | None] = {}  # by name; None = nothing yet
        self._thumbnails: dict[str, QImage | None] = {}
        self._requested: set[str] = set()  # asked for during this session
        self._waiting: list[str] = []  # the newest request is served first
        self._busy = False
        self._retry_at = 0.0

        self._signals = _Signals(self)
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

    def description(self, name: str) -> str:
        record = self._record(name)
        return record.description if record is not None else ""

    def image(self, name: str) -> QImage | None:
        """The artist's picture at full size, or None if there is none."""
        record = self._record(name)
        if record is None or not record.has_image:
            return None
        image = QImage(str(self._path(name, ".jpg")))
        return None if image.isNull() else image

    def thumbnail(self, name: str) -> QImage | None:
        """The artist's picture, square and small enough for a list row."""
        self._record(name)  # starts a lookup if one is needed
        if name not in self._thumbnails:
            image = self.image(name)
            self._thumbnails[name] = (
                None if image is None else make_square_icon(image, THUMBNAIL_SIDE)
            )
        return self._thumbnails[name]

    def shutdown(self) -> None:
        self._waiting.clear()
        self._pool.waitForDone(3000)

    # ----- internal -----

    def _path(self, name: str, suffix: str) -> Path:
        digest = hashlib.sha1(track_key(name, "").encode("utf-8")).hexdigest()
        return self._folder / f"{digest}{suffix}"

    def _record(self, name: str) -> ArtistRecord | None:
        if name not in self._records:
            self._records[name] = _read_record(self._path(name, ".json"))
        record = self._records[name]
        if self._needs_lookup(record):
            self._request(name)
        return record

    def _needs_lookup(self, record: ArtistRecord | None) -> bool:
        if record is None or (self._api_key and not record.bio_checked):
            return True
        complete = record.has_image and bool(record.description)
        return not complete and time.time() - record.fetched > RETRY_MISSING_SECONDS

    def _request(self, name: str) -> None:
        if name in self._requested or time.monotonic() < self._retry_at:
            return
        self._requested.add(name)
        self._waiting.append(name)
        self._start_next()

    def _start_next(self) -> None:
        if self._busy or not self._waiting:
            return
        self._busy = True
        name = self._waiting.pop()
        self._pool.start(
            _LookupTask(
                name,
                self._api_key,
                self._path(name, ".json"),
                self._path(name, ".jpg"),
                self._signals,
            )
        )

    def _on_done(self, name: str, record: ArtistRecord) -> None:
        self._busy = False
        self._records[name] = record
        self._thumbnails.pop(name, None)
        self.artist_updated.emit(name)
        self._start_next()

    def _on_failed(self, name: str, message: str) -> None:
        """Probably no connection: stop for a while, and let the rows ask again later."""
        print(f"MusicHive: artist lookup failed for {name}: {message}", file=sys.stderr)
        self._busy = False
        self._retry_at = time.monotonic() + OFFLINE_RETRY_SECONDS
        self._requested.difference_update(self._waiting)
        self._requested.discard(name)
        self._waiting.clear()
