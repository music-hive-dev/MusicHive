"""Tools > Find Duplicates: songs that are in the library more than once.

Songs count as copies of each other when their artist and title match once the
ways of writing them are taken out (case, accents, "&" for "and", "feat." guests
and, if wanted, notes such as "(Remastered 2011)"), and, if wanted, when their
lengths are close and they are on the same album. Notes about a live, acoustic,
demo, remix or instrumental version always count, so those stay apart.

The check works from the library's database and takes no time. The copies of
the chosen song are shown best quality first: lossless before lossy, then the
higher bitrate. Keeping one moves the others to the Trash; their plays, rating
and places in playlists pass to the one kept (main.py does that part).

Songs that only look alike can be marked as not duplicates: a tag in the file
(SKIP_TAG), so the mark stays with the song, and the search leaves them out.
"""

import csv
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHeaderView,
    QMessageBox,
    QTableWidgetItem,
    QWidget,
)

from src import library_db
from src.check_tags import artist_key
from src.lastfm_dialog import load_dialog
from src.library_db import LibraryInfo
from src.number_field import NumberField
from src.tag_writer import TagJob, TagWriter
from src.track_metadata import TagChanges, TrackInfo, read_track_details

SKIP_TAG = "musichivenotduplicate"  # "true": this song is not a copy of another
SETTINGS_PREFIX = "findDuplicates/"
LOSSLESS_SUFFIXES = {".flac", ".wav", ".aiff", ".aif", ".ape", ".wv"}
TITLE_COLUMN, ARTIST_COLUMN, COPIES_COLUMN, ALBUMS_COLUMN = range(4)
(
    ALBUM_COPY_COLUMN, LENGTH_COPY_COLUMN, FORMAT_COPY_COLUMN, SIZE_COPY_COLUMN,
    PLAYS_COPY_COLUMN, RATING_COPY_COLUMN, FILE_COPY_COLUMN,
) = range(7)
INDEX_ROLE = Qt.ItemDataRole.UserRole

# Notes in brackets or after " - " that say nothing about how a song sounds...
_NOTE_WORDS = re.compile(
    r"\b(remaster(ed)?|deluxe|expanded|bonus|explicit|clean|mono|stereo|version|edition"
    r"|anniversary|feat\.?|ft\.?|featuring)(?=\W|$)",
    re.IGNORECASE,
)
# ...unless they are about another way of playing it.
_OTHER_TAKE_WORDS = re.compile(
    r"\b(live|acoustic|demo|remix|mix|instrumental|karaoke|unplugged|session|edit|reprise"
    r"|orchestral|piano|cover)\b",
    re.IGNORECASE,
)
_GUESTS = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s+.*$|\s*[(\[](feat\.?|ft\.?|featuring)\b.*$", re.IGNORECASE)


def is_skipped(value: str) -> bool:
    """Whether the text of a SKIP_TAG says yes."""
    return value.strip().casefold() in ("true", "1", "yes")


def _plain(text: str) -> str:
    """Text with case, accents, "&" and punctuation taken out, for comparing."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"\s*[&+]\s*", " and ", text)
    return re.sub(r"[\W_]+", "", text)


def _is_note(text: str) -> bool:
    return bool(_NOTE_WORDS.search(text)) and not _OTHER_TAKE_WORDS.search(text)


def title_key(title: str, ignore_notes: bool) -> str:
    if ignore_notes:
        title = re.sub(
            r"\s*[(\[]([^)\]]*)[)\]]",
            lambda match: "" if _is_note(match.group(1)) else match.group(0),
            title,
        )
        tail = re.match(r"^(.*?)\s+[-–—]\s+(.+)$", title)
        if tail and _is_note(tail.group(2)):
            title = tail.group(1)
    return _plain(title)


def main_artist_key(artist: str) -> str:
    """The artist without guests ("A feat. B" is A), for comparing."""
    return artist_key(_GUESTS.sub("", artist))


@dataclass
class Copy:
    path: Path
    info: TrackInfo
    skipped: bool

    @property
    def lossless(self) -> bool:
        return self.path.suffix.lower() in LOSSLESS_SUFFIXES

    @property
    def kbps(self) -> float:
        """The average bitrate, worked out from the file size and length."""
        size, length = self.info.file_size or 0, (self.info.duration_ms or 0) / 1000
        return size * 8 / length / 1000 if size and length else 0.0

    def quality(self) -> tuple:
        """Sorts the best copy last: lossless, then bitrate, then plays."""
        return (self.lossless, round(self.kbps / 16), self.info.play_count, self.info.rating)


@dataclass(frozen=True)
class Options:
    ignore_notes: bool = True
    same_album: bool = False
    same_length: bool = True
    length_ms: int = 3000
    include_skipped: bool = False


@dataclass
class Group:
    copies: list[Copy]  # the best first

    @property
    def title(self) -> str:
        return self.copies[0].info.title

    @property
    def artist(self) -> str:
        return self.copies[0].info.artist or self.copies[0].info.album_artist

    @property
    def albums(self) -> str:
        names = dict.fromkeys(copy.info.album or "(no album)" for copy in self.copies)
        return " · ".join(names)

    @property
    def key(self) -> frozenset:
        return frozenset(copy.path for copy in self.copies)


def _by_length(members: list[Copy], within_ms: int) -> list[list[Copy]]:
    """Split songs into runs whose lengths are close. Songs without a known
    length go with the biggest run."""
    timed = sorted((m for m in members if m.info.duration_ms), key=lambda m: m.info.duration_ms)
    untimed = [m for m in members if not m.info.duration_ms]
    runs: list[list[Copy]] = []
    for member in timed:
        if runs and member.info.duration_ms - runs[-1][-1].info.duration_ms <= within_ms:
            runs[-1].append(member)
        else:
            runs.append([member])
    if untimed:
        if runs:
            max(runs, key=len).extend(untimed)
        else:
            runs.append(untimed)
    return runs


def find_groups(copies: list[Copy], options: Options) -> list[Group]:
    buckets: dict[tuple, list[Copy]] = {}
    for copy in copies:
        if copy.skipped and not options.include_skipped:
            continue
        info = copy.info
        artist = main_artist_key(info.artist or info.album_artist or "")
        title = title_key(info.title or copy.path.stem, options.ignore_notes)
        if not artist or not title:
            continue  # nothing to go by
        album = (info.album or "").strip().casefold() if options.same_album else ""
        buckets.setdefault((artist, title, album), []).append(copy)

    groups = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        runs = _by_length(members, options.length_ms) if options.same_length else [members]
        for run in runs:
            if len(run) > 1:
                run.sort(key=lambda copy: copy.quality(), reverse=True)
                groups.append(Group(run))
    return groups


def _length(ms: int | None) -> str:
    if not ms:
        return ""
    seconds = round(ms / 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _size(size: int | None) -> str:
    return f"{size / 1_000_000:.1f} MB" if size else ""


class FindDuplicatesTool(QObject):
    """Connects the widgets of find_duplicates.ui and runs the search."""

    def __init__(
        self,
        ui_path: Path,
        parent: QWidget,
        tag_writer: TagWriter,
        library_songs: Callable[[], tuple[LibraryInfo, list[tuple[Path, TrackInfo]]] | None],
        edit_songs: Callable[[list[Path], bool], None],
        save_tags: Callable[[list[TagJob], str], None],
        play_song: Callable[[Path], None],
        remove_duplicates: Callable[[Path, list[Path]], int],
        show_status: Callable[[str], None],
    ) -> None:
        """`library_songs` gives the open library and its songs (None if there is
        none); `edit_songs(paths, find_cover)` opens the tag editor, `save_tags`
        writes tag changes, `play_song` plays a song, and `remove_duplicates(keep,
        others)` trashes the others and returns how many went."""
        super().__init__(parent)
        self.dialog = d = load_dialog(ui_path, parent)
        self._library_songs = library_songs
        self._edit_songs = edit_songs
        self._save_tags = save_tags
        self._play_song = play_song
        self._remove_duplicates = remove_duplicates
        self._show_status = show_status
        self._settings = QSettings("MusicHive", "MusicHive")
        self._library_id: str | None = None
        self._groups: list[Group] = []
        self._formats: dict[Path, str] = {}  # read from the files when first shown
        self._has_searched = False
        tag_writer.tags_saved.connect(self._on_tags_saved)

        self._length = NumberField(d.findDupesLengthSeconds)
        self._options = {
            "ignoreNotes": d.findDupesIgnoreNotes,
            "sameAlbum": d.findDupesSameAlbum,
            "sameLength": d.findDupesSameLength,
            "showSkipped": d.findDupesShowSkipped,
        }
        for name, box in self._options.items():
            box.setChecked(self._settings.value(SETTINGS_PREFIX + name, box.isChecked(), type=bool))
            box.toggled.connect(
                lambda on, name=name: self._settings.setValue(SETTINGS_PREFIX + name, on)
            )
        self._length.setValue(
            self._settings.value(SETTINGS_PREFIX + "lengthSeconds", self._length.value(), type=int)
        )
        self._length.valueChanged.connect(
            lambda value: self._settings.setValue(SETTINGS_PREFIX + "lengthSeconds", value)
        )
        d.findDupesLengthSeconds.setEnabled(d.findDupesSameLength.isChecked())
        d.findDupesSameLength.toggled.connect(d.findDupesLengthSeconds.setEnabled)

        table = d.findDupesList
        header = table.horizontalHeader()
        header.setSectionResizeMode(TITLE_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(ARTIST_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COPIES_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(ALBUMS_COLUMN, QHeaderView.ResizeMode.Stretch)
        table.sortByColumn(ARTIST_COLUMN, Qt.SortOrder.AscendingOrder)
        table.itemSelectionChanged.connect(self._show_group)
        copies = d.findDupesCopies
        copies.horizontalHeader().setStretchLastSection(True)
        copies.cellDoubleClicked.connect(lambda _row, _column: self._play())
        copies.itemSelectionChanged.connect(self._update_buttons)
        d.findDupesSplitter.setStretchFactor(0, 1)
        d.findDupesSplitter.setStretchFactor(1, 1)

        d.findDupesStartButton.clicked.connect(lambda _checked=False: self._search())
        d.findDupesKeepButton.clicked.connect(lambda _checked=False: self._keep())
        d.findDupesPlayButton.clicked.connect(lambda _checked=False: self._play())
        d.findDupesEditButton.clicked.connect(lambda _checked=False: self._edit())
        d.findDupesOpenFolderButton.clicked.connect(lambda _checked=False: self._open_folder())
        d.findDupesSkipBox.clicked.connect(lambda _checked=False: self._set_skipped())
        d.findDupesCopyButton.clicked.connect(lambda _checked=False: self._copy_list())
        d.findDupesSaveButton.clicked.connect(lambda _checked=False: self._save_list())
        d.findDupesButtonBox.rejected.connect(d.reject)
        self._show_group()

    def show(self, library: LibraryInfo) -> None:
        if library.id != self._library_id:
            self._library_id = library.id
            self._groups = []
            self._has_searched = False
            self.dialog.findDupesList.setRowCount(0)
            self.dialog.findDupesSummary.setText("")
            self._show_group()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    # ----- searching -----

    def _options_chosen(self) -> Options:
        d = self.dialog
        return Options(
            ignore_notes=d.findDupesIgnoreNotes.isChecked(),
            same_album=d.findDupesSameAlbum.isChecked(),
            same_length=d.findDupesSameLength.isChecked(),
            length_ms=int(self._length.value() * 1000),
            include_skipped=d.findDupesShowSkipped.isChecked(),
        )

    def _search(self) -> None:
        """Search the whole library, keeping the chosen song chosen if it is still listed."""
        d = self.dialog
        found = self._library_songs()
        if found is None:
            d.findDupesSummary.setText("Open a library first.")
            return
        library, pairs = found
        chosen = self._selected_group()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            marks = library_db.load_tag_values(library.db_path, [SKIP_TAG])
            copies = []
            for path, info in pairs:
                if info is None:
                    continue
                try:
                    rel_path = path.relative_to(library.root).as_posix()
                except ValueError:
                    rel_path = str(path)
                skipped = is_skipped(marks.get(rel_path, {}).get(SKIP_TAG, ""))
                copies.append(Copy(path, info, skipped))
            self._groups = find_groups(copies, self._options_chosen())
        except Exception as error:
            d.findDupesSummary.setText(f"Cannot read this library: {error}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._library_id = library.id
        self._has_searched = True
        self._fill_list(chosen.key if chosen is not None else None)
        if self._groups:
            extra = sum(len(group.copies) - 1 for group in self._groups)
            count = len(self._groups)
            d.findDupesSummary.setText(
                f"{count} song{'' if count == 1 else 's'} {'is' if count == 1 else 'are'} "
                f"in the library more than once: {extra} cop{'y' if extra == 1 else 'ies'} too many."
            )
        else:
            d.findDupesSummary.setText(f"None of the {len(copies)} songs are in the library twice.")

    def _on_tags_saved(self, _result) -> None:
        """Tags were saved (the library is already up to date): search again."""
        if self._has_searched and self.dialog.isVisible():
            self._search()

    # ----- the lists -----

    def _fill_list(self, chosen_key: frozenset | None) -> None:
        table = self.dialog.findDupesList
        table.setSortingEnabled(False)  # or rows move while they are being filled
        table.setRowCount(len(self._groups))
        for row, group in enumerate(self._groups):
            title = QTableWidgetItem(group.title)
            title.setData(INDEX_ROLE, row)
            copies = QTableWidgetItem()
            copies.setData(Qt.ItemDataRole.DisplayRole, len(group.copies))
            albums = QTableWidgetItem(group.albums)
            albums.setToolTip(group.albums)
            table.setItem(row, TITLE_COLUMN, title)
            table.setItem(row, ARTIST_COLUMN, QTableWidgetItem(group.artist))
            table.setItem(row, COPIES_COLUMN, copies)
            table.setItem(row, ALBUMS_COLUMN, albums)
        table.setSortingEnabled(True)
        table.clearSelection()
        if chosen_key is not None:
            for row in range(table.rowCount()):
                group = self._groups[table.item(row, TITLE_COLUMN).data(INDEX_ROLE)]
                if group.key & chosen_key:  # still has some of the same songs
                    table.selectRow(row)
                    table.scrollToItem(table.item(row, TITLE_COLUMN))
                    break
        self._show_group()

    def _selected_group(self) -> Group | None:
        table = self.dialog.findDupesList
        if not table.selectionModel().hasSelection():
            return None
        item = table.item(table.currentRow(), TITLE_COLUMN)
        if item is None:
            return None
        index = item.data(INDEX_ROLE)
        return self._groups[index] if 0 <= index < len(self._groups) else None

    def _selected_copy(self) -> Copy | None:
        group = self._selected_group()
        copies = self.dialog.findDupesCopies
        row = copies.currentRow()
        if group is None or not copies.selectionModel().hasSelection():
            return None
        return group.copies[row] if 0 <= row < len(group.copies) else None

    def _format(self, copy: Copy) -> str:
        if copy.path not in self._formats:
            try:
                self._formats[copy.path] = read_track_details(copy.path).audio_format
            except Exception:
                self._formats[copy.path] = copy.path.suffix.lstrip(".").upper()
        return self._formats[copy.path]

    def _show_group(self) -> None:
        """Show the copies of the chosen song, the best first, with it chosen."""
        d = self.dialog
        table = d.findDupesCopies
        table.setRowCount(0)
        group = self._selected_group()
        if group is not None:
            table.setRowCount(len(group.copies))
            for row, copy in enumerate(group.copies):
                info = copy.info
                cells = [
                    info.album or "",
                    _length(info.duration_ms),
                    self._format(copy),
                    _size(info.file_size),
                    str(info.play_count or ""),
                    "★" * (info.rating or 0),
                    str(copy.path),
                ]
                for column, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    if column == FILE_COPY_COLUMN:
                        item.setToolTip(str(copy.path))
                    table.setItem(row, column, item)
                if row == 0:
                    best = table.item(row, FORMAT_COPY_COLUMN)
                    font = best.font()
                    font.setBold(True)
                    best.setFont(font)
                    best.setToolTip("The best quality of these copies")
            table.resizeColumnsToContents()
            table.selectRow(0)

        box = d.findDupesSkipBox
        skipped = [copy.skipped for copy in group.copies] if group is not None else []
        if not any(skipped):
            box.setCheckState(Qt.CheckState.Unchecked)
        elif all(skipped):
            box.setCheckState(Qt.CheckState.Checked)
        else:
            box.setCheckState(Qt.CheckState.PartiallyChecked)
        box.setEnabled(group is not None)
        has_rows = d.findDupesList.rowCount() > 0
        d.findDupesCopyButton.setEnabled(has_rows)
        d.findDupesSaveButton.setEnabled(has_rows)
        self._update_buttons()

    def _update_buttons(self) -> None:
        d = self.dialog
        chosen = self._selected_copy() is not None
        for button in (
            d.findDupesKeepButton, d.findDupesPlayButton,
            d.findDupesEditButton, d.findDupesOpenFolderButton,
        ):
            button.setEnabled(chosen)

    # ----- acting on copies -----

    def _keep(self) -> None:
        group = self._selected_group()
        keep = self._selected_copy()
        if group is None or keep is None:
            return
        others = [copy.path for copy in group.copies if copy is not keep]
        names = "\n".join(str(path) for path in others)
        answer = QMessageBox.question(
            self.dialog,
            "Keep this one",
            f"Keep\n{keep.path}\n\nand move {'this copy' if len(others) == 1 else 'these copies'} "
            f"to the Trash?\n{names}\n\n"
            "Their plays, rating and places in playlists pass to the one you keep.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = self._remove_duplicates(keep.path, others)
        if removed:
            self._show_status(
                f"Kept '{keep.info.title}' and moved {removed} cop{'y' if removed == 1 else 'ies'} to the Trash"
            )
            self._search()

    def _play(self) -> None:
        copy = self._selected_copy()
        if copy is not None:
            self._play_song(copy.path)

    def _edit(self) -> None:
        copy = self._selected_copy()
        if copy is not None:
            self._edit_songs([copy.path], False)

    def _open_folder(self) -> None:
        copy = self._selected_copy()
        if copy is not None and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(copy.path.parent))):
            self._show_status(f"Cannot open {copy.path.parent}")

    def _set_skipped(self) -> None:
        """Write or remove the "not a duplicate" mark in every copy of the chosen song."""
        group = self._selected_group()
        if group is None:
            return
        box = self.dialog.findDupesSkipBox
        skip = box.checkState() != Qt.CheckState.Unchecked  # a click on "partly" means all
        box.setCheckState(Qt.CheckState.Checked if skip else Qt.CheckState.Unchecked)
        jobs = [
            TagJob(copy.path, TagChanges({SKIP_TAG: "true" if skip else ""}))
            for copy in group.copies
            if copy.skipped != skip
        ]
        if jobs:
            what = "Not duplicates" if skip else "Maybe duplicates"
            self._save_tags(jobs, f"{what}: {group.title}")

    # ----- handing the list on -----

    def _rows_as_text(self) -> list[list[str]]:
        """Every copy on its own line, numbered by the song it is a copy of."""
        table = self.dialog.findDupesList
        rows = [["Song", "Title", "Artist", "Album", "Length", "Size", "Plays", "Rating", "File"]]
        for row in range(table.rowCount()):
            group = self._groups[table.item(row, TITLE_COLUMN).data(INDEX_ROLE)]
            for copy in group.copies:
                info = copy.info
                rows.append([
                    str(row + 1), info.title, info.artist, info.album or "",
                    _length(info.duration_ms), _size(info.file_size),
                    str(info.play_count or 0), str(info.rating or 0), str(copy.path),
                ])
        return rows

    def _copy_list(self) -> None:
        text = "\n".join("\t".join(cells) for cells in self._rows_as_text())
        QApplication.clipboard().setText(text + "\n")
        self._show_status("The list of duplicates was copied")

    def _save_list(self) -> None:
        start = str(Path.home() / "duplicate songs.csv")
        path, _ = QFileDialog.getSaveFileName(self.dialog, "Save list", start, "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as file:
                csv.writer(file).writerows(self._rows_as_text())
        except OSError as error:
            QMessageBox.warning(self.dialog, "Save list", f"The list could not be saved: {error}")
            return
        self._show_status(f"Saved the list to {path}")
