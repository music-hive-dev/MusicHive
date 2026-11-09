"""Searching the internet for album images and genres, and comparing what is found.

Nothing found here is kept unless the user approves it in a comparison window.
"""

import hashlib
import json
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QDialog, QListWidgetItem, QWidget

from src.artist_info import LASTFM_RETRY_CODES, LookupFailed, download
from src.lastfm import LastFmError, fetch_top_tags
from src.lastfm_dialog import load_dialog
from src.library_db import track_key

MAX_COVERS = 6
MAX_GENRES = 12
GENRES_TICKED_AT_FIRST = 3
AUTO_TAG_PAUSE_SECONDS = 0.25  # between songs, to stay inside Last.fm's rate limit
AUTO_TAG_ALL = None  # as the number of genres per song: no limit
AUTO_TAG_CANCELLED = 0
COMPARE_SIDE = 360  # pixels


@dataclass
class FoundCover:
    data: bytes
    description: str  # for example "Abbey Road – The Beatles (Deezer)"


def _clean(text: str) -> str:
    return track_key(text, "").rstrip("\0")


def _same_album(found: str, wanted: str) -> bool:
    """Editions are often named "Album (Deluxe)", so either may contain the other."""
    first, second = _clean(found), _clean(wanted)
    return bool(first) and (first in second or second in first)


def _json_at(url: str) -> dict:
    body = download(url)
    if body is None:
        return {}
    try:
        data = json.loads(body)
    except ValueError as error:
        raise LookupFailed("unexpected answer from the server") from error
    return data if isinstance(data, dict) else {}


def _deezer_covers(artist: str, album: str) -> list[tuple[str, str]]:
    """(image address, description) of the albums Deezer finds."""
    query = urllib.parse.urlencode({"q": f"{artist} {album}".strip(), "limit": "10"})
    found = []
    for item in _json_at(f"https://api.deezer.com/search/album?{query}").get("data", []):
        url = item.get("cover_xl") or ""
        if url and _same_album(item.get("title", ""), album):
            name = item.get("artist", {}).get("name", "")
            found.append((url, f"{item.get('title')} – {name} (Deezer)"))
    return found


def _itunes_covers(artist: str, album: str) -> list[tuple[str, str]]:
    """(image address, description) of the albums Apple finds, at their largest size."""
    query = urllib.parse.urlencode(
        {"term": f"{artist} {album}".strip(), "entity": "album", "limit": "10"}
    )
    found = []
    for item in _json_at(f"https://itunes.apple.com/search?{query}").get("results", []):
        url = (item.get("artworkUrl100") or "").replace("100x100bb", "3000x3000bb")
        if url and _same_album(item.get("collectionName", ""), album):
            found.append((url, f"{item.get('collectionName')} – {item.get('artistName')} (Apple)"))
    return found


def find_covers(artist: str, album: str) -> list[FoundCover]:
    """Download the album images the services have. Raises if none could be asked."""
    candidates: list[tuple[str, str]] = []
    problem: LookupFailed | None = None
    for search in (_deezer_covers, _itunes_covers):
        try:
            candidates += search(artist, album)
        except LookupFailed as error:
            problem = error
    if not candidates and problem is not None:
        raise problem

    covers: list[FoundCover] = []
    seen: set[str] = set()
    for url, description in candidates:
        if len(covers) == MAX_COVERS:
            break
        try:
            data = download(url)
        except LookupFailed:
            continue
        digest = hashlib.sha1(data).hexdigest() if data else ""
        if data and digest not in seen:
            seen.add(digest)
            covers.append(FoundCover(data, description))
    return covers


def find_genres(
    api_key: str, artist: str, title: str, album: str, limit: int | None = MAX_GENRES
) -> list[str]:
    """Genres for a song, best first. Raises if Last.fm cannot be asked."""
    if not api_key:
        raise LookupFailed("this needs a Last.fm API key")
    names = fetch_top_tags(api_key, artist, title, album)[:limit]
    return [" ".join(word.capitalize() for word in name.split()) for name in names]


class _Signals(QObject):
    covers_found = Signal(int, object, str)  # request, list of FoundCover, error
    genres_found = Signal(int, object, str)  # request, list of genre names, error
    auto_tag_progress = Signal(int, int)  # songs done, total
    auto_tag_finished = Signal(object, int, str, bool)  # results, not found, error, stopped


@dataclass
class AutoTagSong:
    path: Path
    artist: str
    title: str
    album: str


class _AutoTagTask(QRunnable):
    """Looks up each song's genres. Nothing is written here."""

    def __init__(
        self,
        api_key: str,
        songs: list[AutoTagSong],
        limit: int | None,
        cancel: threading.Event,
        signals: _Signals,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._songs = songs
        self._limit = limit
        self._cancel = cancel
        self._signals = signals

    def run(self) -> None:
        results: list[tuple[Path, list[str]]] = []
        not_found = 0
        error = ""
        for done, song in enumerate(self._songs, start=1):
            if self._cancel.is_set():
                break
            genres: list[str] = []
            try:
                if song.artist:
                    genres = find_genres(
                        self._api_key, song.artist, song.title, song.album, self._limit
                    )
            except LastFmError as problem:
                if problem.code in LASTFM_RETRY_CODES:  # no point in trying the rest now
                    error = problem.message
                    break
            except Exception as problem:  # never let a thread die silently
                error = str(problem)
                break
            if genres:
                results.append((song.path, genres))
            else:
                not_found += 1
            self._signals.auto_tag_progress.emit(done, len(self._songs))
            time.sleep(AUTO_TAG_PAUSE_SECONDS)
        self._signals.auto_tag_finished.emit(results, not_found, error, self._cancel.is_set())


class _SearchTask(QRunnable):
    def __init__(self, request: int, search: Callable[[], list], signal) -> None:
        super().__init__()
        self._request = request
        self._search = search
        self._signal = signal

    def run(self) -> None:
        try:
            self._signal.emit(self._request, self._search(), "")
        except Exception as error:  # never let a thread die silently
            self._signal.emit(self._request, [], str(error))


class OnlineSearch(QObject):
    """Runs searches in the background. Each one's result names its request number."""

    covers_found = Signal(int, object, str)
    genres_found = Signal(int, object, str)
    auto_tag_progress = Signal(int, int)  # songs done, total
    auto_tag_finished = Signal(object, int, str, bool)  # results, not found, error, stopped

    def __init__(self, api_key: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._api_key = api_key
        self._request = 0
        self._signals = _Signals(self)
        self._signals.covers_found.connect(self.covers_found)
        self._signals.genres_found.connect(self.genres_found)
        self._signals.auto_tag_progress.connect(self.auto_tag_progress)
        self._signals.auto_tag_finished.connect(self.auto_tag_finished)
        self._cancel_auto_tag = threading.Event()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)

    def find_covers(self, artist: str, album: str) -> int:
        return self._start(lambda: find_covers(artist, album), self._signals.covers_found)

    def find_genres(self, artist: str, title: str, album: str) -> int:
        search = lambda: find_genres(self._api_key, artist, title, album)
        return self._start(search, self._signals.genres_found)

    def auto_tag(self, songs: list[AutoTagSong], limit: int | None) -> None:
        """Look up the genres of many songs, reporting through the auto_tag signals."""
        self._cancel_auto_tag.clear()
        self._pool.start(
            _AutoTagTask(self._api_key, songs, limit, self._cancel_auto_tag, self._signals)
        )

    def stop_auto_tag(self) -> None:
        self._cancel_auto_tag.set()

    def _start(self, search: Callable[[], list], signal) -> int:
        self._request += 1
        self._pool.start(_SearchTask(self._request, search, signal))
        return self._request

    def shutdown(self) -> None:
        self._cancel_auto_tag.set()
        self._pool.waitForDone(3000)


# ----- the comparison windows -----


def _show_image(label, info_label, data: bytes | None, description: str) -> None:
    """Show an image whole, with its description and size."""
    image = QImage.fromData(data) if data else QImage()
    if image.isNull():
        label.clear()
        label.setText("No image")
        info_label.setText(description)
        return
    scaled = image.scaled(
        COMPARE_SIDE,
        COMPARE_SIDE,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    label.setPixmap(QPixmap.fromImage(scaled))
    size = f"{image.width()} × {image.height()}"
    info_label.setText(f"{description}\n{size}" if description else size)


def compare_covers(
    ui_path: Path, parent: QWidget, current: bytes | None, found: list[FoundCover]
) -> FoundCover | None:
    """Show the current image beside what was found. Returns the approved one, or None."""
    d = load_dialog(ui_path, parent)
    position = [0]

    def show_found() -> None:
        cover = found[position[0]]
        _show_image(d.coverCompareFound, d.coverCompareFoundInfo, cover.data, cover.description)
        d.coverComparePosition.setText(f"{position[0] + 1} of {len(found)}")
        d.coverComparePrevious.setEnabled(position[0] > 0)
        d.coverCompareNext.setEnabled(position[0] < len(found) - 1)

    def step(amount: int) -> None:
        position[0] += amount
        show_found()

    _show_image(d.coverCompareCurrent, d.coverCompareCurrentInfo, current, "")
    show_found()
    d.coverComparePrevious.clicked.connect(lambda _checked=False: step(-1))
    d.coverCompareNext.clicked.connect(lambda _checked=False: step(1))
    d.coverCompareApprove.clicked.connect(d.accept)
    d.coverCompareDiscard.clicked.connect(d.reject)
    approved = d.exec() == QDialog.DialogCode.Accepted
    d.deleteLater()
    return found[position[0]] if approved else None


def ask_auto_tag_count(ui_path: Path, parent: QWidget, song_count: int) -> int | None:
    """Ask how many genres each song gets: a number, AUTO_TAG_ALL or AUTO_TAG_CANCELLED."""
    d = load_dialog(ui_path, parent)
    songs = f"{song_count} selected song{'' if song_count == 1 else 's'}"
    d.autoTagCountLabel.setText(f"This replaces the genres of {songs} with those found on Last.fm.")
    d.autoTagButtonBox.accepted.connect(d.accept)
    d.autoTagButtonBox.rejected.connect(d.reject)
    accepted = d.exec() == QDialog.DialogCode.Accepted
    index = d.autoTagGenreCount.currentIndex()
    last = d.autoTagGenreCount.count() - 1  # the "All" entry
    d.deleteLater()
    if not accepted:
        return AUTO_TAG_CANCELLED
    return AUTO_TAG_ALL if index == last else index + 1


def compare_genres(
    ui_path: Path, parent: QWidget, current: str, found: list[str]
) -> list[str] | None:
    """Show the current genres beside what was found. Returns the ticked ones, or None."""
    d = load_dialog(ui_path, parent)
    d.genreCompareCurrent.setText(current or "(none)")
    for position, name in enumerate(found):
        item = QListWidgetItem(name)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        ticked = position < GENRES_TICKED_AT_FIRST
        item.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
        d.genreCompareFound.addItem(item)
    d.genreCompareApprove.clicked.connect(d.accept)
    d.genreCompareDiscard.clicked.connect(d.reject)
    approved = d.exec() == QDialog.DialogCode.Accepted
    chosen = [
        d.genreCompareFound.item(row).text()
        for row in range(d.genreCompareFound.count())
        if d.genreCompareFound.item(row).checkState() == Qt.CheckState.Checked
    ]
    d.deleteLater()
    return chosen if approved else None
