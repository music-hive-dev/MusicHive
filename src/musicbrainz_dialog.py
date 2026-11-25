"""The MusicBrainz window (musicbrainz_lookup.ui): one album after another, it
finds the album on MusicBrainz, matches the songs to its tracks, and shows each
tag that would change, next to what the song has now. Only the ticked changes
are written, and only when Save is pressed.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, QThreadPool, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHeaderView, QTableWidgetItem, QWidget

from src.lastfm_dialog import load_dialog
from src.musicbrainz import (
    RELEASE_PAGE_URL,
    Candidate,
    Change,
    Release,
    fetch_release,
    propose,
    search_releases,
)
from src.tag_writer import TagJob
from src.track_metadata import TagChanges

ORIGINAL_YEAR_KEY = "musicBrainz/originalYear"
YEAR_ONLY_KEY = "musicBrainz/yearOnly"
TAG_LABELS = {
    "album": "Album",
    "albumartist": "Album artist",
    "date": "Year",
    "discnumber": "Disc",
    "disctotal": "Discs",
    "tracknumber": "Track",
    "tracktotal": "Tracks",
    "title": "Title",
    "artist": "Artist",
}
SONG_COLUMN, TAG_COLUMN, NOW_COLUMN, NEW_COLUMN = range(4)
SEARCH, RELEASE = "search", "release"


@dataclass
class AlbumToLookUp:
    title: str
    artist: str
    year: str  # what most songs say, to help pick the right release; "" if unknown
    songs: list  # check_tags.Song


def _songs(count: int) -> str:
    return f"{count} song{'' if count == 1 else 's'}"


class _Signals(QObject):
    done = Signal(int, object, str)  # request number, result (None if cancelled), error


class _Task(QRunnable):
    def __init__(self, request: int, work: Callable[[], object], signals: _Signals) -> None:
        super().__init__()
        self._request = request
        self._work = work
        self._signals = signals

    def run(self) -> None:
        try:
            self._signals.done.emit(self._request, self._work(), "")
        except Exception as error:  # never let a thread die silently
            self._signals.done.emit(self._request, None, str(error))


class MusicBrainzLookup(QObject):
    """Connects the widgets of musicbrainz_lookup.ui. run() shows it."""

    def __init__(
        self,
        ui_path: Path,
        parent: QWidget,
        albums: list[AlbumToLookUp],
        save_tags: Callable[[list[TagJob]], None],
    ) -> None:
        super().__init__(parent)
        self.dialog = d = load_dialog(ui_path, parent)
        self._albums = albums
        self._save_tags = save_tags
        self._settings = QSettings("MusicHive", "MusicHive")
        self._position = -1
        self._request = 0
        self._waiting_for = ""
        self._closed = False
        self._cancel = threading.Event()
        self._candidates: list[Candidate] = []
        self._releases: dict[str, Release] = {}
        self._changes: list[Change] = []
        self.saved_albums = self.saved_songs = 0

        self._signals = _Signals(self)
        self._signals.done.connect(self._on_done)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)  # one request at a time, as MusicBrainz asks

        table = d.mbChanges
        header = table.horizontalHeader()
        header.setSectionResizeMode(SONG_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(TAG_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(NOW_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(NEW_COLUMN, QHeaderView.ResizeMode.Stretch)

        d.mbOriginalYear.setChecked(
            self._settings.value(ORIGINAL_YEAR_KEY, d.mbOriginalYear.isChecked(), type=bool)
        )
        d.mbOriginalYear.toggled.connect(
            lambda on: self._on_option_toggled(ORIGINAL_YEAR_KEY, on)
        )
        d.mbYearOnly.setChecked(
            self._settings.value(YEAR_ONLY_KEY, d.mbYearOnly.isChecked(), type=bool)
        )
        d.mbYearOnly.toggled.connect(lambda on: self._on_option_toggled(YEAR_ONLY_KEY, on))
        d.mbRelease.currentIndexChanged.connect(self._on_release_chosen)
        d.mbOpenButton.clicked.connect(lambda _checked=False: self._open_release())
        d.mbTickAllButton.clicked.connect(lambda _checked=False: self._tick_all(True))
        d.mbUntickAllButton.clicked.connect(lambda _checked=False: self._tick_all(False))
        d.mbSkipButton.clicked.connect(lambda _checked=False: self._next())
        d.mbSaveButton.clicked.connect(lambda _checked=False: self._save())
        d.mbStopButton.clicked.connect(d.reject)
        d.mbSkipButton.setVisible(len(albums) > 1)
        d.mbStopButton.setText("Stop" if len(albums) > 1 else "Cancel")

    def run(self) -> None:
        """Show the window until every album is done or it is closed."""
        self._next()
        if self._position < len(self._albums):
            self.dialog.exec()
        self._closed = True
        self._cancel.set()  # a request still waiting for its turn gives up
        self.dialog.deleteLater()

    # ----- going through the albums -----

    def _album(self) -> AlbumToLookUp:
        return self._albums[self._position]

    def _next(self) -> None:
        d = self.dialog
        self._position += 1
        if self._position >= len(self._albums):
            d.accept()
            return
        album = self._album()
        name = f"“{album.title}”" + (f" – {album.artist}" if album.artist else "")
        if len(self._albums) > 1:
            d.mbAlbumLabel.setText(
                f"Album {self._position + 1} of {len(self._albums)}: {name} · {_songs(len(album.songs))}"
            )
        else:
            d.mbAlbumLabel.setText(f"{name} · {_songs(len(album.songs))}")
        more = self._position < len(self._albums) - 1
        d.mbSaveButton.setText("Save and Next" if more else "Save")
        d.mbSkipButton.setText("Skip" if more else "Skip and Finish")
        self._candidates = []
        d.mbRelease.blockSignals(True)
        d.mbRelease.clear()
        d.mbRelease.blockSignals(False)
        self._show_nothing("Searching MusicBrainz…")
        self._start(
            lambda: search_releases(
                album.artist, album.title, len(album.songs), album.year, self._cancel
            ),
            SEARCH,
        )

    def _start(self, work: Callable[[], object], kind: str) -> None:
        self._request += 1
        self._waiting_for = kind
        self._pool.start(_Task(self._request, work, self._signals))

    def _show_nothing(self, status: str) -> None:
        d = self.dialog
        d.mbStatus.setText(status)
        d.mbChanges.setRowCount(0)
        self._changes = []
        d.mbSaveButton.setEnabled(False)
        d.mbTickAllButton.setEnabled(False)
        d.mbUntickAllButton.setEnabled(False)
        d.mbOpenButton.setEnabled(bool(self._candidates))
        d.mbRelease.setEnabled(bool(self._candidates))

    def _on_done(self, request: int, result: object, error: str) -> None:
        if self._closed or request != self._request:
            return  # for an album or release no longer shown
        if error:
            self._show_nothing(f"MusicBrainz could not be asked: {error}")
            return
        if result is None:
            return  # cancelled
        if self._waiting_for == SEARCH:
            self._candidates = result
            if not self._candidates:
                self._show_nothing(
                    "MusicBrainz knows no release by this name. Check the album and artist "
                    "names (Edit Tags), then look it up again."
                )
                return
            d = self.dialog
            d.mbRelease.blockSignals(True)
            for candidate in self._candidates:
                d.mbRelease.addItem(candidate.description)
            d.mbRelease.setCurrentIndex(0)
            d.mbRelease.blockSignals(False)
            self._fetch(0)
        else:
            release: Release = result
            self._releases[release.mbid] = release
            self._show_changes()

    def _on_release_chosen(self, index: int) -> None:
        if 0 <= index < len(self._candidates):
            self._fetch(index)

    def _fetch(self, index: int) -> None:
        candidate = self._candidates[index]
        if candidate.mbid in self._releases:
            self._show_changes()
            return
        self._show_nothing("Fetching the tracks of this release…")
        self._start(lambda: fetch_release(candidate.mbid, self._cancel), RELEASE)

    def _current_release(self) -> Release | None:
        index = self.dialog.mbRelease.currentIndex()
        if not 0 <= index < len(self._candidates):
            return None
        return self._releases.get(self._candidates[index].mbid)

    # ----- the changes -----

    def _on_option_toggled(self, key: str, on: bool) -> None:
        self._settings.setValue(key, on)
        self._show_changes()

    def _show_changes(self) -> None:
        d = self.dialog
        release = self._current_release()
        if release is None:
            return
        album = self._album()
        self._changes, unmatched = propose(
            album.songs, release, d.mbOriginalYear.isChecked(), d.mbYearOnly.isChecked()
        )
        table = d.mbChanges
        table.setRowCount(len(self._changes))
        for row, change in enumerate(self._changes):
            song = QTableWidgetItem(change.path.name)
            song.setToolTip(str(change.path))
            song.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            song.setCheckState(Qt.CheckState.Checked if change.ticked else Qt.CheckState.Unchecked)
            now = QTableWidgetItem(change.now or "(none)")
            now.setToolTip(change.now)
            new = QTableWidgetItem(change.new)
            new.setToolTip(change.new)
            table.setItem(row, SONG_COLUMN, song)
            table.setItem(row, TAG_COLUMN, QTableWidgetItem(TAG_LABELS.get(change.tag, change.tag)))
            table.setItem(row, NOW_COLUMN, now)
            table.setItem(row, NEW_COLUMN, new)

        notes = []
        tracks = len(release.tracks)
        if tracks != len(album.songs):
            notes.append(f"This release has {tracks} tracks; the album has {_songs(len(album.songs))}.")
        if unmatched:
            notes.append(
                f"{_songs(unmatched)} fit no track of this release, so "
                f"{'it only gets' if unmatched == 1 else 'they only get'} the album's tags."
            )
        if self._changes:
            changed = len({change.path for change in self._changes})
            notes.insert(0, f"{len(self._changes)} changes in {_songs(changed)}. Untick any you do not want.")
        else:
            notes.insert(0, "The songs already match this release.")
        d.mbStatus.setText(" ".join(notes))
        d.mbSaveButton.setEnabled(bool(self._changes))
        d.mbTickAllButton.setEnabled(bool(self._changes))
        d.mbUntickAllButton.setEnabled(bool(self._changes))
        d.mbOpenButton.setEnabled(True)
        d.mbRelease.setEnabled(True)

    def _tick_all(self, ticked: bool) -> None:
        table = self.dialog.mbChanges
        state = Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked
        for row in range(table.rowCount()):
            table.item(row, SONG_COLUMN).setCheckState(state)

    def _open_release(self) -> None:
        index = self.dialog.mbRelease.currentIndex()
        if 0 <= index < len(self._candidates):
            QDesktopServices.openUrl(QUrl(RELEASE_PAGE_URL + self._candidates[index].mbid))

    def _save(self) -> None:
        table = self.dialog.mbChanges
        by_song: dict[Path, dict[str, str]] = {}
        for row, change in enumerate(self._changes):
            if table.item(row, SONG_COLUMN).checkState() == Qt.CheckState.Checked:
                by_song.setdefault(change.path, {})[change.tag] = change.new
        if by_song:
            self._save_tags([TagJob(path, TagChanges(tags)) for path, tags in by_song.items()])
            self.saved_albums += 1
            self.saved_songs += len(by_song)
        self._next()
