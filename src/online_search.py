"""Searching the internet for album images, genres and lyrics, and comparing what is found.

Nothing found here is kept unless the user approves it in a comparison window.
"""

import hashlib
import json
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QDialog, QListWidgetItem, QWidget

from src.artist_info import LASTFM_RETRY_CODES, LookupFailed, RateLimited, download
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


LYRICS_URL = "https://lrclib.net/api"  # LRCLIB: free, no account or key
LYRICS_SEARCH_RESULTS = 10


@dataclass
class FoundLyrics:
    timed: str  # LRC, with [mm:ss.xx] times; "" if there are none
    plain: str  # "" if there are none
    instrumental: bool
    duration: float  # seconds, as LRCLIB knows the song
    description: str  # for example "Song – Artist (Album)"


def _lyrics_from(entry: dict) -> FoundLyrics:
    parts = [entry.get("trackName") or "", entry.get("artistName") or ""]
    description = " – ".join(p for p in parts if p)
    if entry.get("albumName"):
        description += f" ({entry['albumName']})"
    return FoundLyrics(
        timed=(entry.get("syncedLyrics") or "").strip(),
        plain=(entry.get("plainLyrics") or "").strip(),
        instrumental=bool(entry.get("instrumental")),
        duration=float(entry.get("duration") or 0),
        description=description,
    )


# The entries of tagEditLyricsPreference and autoLyricsPreference, in order.
PREFER_TIMED, ONLY_TIMED, PREFER_PLAIN, ONLY_PLAIN = range(4)
LYRICS_PREFERENCE_KEY = "lyrics/searchPreference"
_LRC_TIME = re.compile(r"^\s*(\[\d+:\d+(?:[.:]\d+)?\])+\s*")
_LRC_INFO = re.compile(r"^\s*\[[a-z]+:.*\]\s*$", re.IGNORECASE)  # [ar:Artist] and the like


def without_times(lrc: str) -> str:
    """Timed (LRC) lyrics as plain text: the times and the [ar:…] lines left out."""
    lines = [_LRC_TIME.sub("", line) for line in lrc.splitlines() if not _LRC_INFO.match(line)]
    return "\n".join(lines).strip()


def pick_lyrics(found: list[FoundLyrics], preference: int) -> str | None:
    """The lyrics to use, best first, as the preference says; None if none fit."""
    wants_timed = preference in (PREFER_TIMED, ONLY_TIMED)
    for lyrics in found:
        text = lyrics.timed if wants_timed else lyrics.plain
        if text:
            return text
    if preference == ONLY_TIMED:
        return None
    for lyrics in found:  # the other kind
        if preference == PREFER_TIMED and lyrics.plain:
            return lyrics.plain
        if preference in (PREFER_PLAIN, ONLY_PLAIN) and lyrics.timed:
            return lyrics.timed if preference == PREFER_PLAIN else without_times(lyrics.timed)
    return None


def find_lyrics(artist: str, title: str, album: str, duration_s: float) -> list[FoundLyrics]:
    """Lyrics for a song from LRCLIB, best first: the exact match (artist, title,
    album and length) if there is one, then search results closest in length."""
    found: list[FoundLyrics] = []
    if duration_s > 0:
        query = urllib.parse.urlencode({
            "artist_name": artist, "track_name": title, "album_name": album,
            "duration": round(duration_s),
        })
        data = download(f"{LYRICS_URL}/get?{query}")
        if data:
            found.append(_lyrics_from(json.loads(data)))
            if found[0].timed:
                return found  # the exact song, with timed lyrics: nothing better to find
    query = urllib.parse.urlencode({"artist_name": artist, "track_name": title})
    data = download(f"{LYRICS_URL}/search?{query}")
    results = [_lyrics_from(entry) for entry in (json.loads(data) if data else [])]
    if duration_s > 0:
        results.sort(key=lambda lyrics: abs(lyrics.duration - duration_s) if lyrics.duration else 9999)
    for lyrics in results[:LYRICS_SEARCH_RESULTS]:
        if all((lyrics.timed, lyrics.plain) != (other.timed, other.plain) for other in found):
            found.append(lyrics)
    return found


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
    lyrics_found = Signal(int, object, str)  # request, list of FoundLyrics, error
    auto_tag_progress = Signal(int, int)  # songs done, total
    auto_tag_finished = Signal(object, int, str, bool)  # results, not found, error, stopped
    auto_lyrics_progress = Signal(int, int)  # songs done, total
    # [(path, lyrics, where)], not found, skipped (had lyrics), upgraded, error, stopped
    auto_lyrics_finished = Signal(object, int, int, int, str, bool)
    auto_lyrics_waiting = Signal(int)  # LRCLIB asked to slow down: seconds to wait
    auto_lyrics_checking = Signal(int, int)  # songs checked for lyrics they have, total


@dataclass
class AutoLyricsSong:
    path: Path
    artist: str
    title: str
    album: str
    duration_s: float


AUTO_LYRICS_PAUSE_SECONDS = 1.0  # between songs, to be kind to LRCLIB
# When LRCLIB answers "too many requests" without saying how long to wait.
RATE_LIMIT_WAITS = (15, 30, 60, 120)  # seconds; after these, the lookup gives up


LRC, EMBEDDED = "lrc", "embedded"  # where a song's lyrics are


def is_timed(text: str) -> bool:
    return any(_LRC_TIME.match(line) for line in text.splitlines())


# Set to "false" on songs LRCLIB calls instrumental, and to "true" when lyrics are found.
# (Not the Keywords: "Instrumental" there marks instrumental versions of songs.)
HAS_LYRICS_TAG = "haslyrics"


def lyrics_state(path: Path) -> tuple[tuple[str, str] | None, str]:
    """What a song has already: its lyrics as ("timed" or "plain", LRC or EMBEDDED)
    or None, and its haslyrics tag. A .lrc file next to the song counts first, as
    Now Playing shows that one."""
    from src.track_metadata import read_editable_file

    tags = read_editable_file(path).tags
    has_lyrics = tags.get(HAS_LYRICS_TAG, "")
    lrc = path.with_suffix(".lrc")
    if lrc.is_file():
        try:
            text = lrc.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            text = ""
        if text.strip():
            return (("timed" if is_timed(text) else "plain"), LRC), has_lyrics
    text = tags.get("lyrics", "")
    if text.strip():
        return (("timed" if is_timed(text) else "plain"), EMBEDDED), has_lyrics
    return None, has_lyrics


def known_instrumental(has_lyrics: str) -> bool:
    """Whether a haslyrics tag says the song has no lyrics."""
    return has_lyrics.strip().casefold() == "false"


def is_instrumental(found: list[FoundLyrics]) -> bool:
    """Whether LRCLIB calls the song an instrumental (its best match does)."""
    return bool(found) and found[0].instrumental


class _AutoLyricsTask(QRunnable):
    """Looks up each song's lyrics. Nothing is written here."""

    def __init__(self, songs: list[AutoLyricsSong], preference: int, skip_existing: bool,
                 upgrade_plain: bool, cancel: threading.Event, signals: "_Signals") -> None:
        super().__init__()
        self._songs = songs
        self._preference = preference
        self._skip_existing = skip_existing
        self._upgrade_plain = upgrade_plain  # songs with plain lyrics get timed ones
        self._cancel = cancel
        self._signals = signals

    def run(self) -> None:
        # (path, lyrics or None, where to save them: None for the chosen place, or
        # LRC / EMBEDDED): an instrumental has no lyrics, and gets haslyrics=false.
        results: list[tuple[Path, str | None, str | None]] = []
        not_found = skipped = upgraded = 0
        error = ""
        # First which songs need looking up, so the count is of those alone:
        # (song, what to look for, where to save it).
        to_look_up: list[tuple[AutoLyricsSong, int, str | None]] = []
        for checked, song in enumerate(self._songs, start=1):
            if self._cancel.is_set():
                break
            preference, where = self._preference, None
            try:
                existing, has_lyrics = lyrics_state(song.path)
            except Exception:  # an unreadable file: look it up anyway
                existing, has_lyrics = None, ""
            if self._skip_existing:
                if known_instrumental(has_lyrics):
                    skipped += 1
                    self._signals.auto_lyrics_checking.emit(checked, len(self._songs))
                    continue
                if existing is not None:
                    kind, place = existing
                    if kind == "timed" or not self._upgrade_plain:
                        skipped += 1
                        self._signals.auto_lyrics_checking.emit(checked, len(self._songs))
                        continue
                    # Plain lyrics: only timed ones would be better, in the same place.
                    preference, where = ONLY_TIMED, place
            to_look_up.append((song, preference, where))
            self._signals.auto_lyrics_checking.emit(checked, len(self._songs))

        for done, (song, preference, where) in enumerate(to_look_up, start=1):
            if self._cancel.is_set():
                break
            try:
                text = None
                instrumental = False
                if song.artist and song.title:
                    found = self._find(song)
                    if found is None:  # stopped while waiting
                        break
                    text = pick_lyrics(found, preference)
                    instrumental = not text and is_instrumental(found)
            except LookupFailed as problem:  # LRCLIB cannot be reached: stop here
                error = str(problem)
                break
            except Exception as problem:  # never let a thread die silently
                error = str(problem)
                break
            if text:
                results.append((song.path, text, where))
                upgraded += where is not None
            elif instrumental:
                results.append((song.path, None, None))
            else:
                not_found += 1
            self._signals.auto_lyrics_progress.emit(done, len(to_look_up))
            time.sleep(AUTO_LYRICS_PAUSE_SECONDS)
        self._signals.auto_lyrics_finished.emit(
            results, not_found, skipped, upgraded, error, self._cancel.is_set()
        )

    def _find(self, song: AutoLyricsSong) -> list[FoundLyrics] | None:
        """The song's lyrics; when LRCLIB asks to slow down, wait and ask again.
        None if stopped while waiting. Raises RateLimited when it keeps refusing."""
        for attempt in range(len(RATE_LIMIT_WAITS) + 1):
            try:
                return find_lyrics(song.artist, song.title, song.album, song.duration_s)
            except RateLimited as problem:
                if attempt == len(RATE_LIMIT_WAITS):
                    raise LookupFailed("LRCLIB keeps answering “too many requests”; try again later")
                wait = problem.wait or RATE_LIMIT_WAITS[attempt]
                self._signals.auto_lyrics_waiting.emit(wait)
                if self._cancel.wait(wait):  # returns at once when stopped
                    return None
        return None


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
    lyrics_found = Signal(int, object, str)
    auto_tag_progress = Signal(int, int)  # songs done, total
    auto_tag_finished = Signal(object, int, str, bool)  # results, not found, error, stopped
    auto_lyrics_progress = Signal(int, int)
    auto_lyrics_finished = Signal(object, int, int, int, str, bool)
    auto_lyrics_waiting = Signal(int)
    auto_lyrics_checking = Signal(int, int)

    def __init__(self, api_key: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._api_key = api_key
        self._request = 0
        self._signals = _Signals(self)
        self._signals.covers_found.connect(self.covers_found)
        self._signals.genres_found.connect(self.genres_found)
        self._signals.lyrics_found.connect(self.lyrics_found)
        self._signals.auto_tag_progress.connect(self.auto_tag_progress)
        self._signals.auto_tag_finished.connect(self.auto_tag_finished)
        self._signals.auto_lyrics_progress.connect(self.auto_lyrics_progress)
        self._signals.auto_lyrics_finished.connect(self.auto_lyrics_finished)
        self._signals.auto_lyrics_waiting.connect(self.auto_lyrics_waiting)
        self._signals.auto_lyrics_checking.connect(self.auto_lyrics_checking)
        self._cancel_auto_tag = threading.Event()
        self._cancel_auto_lyrics = threading.Event()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)

    def find_covers(self, artist: str, album: str) -> int:
        return self._start(lambda: find_covers(artist, album), self._signals.covers_found)

    def find_genres(self, artist: str, title: str, album: str) -> int:
        search = lambda: find_genres(self._api_key, artist, title, album)
        return self._start(search, self._signals.genres_found)

    def find_lyrics(self, artist: str, title: str, album: str, duration_s: float) -> int:
        search = lambda: find_lyrics(artist, title, album, duration_s)
        return self._start(search, self._signals.lyrics_found)

    def auto_tag(self, songs: list[AutoTagSong], limit: int | None) -> None:
        """Look up the genres of many songs, reporting through the auto_tag signals."""
        self._cancel_auto_tag.clear()
        self._pool.start(
            _AutoTagTask(self._api_key, songs, limit, self._cancel_auto_tag, self._signals)
        )

    def stop_auto_tag(self) -> None:
        self._cancel_auto_tag.set()

    def auto_lyrics(self, songs: list[AutoLyricsSong], preference: int, skip_existing: bool,
                    upgrade_plain: bool) -> None:
        """Look up the lyrics of many songs, reporting through the auto_lyrics signals."""
        self._cancel_auto_lyrics.clear()
        self._pool.start(_AutoLyricsTask(
            songs, preference, skip_existing, upgrade_plain, self._cancel_auto_lyrics,
            self._signals,
        ))

    def stop_auto_lyrics(self) -> None:
        self._cancel_auto_lyrics.set()

    def _start(self, search: Callable[[], list], signal) -> int:
        self._request += 1
        self._pool.start(_SearchTask(self._request, search, signal))
        return self._request

    def shutdown(self) -> None:
        self._cancel_auto_tag.set()
        self._cancel_auto_lyrics.set()
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


LYRICS_SAVE_KEY = "lyrics/autoSaveTo"
LYRICS_SKIP_KEY = "lyrics/autoSkipExisting"
LYRICS_UPGRADE_KEY = "lyrics/autoUpgradePlain"
SAVE_TO_LRC, SAVE_EMBEDDED = 0, 1  # the entries of autoLyricsSaveTo, in order


def ask_auto_lyrics(
    ui_path: Path, parent: QWidget, song_count: int, settings
) -> tuple[int, int, bool, bool] | None:
    """Ask what to look for and where to save it: (preference, save to, skip songs
    with lyrics, upgrade plain lyrics to timed), or None if cancelled. The answers
    are remembered for next time."""
    d = load_dialog(ui_path, parent)
    songs = f"{song_count} selected song{'' if song_count == 1 else 's'}"
    d.autoLyricsCountLabel.setText(f"This looks up the lyrics of {songs} on LRCLIB.")
    d.autoLyricsPreference.setCurrentIndex(settings.value(LYRICS_PREFERENCE_KEY, PREFER_TIMED, type=int))
    d.autoLyricsSaveTo.setCurrentIndex(settings.value(LYRICS_SAVE_KEY, SAVE_TO_LRC, type=int))
    d.autoLyricsSkipExisting.setChecked(settings.value(LYRICS_SKIP_KEY, True, type=bool))
    d.autoLyricsUpgrade.setChecked(settings.value(LYRICS_UPGRADE_KEY, True, type=bool))
    # Upgrading only means something while songs with lyrics are otherwise left alone.
    d.autoLyricsUpgrade.setEnabled(d.autoLyricsSkipExisting.isChecked())
    d.autoLyricsSkipExisting.toggled.connect(d.autoLyricsUpgrade.setEnabled)
    d.autoLyricsButtonBox.accepted.connect(d.accept)
    d.autoLyricsButtonBox.rejected.connect(d.reject)
    accepted = d.exec() == QDialog.DialogCode.Accepted
    answer = (
        d.autoLyricsPreference.currentIndex(),
        d.autoLyricsSaveTo.currentIndex(),
        d.autoLyricsSkipExisting.isChecked(),
        d.autoLyricsUpgrade.isChecked(),
    )
    d.deleteLater()
    if not accepted:
        return None
    settings.setValue(LYRICS_PREFERENCE_KEY, answer[0])
    settings.setValue(LYRICS_SAVE_KEY, answer[1])
    settings.setValue(LYRICS_SKIP_KEY, answer[2])
    settings.setValue(LYRICS_UPGRADE_KEY, answer[3])
    return answer


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
