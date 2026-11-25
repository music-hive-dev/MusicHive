"""Tools > Check Covers: find albums whose cover needs replacing.

Every album's cover is read from the files themselves (the library only keeps
small thumbnails), and the album is listed when its cover is missing, cannot
be read, is not square, is too small, is a very big file, or when its songs do
not share one cover. The different covers of the chosen album are shown below
the list: one can be stretched until it is square, or given to the whole album.
An album's songs also open in the tag editor, which can search the internet for
a better cover. Once tags are saved, the album is checked again, and leaves the
list when it is fixed.

Songs whose cover is fine as it is can be marked to skip: a tag in the file
(SKIP_TAG), so the mark stays with the song, and the check leaves them out.
"""

import csv
import hashlib
import itertools
import os
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QObject,
    QRunnable,
    QSettings,
    QThreadPool,
    QUrl,
    Qt,
    Signal,
)
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHeaderView,
    QListWidgetItem,
    QMessageBox,
    QTableWidgetItem,
    QWidget,
)

from src import library_db
from src.i18n import tr
from src.lastfm_dialog import load_dialog
from src.library_db import LibraryInfo
from src.number_field import NumberField
from src.tag_writer import TagJob, TagWriter
from src.track_metadata import (
    TagChanges,
    TrackInfo,
    find_folder_cover,
    forget_folder_covers,
    read_cover,
)

SKIP_TAG = "musichiveskipcover"  # "true": leave this song's cover alone
SETTINGS_PREFIX = "checkCovers/"
SQUARE_TOLERANCE = 0.01  # sides that differ by up to 1% still count as square
THUMBNAIL_SIDE = 160  # drawn at 80 px at most, so it stays sharp on HiDPI screens
JPEG_QUALITY = 95
MAX_WORKERS = 4
ALBUM_COLUMN, ARTIST_COLUMN, SONGS_COLUMN, COVER_COLUMN, PROBLEMS_COLUMN = range(5)
ALBUM_ID_ROLE = Qt.ItemDataRole.UserRole
LISTED_NAMES = 12  # song names in a tooltip


def is_skipped(value: str) -> bool:
    """Whether the text of a SKIP_TAG says yes."""
    return value.strip().casefold() in ("true", "1", "yes")


@dataclass
class Album:
    """The songs that should share one cover."""

    title: str
    artist: str
    paths: list[Path]
    # The library's cover key of each song, so that only one song per cover is
    # read. None means "read every song", for a check after the tags changed.
    keys: list[str | None] | None
    skipped: list[bool]  # of each song: marked to be left out

    @property
    def folder(self) -> Path:
        try:
            return Path(os.path.commonpath([str(path.parent) for path in self.paths]))
        except ValueError:  # songs on different drives
            return self.paths[0].parent


@dataclass(frozen=True)
class Limits:
    """Which problems to look for."""

    missing: bool = True
    mixed: bool = True
    square: bool = True
    low_res: bool = True
    min_side: int = 500
    large: bool = False
    max_bytes: int = 2_000_000
    include_skipped: bool = False


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB" if size >= 100_000 else f"{size // 1000} KB"


def _songs(count: int) -> str:
    return tr("%n song(s)", "", count)


def _names(paths: list[Path]) -> str:
    """File names for a tooltip, the first few of them."""
    names = [path.name for path in paths[:LISTED_NAMES]]
    if len(paths) > LISTED_NAMES:
        names.append(tr("… and {count} more").format(count=len(paths) - LISTED_NAMES))
    return "\n".join(names)


@dataclass
class CoverInfo:
    """One cover, and the songs of the album that have it."""

    paths: list[Path]
    source: str  # "embedded", or the name of the image file in the folder
    size: int  # of the image file, in bytes
    width: int = 0  # 0 if the image cannot be read
    height: int = 0
    thumbnail: QImage | None = None

    @property
    def readable(self) -> bool:
        return self.width > 0 and self.height > 0

    @property
    def square(self) -> bool:
        longer = max(self.width, self.height)
        return abs(self.width - self.height) <= max(2, longer * SQUARE_TOLERANCE)

    @property
    def dimensions(self) -> str:
        return f"{self.width} × {self.height}" if self.readable else tr("Cannot be read")

    @property
    def text(self) -> str:
        return f"{self.dimensions} · {_megabytes(self.size)} · {self.source}"


@dataclass
class CoverReport:
    problems: list[str] = field(default_factory=list)
    covers: list[CoverInfo] = field(default_factory=list)  # most songs first
    without: list[Path] = field(default_factory=list)  # checked songs that have no cover
    skipped: list[bool] | None = None  # of each song, when the files were read

    @property
    def cover_text(self) -> str:
        return self.covers[0].text if self.covers else tr("None")


def group_albums(songs: list[tuple[Path, TrackInfo | None]], skipped: set[Path]) -> list[Album]:
    """Songs grouped by album. Songs of one album name are kept together if they
    share an album artist, or (when they have none) a folder, so compilations stay
    one album. A song without an album name is an album of its own."""
    groups: dict[tuple, list[tuple[Path, TrackInfo]]] = {}
    for path, info in songs:
        if info is None:
            info = TrackInfo(title=path.stem)
        album = (info.album or "").strip()
        album_artist = (info.album_artist or "").strip()
        if not album:
            key = ("song", str(path))
        elif album_artist:
            key = ("album", album.casefold(), album_artist.casefold())
        else:
            key = ("folder", album.casefold(), str(path.parent))
        groups.setdefault(key, []).append((path, info))

    albums = []
    for members in groups.values():
        members.sort(key=lambda m: (m[1].disc_number or 0, m[1].track_number or 0, m[0].name))
        first = members[0][1]
        artists = {(info.artist or "").strip() for _path, info in members} - {""}
        if (first.album_artist or "").strip():
            artist = first.album_artist.strip()
        elif len(artists) == 1:
            artist = artists.pop()
        else:
            artist = tr("Various artists") if artists else ""
        title = (first.album or "").strip() or tr("{title} (no album)").format(
            title=first.title or members[0][0].stem,
        )
        albums.append(
            Album(
                title,
                artist,
                [path for path, _info in members],
                [info.cover_key for _path, info in members],
                [path in skipped for path, _info in members],
            )
        )
    return albums


def _describe(paths: list[Path], data: bytes, source: str) -> CoverInfo:
    """Measure a cover and make its thumbnail, without decoding it at full size."""
    cover = CoverInfo(paths, source, len(data))
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    if not size.isValid():
        return cover
    reader.setScaledSize(
        size.scaled(THUMBNAIL_SIDE, THUMBNAIL_SIDE, Qt.AspectRatioMode.KeepAspectRatio)
    )
    image = reader.read()
    if not image.isNull():
        cover.width, cover.height = size.width(), size.height()
        cover.thumbnail = image
    return cover


def check_album(album: Album, limits: Limits) -> CoverReport:
    """Read the album's covers and say what is wrong with them."""
    # cover key -> [songs that have it, image data, where it comes from]; None: no cover
    found: dict[str | None, list] = {}
    report = CoverReport()
    if album.keys is None:
        report.skipped = []
        for path in album.paths:
            data, source, mark = read_cover(path, SKIP_TAG)
            report.skipped.append(is_skipped(mark))
            if report.skipped[-1] and not limits.include_skipped:
                continue
            key = hashlib.sha1(data).hexdigest() if data else None
            found.setdefault(key, [[], data, source])[0].append(path)
        skipped = report.skipped
    else:
        skipped = album.skipped
        for path, key, skip in zip(album.paths, album.keys, album.skipped):
            if skip and not limits.include_skipped:
                continue
            entry = found.get(key)
            if entry is None:
                data, source, _mark = read_cover(path)
                found[key] = entry = [[], data, source]
            entry[0].append(path)

    if not found:
        return report  # every song is marked to skip
    for paths, data, source in found.values():
        if data:
            report.covers.append(_describe(paths, data, source))
        else:
            report.without.extend(paths)
    report.covers.sort(key=lambda cover: -len(cover.paths))

    checked = sum(len(entry[0]) for entry in found.values())
    problems = report.problems
    if not report.covers:
        if limits.missing:
            problems.append(tr("No cover"))
    else:
        if report.without and limits.missing:
            problems.append(tr("{without} of {checked} songs have no cover").format(
                without=len(report.without),
                checked=checked,
            ))
        if len(report.covers) > 1 and limits.mixed:
            problems.append(tr("{count} different covers").format(
                count=len(report.covers),
            ))
        main = report.covers[0]  # the cover most songs have
        if not main.readable:
            problems.append(tr("Cover cannot be read"))
        else:
            if limits.square and not main.square:
                problems.append(tr("Not square ({dimensions})").format(dimensions=main.dimensions))
            if limits.low_res and min(main.width, main.height) < limits.min_side:
                problems.append(tr("Low resolution ({dimensions})").format(
                    dimensions=main.dimensions,
                ))
            if limits.large and main.size > limits.max_bytes:
                problems.append(tr("Large image ({megabytes})").format(
                    megabytes=_megabytes(main.size),
                ))
    if problems and any(skipped):
        problems.insert(0, tr("{songs} marked to skip").format(songs=_songs(sum(skipped))))
    return report


def _encode(image: QImage, kind: str) -> bytes | None:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, kind, JPEG_QUALITY if kind == "JPEG" else -1):
        return None
    return bytes(buffer.data())


def stretch_square(data: bytes) -> QImage | None:
    """The image stretched until both sides are as long as its longer one."""
    image = QImage.fromData(data)
    if image.isNull():
        return None
    side = max(image.width(), image.height())
    return image.scaled(
        side,
        side,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class _Signals(QObject):
    checked = Signal(int, object, bool)  # album id, CoverReport, part of the full check
    worker_done = Signal(object)  # the _Worker


class _Worker(QRunnable):
    """Checks albums from the shared queue until there are none left."""

    def __init__(self, queue: deque, cancel: threading.Event, signals: _Signals) -> None:
        super().__init__()
        self.setAutoDelete(False)  # Python keeps ownership of this object
        self._queue = queue
        self._cancel = cancel
        self._signals = signals

    def run(self) -> None:
        try:
            while not self._cancel.is_set():
                try:
                    album_id, album, limits, counted = self._queue.popleft()
                except IndexError:
                    break
                try:
                    report = check_album(album, limits)
                except Exception as error:  # one bad album must not stop the others
                    report = CoverReport([tr("Cannot be checked: {error}").format(error=error)])
                if not self._cancel.is_set():
                    self._signals.checked.emit(album_id, report, counted)
        finally:
            self._signals.worker_done.emit(self)


class CheckCoversTool(QObject):
    """Connects the widgets of check_covers.ui and runs the checking."""

    def __init__(
        self,
        ui_path: Path,
        parent: QWidget,
        tag_writer: TagWriter,
        library_songs: Callable[[], tuple[LibraryInfo, list[tuple[Path, TrackInfo]]] | None],
        edit_songs: Callable[[list[Path], bool], None],
        save_tags: Callable[[list[TagJob], str], None],
        show_status: Callable[[str], None],
    ) -> None:
        """`library_songs` gives the open library and its songs (None if there is
        none); `edit_songs(paths, find_cover)` opens the tag editor for songs, and
        `save_tags` writes tag changes to songs of the open library."""
        super().__init__(parent)
        self.dialog = d = load_dialog(ui_path, parent)
        self._library_songs = library_songs
        self._edit_songs = edit_songs
        self._save_tags = save_tags
        self._show_status = show_status
        self._settings = QSettings("MusicHive", "MusicHive")

        self._library_id: str | None = None
        self._albums: dict[int, Album] = {}
        self._reports: dict[int, CoverReport] = {}  # of the albums in the list
        self._next_id = itertools.count()  # results of an older check are ignored
        self._queue: deque = deque()
        self._cancel = threading.Event()
        self._workers: set[_Worker] = set()
        self._total = self._done = 0  # albums of the full check
        self._signals = _Signals(self)
        self._signals.checked.connect(self._on_checked)
        self._signals.worker_done.connect(self._on_worker_done)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, min(MAX_WORKERS, (os.cpu_count() or 2) - 1)))
        tag_writer.tags_saved.connect(self._on_tags_saved)

        self._min_side = NumberField(d.checkCoversMinSize)
        self._max_size = NumberField(d.checkCoversMaxSize)
        self._options = {
            "missing": d.checkCoversMissing,
            "mixed": d.checkCoversMixed,
            "square": d.checkCoversSquare,
            "lowRes": d.checkCoversLowRes,
            "large": d.checkCoversLarge,
            "showSkipped": d.checkCoversShowSkipped,
        }
        for name, box in self._options.items():
            box.setChecked(
                self._settings.value(SETTINGS_PREFIX + name, box.isChecked(), type=bool)
            )
            box.toggled.connect(
                lambda on, name=name: self._settings.setValue(SETTINGS_PREFIX + name, on)
            )
        self._min_side.setValue(
            self._settings.value(SETTINGS_PREFIX + "minSide", self._min_side.value(), type=int)
        )
        self._max_size.setValue(
            self._settings.value(
                SETTINGS_PREFIX + "maxMegabytes", self._max_size.value(), type=float
            )
        )
        self._min_side.valueChanged.connect(
            lambda value: self._settings.setValue(SETTINGS_PREFIX + "minSide", value)
        )
        self._max_size.valueChanged.connect(
            lambda value: self._settings.setValue(SETTINGS_PREFIX + "maxMegabytes", value)
        )
        d.checkCoversLowRes.toggled.connect(d.checkCoversMinSize.setEnabled)
        d.checkCoversLarge.toggled.connect(d.checkCoversMaxSize.setEnabled)

        table = d.checkCoversList
        header = table.horizontalHeader()
        header.setSectionResizeMode(ALBUM_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(ARTIST_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(SONGS_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COVER_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(PROBLEMS_COLUMN, QHeaderView.ResizeMode.Stretch)
        table.sortByColumn(ARTIST_COLUMN, Qt.SortOrder.AscendingOrder)
        table.cellDoubleClicked.connect(lambda _row, _column: self._edit(False))
        table.itemSelectionChanged.connect(self._show_album_covers)
        d.checkCoversAlbumCovers.itemSelectionChanged.connect(self._update_buttons)

        d.checkCoversStartButton.clicked.connect(lambda _checked=False: self._start())
        d.checkCoversStopButton.clicked.connect(lambda _checked=False: self._stop())
        d.checkCoversEditButton.clicked.connect(lambda _checked=False: self._edit(False))
        d.checkCoversFindButton.clicked.connect(lambda _checked=False: self._edit(True))
        d.checkCoversOpenFolderButton.clicked.connect(lambda _checked=False: self._open_folder())
        d.checkCoversSquareButton.clicked.connect(lambda _checked=False: self._make_square())
        d.checkCoversUseCoverButton.clicked.connect(lambda _checked=False: self._use_for_album())
        d.checkCoversSkipBox.clicked.connect(lambda _checked=False: self._set_skipped())
        d.checkCoversCopyButton.clicked.connect(lambda _checked=False: self._copy_list())
        d.checkCoversSaveButton.clicked.connect(lambda _checked=False: self._save_list())
        d.checkCoversButtonBox.rejected.connect(d.reject)
        self._show_running(False)
        self._show_album_covers()

    @property
    def is_running(self) -> bool:
        return self._total > 0

    def show(self, library: LibraryInfo) -> None:
        if library.id != self._library_id and not self.is_running:
            self._library_id = library.id
            self._albums = {}
            self._reports = {}
            self.dialog.checkCoversList.setRowCount(0)
            self.dialog.checkCoversSummary.setText("")
            self._show_running(False)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def shutdown(self) -> None:
        """The app is closing: stop checking."""
        self._cancel.set()
        self._queue.clear()
        self._pool.waitForDone(5000)

    # ----- running -----

    def _limits(self) -> Limits:
        d = self.dialog
        return Limits(
            missing=d.checkCoversMissing.isChecked(),
            mixed=d.checkCoversMixed.isChecked(),
            square=d.checkCoversSquare.isChecked(),
            low_res=d.checkCoversLowRes.isChecked(),
            min_side=int(self._min_side.value()),
            large=d.checkCoversLarge.isChecked(),
            max_bytes=int(self._max_size.value() * 1_000_000),
            include_skipped=d.checkCoversShowSkipped.isChecked(),
        )

    def _start(self) -> None:
        d = self.dialog
        found = self._library_songs()
        if found is None:
            d.checkCoversSummary.setText(tr("Open a library first."))
            return
        library, songs = found
        try:
            marks = library_db.load_tag_values(library.db_path, [SKIP_TAG])
        except Exception as error:
            d.checkCoversSummary.setText(tr("Cannot read this library: {error}").format(
                error=error,
            ))
            return
        skipped = {
            library.root / rel
            for rel, tags in marks.items()
            if is_skipped(tags.get(SKIP_TAG, ""))
        }
        self._library_id = library.id
        albums = group_albums(songs, skipped)
        if not albums:
            d.checkCoversSummary.setText(tr("This library has no songs."))
            return
        self._cancel = threading.Event()
        self._queue.clear()
        self._albums = {next(self._next_id): album for album in albums}
        self._reports = {}
        d.checkCoversList.setRowCount(0)
        limits = self._limits()
        self._queue.extend(
            (album_id, album, limits, True) for album_id, album in self._albums.items()
        )
        self._total, self._done = len(albums), 0
        d.checkCoversProgress.setRange(0, self._total)
        d.checkCoversProgress.setValue(0)
        d.checkCoversProgress.show()
        d.checkCoversSummary.setText(tr("Checking {total} albums…").format(total=self._total))
        self._show_running(True)
        self._start_workers()

    def _start_workers(self) -> None:
        while self._queue and len(self._workers) < self._pool.maxThreadCount():
            worker = _Worker(self._queue, self._cancel, self._signals)
            self._workers.add(worker)
            self._pool.start(worker)

    def _stop(self) -> None:
        self._cancel.set()
        self._queue.clear()
        self._finish(stopped=True)

    def _recheck(self, album_ids: list[int]) -> None:
        """Check these albums again, reading every song as it is now."""
        limits = self._limits()
        for album_id in album_ids:
            album = self._albums[album_id]
            album.keys = None  # the library's cover keys may be out of date
            self._queue.append((album_id, album, limits, False))
        if self._cancel.is_set():
            self._cancel = threading.Event()
        self._start_workers()

    def _on_worker_done(self, worker: _Worker) -> None:
        self._workers.discard(worker)
        if self._queue and not self._cancel.is_set():
            self._start_workers()  # albums were added just as it ran out

    def _on_checked(self, album_id: int, report: CoverReport, counted: bool) -> None:
        album = self._albums.get(album_id)
        if album is None:
            return  # from a check that was replaced since
        if report.skipped is not None and len(report.skipped) == len(album.paths):
            album.skipped = report.skipped
        self._show_report(album_id, report)
        if counted and self.is_running:
            self._done += 1
            self.dialog.checkCoversProgress.setValue(self._done)
            if self._done >= self._total:
                self._finish(stopped=False)
        elif not counted:
            if not report.problems:
                self._show_status(tr("“{title}” has no cover problems to show now").format(
                    title=album.title,
                ))
            self._show_summary()

    def _finish(self, stopped: bool) -> None:
        self._total = self._done = 0
        self.dialog.checkCoversProgress.hide()
        self._show_running(False)
        self._show_summary(stopped)

    def _show_summary(self, stopped: bool = False) -> None:
        if self.is_running:
            return
        listed = self.dialog.checkCoversList.rowCount()
        albums = len(self._albums)
        if stopped:
            text = (
                tr("Stopped. %n album(s) found so far need(s/) a better cover.", "", listed)
            )
        elif listed:
            text = tr("{listed} of %n album(s) need a better cover.", "", albums).format(listed=listed)
        else:
            text = tr("All {albums} albums have a good cover.").format(albums=albums)
        self.dialog.checkCoversSummary.setText(text)

    def _show_running(self, running: bool) -> None:
        d = self.dialog
        d.checkCoversStartButton.setEnabled(not running)
        d.checkCoversStopButton.setEnabled(running)
        for box in self._options.values():
            box.setEnabled(not running)
        d.checkCoversMinSize.setEnabled(not running and d.checkCoversLowRes.isChecked())
        d.checkCoversMaxSize.setEnabled(not running and d.checkCoversLarge.isChecked())
        self._update_buttons()

    # ----- the list -----

    def _row_of(self, album_id: int) -> int:
        table = self.dialog.checkCoversList
        for row in range(table.rowCount()):
            item = table.item(row, ALBUM_COLUMN)
            if item is not None and item.data(ALBUM_ID_ROLE) == album_id:
                return row
        return -1

    def _show_report(self, album_id: int, report: CoverReport) -> None:
        table = self.dialog.checkCoversList
        row = self._row_of(album_id)
        was_chosen = row >= 0 and self._selected_id() == album_id
        if not report.problems:
            self._reports.pop(album_id, None)
            if row >= 0:
                table.removeRow(row)
            return
        self._reports[album_id] = report
        album = self._albums[album_id]
        table.setSortingEnabled(False)  # or the row moves while it is being filled
        if row < 0:
            row = table.rowCount()
            table.insertRow(row)
        title = QTableWidgetItem(album.title)
        title.setData(ALBUM_ID_ROLE, album_id)
        title.setToolTip(str(album.folder))
        if report.covers and report.covers[0].thumbnail is not None:
            title.setIcon(QIcon(QPixmap.fromImage(report.covers[0].thumbnail)))
        songs = QTableWidgetItem()
        songs.setData(Qt.ItemDataRole.DisplayRole, len(album.paths))
        problems = "; ".join(report.problems)
        problems_item = QTableWidgetItem(problems)
        problems_item.setToolTip(problems)
        table.setItem(row, ALBUM_COLUMN, title)
        table.setItem(row, ARTIST_COLUMN, QTableWidgetItem(album.artist))
        table.setItem(row, SONGS_COLUMN, songs)
        table.setItem(row, COVER_COLUMN, QTableWidgetItem(report.cover_text))
        table.setItem(row, PROBLEMS_COLUMN, problems_item)
        table.setSortingEnabled(True)
        if was_chosen:
            table.selectRow(self._row_of(album_id))
            self._show_album_covers()
        self._update_buttons()

    def _selected_id(self) -> int | None:
        table = self.dialog.checkCoversList
        if not table.selectionModel().hasSelection():
            return None
        item = table.item(table.currentRow(), ALBUM_COLUMN)
        return item.data(ALBUM_ID_ROLE) if item is not None else None

    def _selected_album(self) -> Album | None:
        album_id = self._selected_id()
        return self._albums.get(album_id) if album_id is not None else None

    def _selected_report(self) -> CoverReport | None:
        album_id = self._selected_id()
        return self._reports.get(album_id) if album_id is not None else None

    def _selected_cover(self) -> CoverInfo | None:
        """The cover chosen below the list, or the album's main one."""
        report = self._selected_report()
        if report is None or not report.covers:
            return None
        row = self.dialog.checkCoversAlbumCovers.currentRow()
        return report.covers[row] if 0 <= row < len(report.covers) else report.covers[0]

    def _show_album_covers(self) -> None:
        """Show each different cover of the chosen album, and its skip mark."""
        d = self.dialog
        strip = d.checkCoversAlbumCovers
        strip.clear()
        album = self._selected_album()
        report = self._selected_report()
        if album is not None and report is not None:
            for cover in report.covers:
                icon = QIcon(QPixmap.fromImage(cover.thumbnail)) if cover.thumbnail else QIcon()
                text = f"{_songs(len(cover.paths))}\n{cover.dimensions}\n{cover.source}"
                item = QListWidgetItem(icon, text)
                item.setToolTip(f"{cover.text}\n\n{_names(cover.paths)}")
                item.setSizeHint(strip.gridSize())
                strip.addItem(item)
            if report.without:
                item = QListWidgetItem(QIcon(), tr("{songs}\nwithout a cover").format(
                    songs=_songs(len(report.without)),
                ))
                item.setToolTip(_names(report.without))
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)  # shown, but cannot be chosen
                item.setSizeHint(strip.gridSize())
                strip.addItem(item)
            if report.covers:
                strip.setCurrentRow(0)
        box = d.checkCoversSkipBox
        if album is None or not any(album.skipped):
            box.setCheckState(Qt.CheckState.Unchecked)
        elif all(album.skipped):
            box.setCheckState(Qt.CheckState.Checked)
        else:
            box.setCheckState(Qt.CheckState.PartiallyChecked)
        self._update_buttons()

    def _update_buttons(self) -> None:
        d = self.dialog
        album = self._selected_album()
        chosen = album is not None
        d.checkCoversEditButton.setEnabled(chosen)
        d.checkCoversFindButton.setEnabled(chosen)
        d.checkCoversOpenFolderButton.setEnabled(chosen)
        d.checkCoversSkipBox.setEnabled(chosen)
        cover = self._selected_cover() if chosen else None
        usable = cover is not None and cover.readable
        d.checkCoversSquareButton.setEnabled(usable and not cover.square)
        report = self._selected_report() if chosen else None
        others = report is not None and (len(report.covers) > 1 or bool(report.without))
        d.checkCoversUseCoverButton.setEnabled(usable and others)
        has_rows = d.checkCoversList.rowCount() > 0
        d.checkCoversCopyButton.setEnabled(has_rows)
        d.checkCoversSaveButton.setEnabled(has_rows)

    # ----- changing songs -----

    def _edit(self, find_cover: bool) -> None:
        album = self._selected_album()
        if album is not None:
            self._edit_songs(album.paths, find_cover)

    def _open_folder(self) -> None:
        album = self._selected_album()
        if album is None:
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(album.folder))):
            self._show_status(tr("Cannot open {folder}").format(folder=album.folder))

    def _confirmed(self, title: str, question: str) -> bool:
        answer = QMessageBox.question(
            self.dialog,
            title,
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _cover_data(self, cover: CoverInfo) -> bytes | None:
        """The cover's image, read again from its first song."""
        data, _source, _mark = read_cover(cover.paths[0])
        if not data:
            QMessageBox.warning(
                self.dialog, tr("Cover"), tr("The cover could not be read from the song any more.")
            )
        return data or None

    def _make_square(self) -> None:
        album = self._selected_album()
        cover = self._selected_cover()
        if album is None or cover is None or not cover.readable:
            return
        side = max(cover.width, cover.height)
        if cover.source == "embedded":
            where = tr("in the {songs} that have it").format(songs=_songs(len(cover.paths)))
        else:
            where = tr("over the image file “{source}” in the album's folder").format(
                source=cover.source,
            )
        if not self._confirmed(
            tr("Make square"),
            tr(
                "Stretch the cover of “{title}” from {dimensions} "
                "to {side} × {side}, and save it {where}?"
            ).format(title=album.title, dimensions=cover.dimensions, side=side, where=where),
        ):
            return
        data = self._cover_data(cover)
        if data is None:
            return
        square = stretch_square(data)
        if square is None:
            QMessageBox.warning(self.dialog, tr("Make square"), tr("The cover could not be stretched."))
            return

        if cover.source == "embedded":
            encoded = _encode(square, "PNG" if data.startswith(b"\x89PNG") else "JPEG")
            if encoded is None:
                QMessageBox.warning(self.dialog, tr("Make square"), tr("The cover could not be saved."))
                return
            changes = TagChanges(cover=encoded, cover_changed=True)
            self._save_tags(
                [TagJob(path, changes) for path in cover.paths], tr("Make square: {title}").format(
                    title=album.title,
                )
            )
            return

        # A cover file in the folder: replace that file, in the format its name says.
        files = {find_folder_cover(str(path.parent)) for path in cover.paths} - {None}
        failed = []
        for name in files:
            target = Path(name)
            kind = {".png": "PNG", ".webp": "WEBP"}.get(target.suffix.lower(), "JPEG")
            encoded = _encode(square, kind)
            if encoded is None:
                failed.append(target.name)
                continue
            temp = target.with_name(f".{target.name}.musichive-edit")
            try:
                temp.write_bytes(encoded)
                os.replace(temp, target)
            except OSError as error:
                failed.append(f"{target.name}: {error}")
                temp.unlink(missing_ok=True)
        if failed:
            QMessageBox.warning(self.dialog, tr("Make square"), tr("Could not save ") + "; ".join(failed))
        forget_folder_covers()
        # Nothing changes in the songs, but saving them reads them again, which
        # brings the library's thumbnails up to date and checks the album again.
        self._save_tags([TagJob(path, TagChanges()) for path in album.paths], tr("Make square: {title}").format(
            title=album.title,
        ))

    def _use_for_album(self) -> None:
        album = self._selected_album()
        report = self._selected_report()
        cover = self._selected_cover()
        if album is None or report is None or cover is None:
            return
        others = [path for other in report.covers if other is not cover for path in other.paths]
        targets = others + report.without
        if not targets:
            return
        if not self._confirmed(
            tr("Use for whole album"),
            tr(
                "Save this cover ({text}) in the {songs} of "
                "“{title}” that have another cover or none?"
            ).format(text=cover.text, songs=_songs(len(targets)), title=album.title),
        ):
            return
        data = self._cover_data(cover)
        if data is None:
            return
        changes = TagChanges(cover=data, cover_changed=True)
        self._save_tags([TagJob(path, changes) for path in targets], tr("Cover for whole album: {title}").format(
            title=album.title,
        ))

    def _set_skipped(self) -> None:
        """Write or remove the skip mark in every song of the chosen album."""
        album = self._selected_album()
        if album is None:
            return
        box = self.dialog.checkCoversSkipBox
        skip = box.checkState() != Qt.CheckState.Unchecked  # a click on "partly" means all
        box.setCheckState(Qt.CheckState.Checked if skip else Qt.CheckState.Unchecked)
        jobs = [
            TagJob(path, TagChanges({SKIP_TAG: "true" if skip else ""}))
            for path, marked in zip(album.paths, album.skipped)
            if marked != skip
        ]
        if jobs:
            what = tr("Skip cover problems") if skip else tr("Check cover again")
            self._save_tags(jobs, f"{what}: {album.title}")

    def _on_tags_saved(self, result) -> None:
        """Songs were saved: check their albums again, as the files are now."""
        saved = {job.path for job in result.jobs}
        touched = [
            album_id
            for album_id, album in self._albums.items()
            if saved.intersection(album.paths)
        ]
        if touched:
            self._recheck(touched)

    # ----- handing the list on -----

    def _rows_as_text(self) -> list[list[str]]:
        table = self.dialog.checkCoversList
        rows = [[tr("Album"), tr("Artist"), tr("Songs"), tr("Cover"), tr("Problems"), tr("Folder")]]
        for row in range(table.rowCount()):
            album = self._albums.get(table.item(row, ALBUM_COLUMN).data(ALBUM_ID_ROLE))
            cells = [table.item(row, column).text() for column in range(PROBLEMS_COLUMN + 1)]
            rows.append(cells + [str(album.folder) if album else ""])
        return rows

    def _copy_list(self) -> None:
        text = "\n".join("\t".join(cells) for cells in self._rows_as_text())
        QApplication.clipboard().setText(text + "\n")
        self._show_status(tr("The list of covers to fix was copied"))

    def _save_list(self) -> None:
        start = str(Path.home() / tr("covers to fix.csv"))
        path, _ = QFileDialog.getSaveFileName(
            self.dialog, tr("Save list"), start, tr("CSV files (*.csv)")
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as file:
                csv.writer(file).writerows(self._rows_as_text())
        except OSError as error:
            QMessageBox.warning(self.dialog, tr("Save list"), tr("The list could not be saved: {error}").format(
                error=error,
            ))
            return
        self._show_status(tr("Saved the list to {path}").format(path=path))
