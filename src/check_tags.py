"""Tools > Check Tags: find albums and artists whose tags need tidying up.

The check works from the library's database, which holds every tag of every
song, so it does not read the files and takes no time at all. It lists:

- songs without a title, artist, album, year, genre or track number;
- albums with two songs of the same number, numbers above the track total, or
  numbers missing between 1 and the highest one;
- albums whose songs disagree on the year, the album artist or how the album
  name is written (and, if wanted, the genre);
- artists written more than one way, such as "The Beatles" and "Beatles".

The songs of the chosen entry are shown with the tags that need a look marked.
A tag can be given one value in all of them, an album's tracks can be numbered
again, or the songs opened in the tag editor. Once tags are saved, the check
runs again, so what is fixed leaves the list.

Songs whose tags are fine as they are can be marked to skip: a tag in the file
(SKIP_TAG), so the mark stays with the song, and the check leaves them out.
"""

import csv
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QUrl, Qt
from PySide6.QtGui import QColor, QDesktopServices, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHeaderView,
    QMessageBox,
    QTableWidgetItem,
    QWidget,
)

from src import library_db
from src.lastfm_dialog import load_dialog
from src.musicbrainz_dialog import AlbumToLookUp, MusicBrainzLookup
from src.library_db import LibraryInfo
from src.tag_writer import TagJob, TagWriter
from src.track_metadata import TagChanges, TrackInfo

SKIP_TAG = "musichiveskiptags"  # "true": leave this song's tags alone
SETTINGS_PREFIX = "checkTags/"
TEXT_FIELDS = ("title", "artist", "albumartist", "album", "date", "genre")
NUMBER_TAGS = ("tracknumber", "tracktotal", "discnumber", "disctotal")
LABELS = {
    "title": "title",
    "artist": "artist",
    "albumartist": "album artist",
    "album": "album",
    "date": "year",
    "genre": "genre",
    "tracknumber": "track number",
    "discnumber": "disc number",
}
# The columns of checkTagsSongs: the tag each one shows (None: the file name).
SONG_COLUMNS = (
    "discnumber", "tracknumber", "title", "artist", "albumartist", "album", "date", "genre", None,
)
ALBUM_FIELDS = ("date", "genre", "albumartist", "album")  # offered by "Make the same"
ALBUM_COLUMN, ARTIST_COLUMN, SONGS_COLUMN, PROBLEMS_COLUMN = range(4)
INDEX_ROLE = Qt.ItemDataRole.UserRole
LISTED_NUMBERS = 6  # track numbers named in a problem, before "and N more"
HIGHEST_TRACK = 999  # gaps are not looked for above this
PREVIEW_LINES = 15


def is_skipped(value: str) -> bool:
    """Whether the text of a SKIP_TAG says yes."""
    return value.strip().casefold() in ("true", "1", "yes")


def artist_key(name: str) -> str:
    """What is left of an artist's name when the ways of writing it are taken out:
    case, accents, "The" in front, "&" for "and", spaces and punctuation."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold().strip()
    text = re.sub(r"\s*[&+]\s*", " and ", text)
    text = re.sub(r"^the\s+", "", text)
    return re.sub(r"[\W_]+", "", text)


def _number(text: str) -> int | None:
    match = re.match(r"\s*(\d+)", text or "")
    return int(match.group(1)) if match else None


def _total(number_text: str, total_text: str) -> int | None:
    """A total from its own tag, or from "3/12" in the number's."""
    total = _number(total_text)
    if total is None and "/" in (number_text or ""):
        total = _number(number_text.partition("/")[2])
    return total


def _year(text: str) -> str:
    text = text.strip()
    return text[:4] if text[:4].isdigit() else text


def _songs(count: int) -> str:
    return f"{count} song{'' if count == 1 else 's'}"


def _numbers(numbers: list[int]) -> str:
    shown = ", ".join(str(n) for n in numbers[:LISTED_NUMBERS])
    if len(numbers) > LISTED_NUMBERS:
        shown += f" and {len(numbers) - LISTED_NUMBERS} more"
    return shown


@dataclass
class Song:
    path: Path
    values: dict[str, str]  # each of TEXT_FIELDS, "" if missing; "date" is only the year
    track: int | None
    disc: int | None
    track_total: int | None
    skipped: bool
    disc_total: int | None = None
    duration_ms: int | None = None
    full_date: str = ""  # the date tag as it is, such as "1969-09-26"

    @property
    def disc_key(self) -> int:
        return self.disc or 1


@dataclass(frozen=True)
class Limits:
    """Which problems to look for."""

    missing: frozenset[str] = frozenset()  # tags that songs should have
    duplicates: bool = True
    gaps: bool = True
    mixed: bool = True
    mixed_genre: bool = False
    spellings: bool = True
    include_skipped: bool = False


@dataclass
class Issue:
    """One entry of the list: an album, the songs of a folder without an album,
    or an artist written more than one way."""

    key: tuple
    kind: str  # "album", "loose" or "artist"
    title: str
    artist: str
    songs: list[Song]
    problems: list[str] = field(default_factory=list)
    marks: dict[Path, dict[str, str]] = field(default_factory=dict)  # song -> tag -> why
    fields: list[str] = field(default_factory=list)  # tags "Make the same" offers
    spelling: str | None = None  # the artist_key of an "artist" entry

    def mark(self, song: Song, tag: str, why: str) -> None:
        self.marks.setdefault(song.path, {})[tag] = why

    @property
    def folder(self) -> Path:
        try:
            return Path(os.path.commonpath([str(song.path.parent) for song in self.songs]))
        except ValueError:  # songs on different drives
            return self.songs[0].path.parent

    def applies_to(self, song: Song, tag: str) -> bool:
        """Whether "Make the same" may change this tag of this song. For an artist
        entry, only the ways of writing that artist are changed, not other artists."""
        if self.spelling is None:
            return True
        value = song.values[tag]
        return bool(value) and artist_key(value) == self.spelling


def make_songs(
    library: LibraryInfo, songs: list[tuple[Path, TrackInfo | None]]
) -> list[Song]:
    """The library's songs with the tags the check needs, read from its database."""
    tags = library_db.load_tag_values(library.db_path, [*TEXT_FIELDS, *NUMBER_TAGS, SKIP_TAG])
    made = []
    for path, info in songs:
        try:
            rel_path = path.relative_to(library.root).as_posix()
        except ValueError:
            rel_path = str(path)
        found = tags.get(rel_path, {})
        values = {name: found.get(name, "").strip() for name in TEXT_FIELDS}
        values["date"] = _year(values["date"])
        track_text, disc_text = found.get("tracknumber", ""), found.get("discnumber", "")
        track = info.track_number if info is not None else _number(track_text)
        disc = info.disc_number if info is not None else _number(disc_text)
        made.append(
            Song(
                path,
                values,
                track,
                disc,
                _total(track_text, found.get("tracktotal", "")),
                is_skipped(found.get(SKIP_TAG, "")),
                _total(disc_text, found.get("disctotal", "")),
                info.duration_ms if info is not None else None,
                found.get("date", "").strip(),
            )
        )
    return made


def album_groups(songs: list[Song]) -> dict[tuple, list[Song]]:
    """Songs by album: those of one album name in one folder, joined with the same
    album's songs in other folders (such as CD1 and CD2) when they share an album
    artist. Songs without an album name are grouped by folder."""
    folders: dict[tuple, list[Song]] = {}
    for song in songs:
        album = song.values["album"]
        kind = "album" if album else "loose"
        folders.setdefault((kind, album.casefold(), str(song.path.parent)), []).append(song)

    parent = {key: key for key in folders}

    def root(key: tuple) -> tuple:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    first_with: dict[tuple[str, str], tuple] = {}
    for key, members in folders.items():
        if key[0] != "album":
            continue
        for album_artist in {song.values["albumartist"].casefold() for song in members} - {""}:
            other = first_with.setdefault((key[1], album_artist), key)
            parent[root(key)] = root(other)

    groups: dict[tuple, list[Song]] = {}
    for key, members in folders.items():
        groups.setdefault(root(key), []).extend(members)
    return groups


def _disagreement(issue: Issue, tag: str) -> None:
    """Note it when the songs have more than one value for this tag."""
    songs = issue.songs
    counts = Counter(song.values[tag] for song in songs if song.values[tag])
    if not counts:
        return
    main = counts.most_common(1)[0][0]
    if len(counts) > 1:
        if tag == "album":
            issue.problems.append(f"Album name written {len(counts)} ways")
        else:
            issue.problems.append(f"{len(counts)} different {LABELS[tag]}s")
        for song in songs:
            if song.values[tag] and song.values[tag] != main:
                issue.mark(song, tag, f"Most songs have “{main}”")
    if tag in ("albumartist", "date"):
        without = [song for song in songs if not song.values[tag]]
        if without:
            issue.problems.append(f"{_songs(len(without))} without {LABELS[tag]}")
            for song in without:
                issue.mark(song, tag, f"No {LABELS[tag]}; most songs have “{main}”")


def _track_numbers(issue: Issue, limits: Limits) -> None:
    numbered = [song for song in issue.songs if song.track is not None]
    discs = sorted({song.disc_key for song in issue.songs})
    for disc in discs:
        members = [song for song in numbered if song.disc_key == disc]
        if not members:
            continue
        where = f"Disc {disc}: t" if len(discs) > 1 else "T"
        counts = Counter(song.track for song in members)
        if limits.duplicates:
            doubled = sorted(n for n, count in counts.items() if count > 1)
            if doubled:
                s = "s" if len(doubled) > 1 else ""
                issue.problems.append(f"{where}rack{s} {_numbers(doubled)} used more than once")
                for song in members:
                    if counts[song.track] > 1:
                        issue.mark(song, "tracknumber", "Another song of this disc has this number")
            totals = Counter(song.track_total for song in members if song.track_total)
            total = totals.most_common(1)[0][0] if totals else None
            impossible = [
                song for song in members if song.track < 1 or (total and song.track > total)
            ]
            if impossible:
                above = f" (the disc has {total})" if total else ""
                issue.problems.append(f"{where}rack numbers that cannot be right{above}")
                for song in impossible:
                    issue.mark(song, "tracknumber", f"The disc has {total} tracks" if total else "")
        if limits.gaps:
            top = min(max(counts), HIGHEST_TRACK)
            missing = sorted(set(range(1, top + 1)) - set(counts))
            if missing:
                s = "s" if len(missing) > 1 else ""
                issue.problems.append(f"{where}rack{s} {_numbers(missing)} missing")


def check_album(key: tuple, songs: list[Song], limits: Limits) -> Issue:
    songs.sort(key=lambda song: (song.disc_key, song.track or HIGHEST_TRACK + 1, song.path.name))
    kind = key[0]
    if kind == "album":
        title = Counter(song.values["album"] for song in songs).most_common(1)[0][0]
    else:
        title = f"Songs without an album · {Path(key[2]).name}"
    album_artists = Counter(song.values["albumartist"] for song in songs if song.values["albumartist"])
    artists = {song.values["artist"] for song in songs} - {""}
    if album_artists:
        artist = album_artists.most_common(1)[0][0]
    elif len(artists) == 1:
        artist = next(iter(artists))
    else:
        artist = "Various artists" if artists else ""
    issue = Issue(key, kind, title, artist, songs, fields=list(ALBUM_FIELDS))

    for tag in ("title", "artist", "album", "date", "genre", "tracknumber"):
        if tag not in limits.missing or (tag == "album" and kind == "album"):
            continue
        if tag == "tracknumber":
            if kind != "album":
                continue
            without = [song for song in songs if song.track is None]
        else:
            without = [song for song in songs if not song.values[tag]]
        if without:
            issue.problems.append(f"{_songs(len(without))} without {LABELS[tag]}")
            for song in without:
                issue.mark(song, tag, f"No {LABELS[tag]}")

    if kind == "album":
        if limits.mixed:
            for tag in ("albumartist", "album", "date"):
                _disagreement(issue, tag)
        if limits.mixed_genre:
            _disagreement(issue, "genre")
        if limits.duplicates or limits.gaps:
            _track_numbers(issue, limits)

    issue.problems = list(dict.fromkeys(issue.problems))  # each said once
    skipped = sum(song.skipped for song in songs)
    if issue.problems and skipped:
        issue.problems.insert(0, f"{_songs(skipped)} marked to skip")
    return issue


def spelling_issues(songs: list[Song]) -> list[Issue]:
    """An entry for each artist whose name is written more than one way."""
    spellings: dict[str, Counter] = {}
    for song in songs:
        for tag in ("artist", "albumartist"):
            name = song.values[tag]
            key = artist_key(name) if name else ""
            if key:
                spellings.setdefault(key, Counter())[name] += 1
    issues = []
    for key, counts in spellings.items():
        if len(counts) < 2:
            continue
        names = set(counts)
        members = [
            song for song in songs
            if song.values["artist"] in names or song.values["albumartist"] in names
        ]
        ranked = counts.most_common()
        main = ranked[0][0]
        issue = Issue(
            ("artist", key),
            "artist",
            "Artist written more than one way",
            main,
            members,
            [f"{len(counts)} ways: " + ", ".join(f"“{name}” ({n}×)" for name, n in ranked)],
            fields=[t for t in ("artist", "albumartist") if any(s.values[t] in names for s in members)],
            spelling=key,
        )
        for song in members:
            for tag in issue.fields:
                if song.values[tag] in names and song.values[tag] != main:
                    issue.mark(song, tag, f"Most songs have “{main}”")
        skipped = sum(song.skipped for song in members)
        if skipped:
            issue.problems.insert(0, f"{_songs(skipped)} marked to skip")
        issues.append(issue)
    return issues


def find_issues(songs: list[Song], limits: Limits) -> list[Issue]:
    checked = [song for song in songs if limits.include_skipped or not song.skipped]
    issues = [
        issue
        for key, members in album_groups(checked).items()
        if (issue := check_album(key, members, limits)).problems
    ]
    if limits.spellings:
        issues.extend(spelling_issues(checked))
    return issues


def numbered_again(songs: list[Song]) -> list[tuple[Song, int]]:
    """New track numbers for an album, disc by disc: the numbers the file names
    start with, if every song of the disc has a different one; otherwise the
    position in file name order. Only the songs whose number changes."""
    changes = []
    for disc in sorted({song.disc_key for song in songs}):
        members = sorted((s for s in songs if s.disc_key == disc), key=lambda s: s.path.name.casefold())
        from_names = [_number(song.path.stem) for song in members]
        # A year or a catalogue number in front of the names is not a track number.
        usable = None not in from_names and len(set(from_names)) == len(from_names)
        if not usable or min(from_names) < 1 or max(from_names) > max(len(members) * 2, 99):
            new_numbers = list(range(1, len(members) + 1))
        else:
            new_numbers = from_names
        changes.extend(
            (song, number) for song, number in zip(members, new_numbers) if song.track != number
        )
    return changes


class CheckTagsTool(QObject):
    """Connects the widgets of check_tags.ui and runs the check."""

    def __init__(
        self,
        ui_path: Path,
        parent: QWidget,
        tag_writer: TagWriter,
        musicbrainz_ui_path: Path,
        library_songs: Callable[[], tuple[LibraryInfo, list[tuple[Path, TrackInfo]]] | None],
        edit_songs: Callable[[list[Path], bool], None],
        save_tags: Callable[[list[TagJob]], None],
        show_status: Callable[[str], None],
    ) -> None:
        """`library_songs` gives the open library and its songs (None if there is
        none); `edit_songs(paths, find_cover)` opens the tag editor for songs, and
        `save_tags` writes tag changes to songs of the open library."""
        super().__init__(parent)
        self.dialog = d = load_dialog(ui_path, parent)
        self._musicbrainz_ui_path = musicbrainz_ui_path
        self._library_songs = library_songs
        self._edit_songs = edit_songs
        self._save_tags = save_tags
        self._show_status = show_status
        self._settings = QSettings("MusicHive", "MusicHive")
        self._library_id: str | None = None
        self._issues: list[Issue] = []
        self._has_checked = False
        tag_writer.tags_saved.connect(self._on_tags_saved)

        self._missing_boxes = {
            "title": d.checkTagsMissingTitle,
            "artist": d.checkTagsMissingArtist,
            "album": d.checkTagsMissingAlbum,
            "date": d.checkTagsMissingYear,
            "genre": d.checkTagsMissingGenre,
            "tracknumber": d.checkTagsMissingTrack,
        }
        options = {f"missing_{tag}": box for tag, box in self._missing_boxes.items()}
        options.update(
            duplicates=d.checkTagsDuplicates,
            gaps=d.checkTagsGaps,
            mixed=d.checkTagsMixed,
            mixedGenre=d.checkTagsMixedGenre,
            spellings=d.checkTagsSpellings,
            showSkipped=d.checkTagsShowSkipped,
        )
        for name, box in options.items():
            box.setChecked(self._settings.value(SETTINGS_PREFIX + name, box.isChecked(), type=bool))
            box.toggled.connect(
                lambda on, name=name: self._settings.setValue(SETTINGS_PREFIX + name, on)
            )

        table = d.checkTagsList
        header = table.horizontalHeader()
        header.setSectionResizeMode(ALBUM_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(ARTIST_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(SONGS_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(PROBLEMS_COLUMN, QHeaderView.ResizeMode.Stretch)
        table.sortByColumn(ARTIST_COLUMN, Qt.SortOrder.AscendingOrder)
        table.cellDoubleClicked.connect(lambda _row, _column: self._edit_all())
        table.itemSelectionChanged.connect(self._show_issue)
        songs_header = d.checkTagsSongs.horizontalHeader()
        songs_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        songs_header.setStretchLastSection(True)
        d.checkTagsSongs.cellDoubleClicked.connect(lambda row, _column: self._edit_song(row))
        d.checkTagsSplitter.setStretchFactor(0, 1)
        d.checkTagsSplitter.setStretchFactor(1, 1)

        d.checkTagsField.currentIndexChanged.connect(lambda _index: self._fill_values())
        d.checkTagsApplyButton.clicked.connect(lambda _checked=False: self._apply_value())
        d.checkTagsNumberButton.clicked.connect(lambda _checked=False: self._number_tracks())
        d.checkTagsLookUpButton.clicked.connect(lambda _checked=False: self._look_up(every=False))
        d.checkTagsLookUpAllButton.clicked.connect(lambda _checked=False: self._look_up(every=True))
        d.checkTagsStartButton.clicked.connect(lambda _checked=False: self._check())
        d.checkTagsEditButton.clicked.connect(lambda _checked=False: self._edit_all())
        d.checkTagsOpenFolderButton.clicked.connect(lambda _checked=False: self._open_folder())
        d.checkTagsSkipBox.clicked.connect(lambda _checked=False: self._set_skipped())
        d.checkTagsCopyButton.clicked.connect(lambda _checked=False: self._copy_list())
        d.checkTagsSaveButton.clicked.connect(lambda _checked=False: self._save_list())
        d.checkTagsButtonBox.rejected.connect(d.reject)
        self._show_issue()

    def show(self, library: LibraryInfo) -> None:
        if library.id != self._library_id:
            self._library_id = library.id
            self._issues = []
            self._has_checked = False
            self.dialog.checkTagsList.setRowCount(0)
            self.dialog.checkTagsSummary.setText("")
            self._show_issue()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    # ----- checking -----

    def _limits(self) -> Limits:
        d = self.dialog
        return Limits(
            missing=frozenset(tag for tag, box in self._missing_boxes.items() if box.isChecked()),
            duplicates=d.checkTagsDuplicates.isChecked(),
            gaps=d.checkTagsGaps.isChecked(),
            mixed=d.checkTagsMixed.isChecked(),
            mixed_genre=d.checkTagsMixedGenre.isChecked(),
            spellings=d.checkTagsSpellings.isChecked(),
            include_skipped=d.checkTagsShowSkipped.isChecked(),
        )

    def _check(self) -> None:
        """Check the whole library, keeping the chosen entry chosen if it is still listed."""
        d = self.dialog
        found = self._library_songs()
        if found is None:
            d.checkTagsSummary.setText("Open a library first.")
            return
        library, pairs = found
        chosen = self._selected_issue()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            songs = make_songs(library, pairs)
            self._issues = find_issues(songs, self._limits())
        except Exception as error:
            d.checkTagsSummary.setText(f"Cannot read this library: {error}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._library_id = library.id
        self._has_checked = True
        self._fill_list(chosen.key if chosen is not None else None)
        albums = sum(issue.kind != "artist" for issue in self._issues)
        artists = len(self._issues) - albums
        if not self._issues:
            text = f"The tags of all {len(songs)} songs look tidy."
        else:
            parts = []
            if albums:
                parts.append(f"{albums} album{'' if albums == 1 else 's'} or folder{'' if albums == 1 else 's'}")
            if artists:
                parts.append(f"{artists} artist{'' if artists == 1 else 's'}")
            text = f"{' and '.join(parts)} need a look."
            text = text[0].upper() + text[1:]
        d.checkTagsSummary.setText(text)

    def _on_tags_saved(self, _result) -> None:
        """Tags were saved (the library is already up to date): check again."""
        if self._has_checked and self.dialog.isVisible():
            self._check()

    # ----- the list -----

    def _fill_list(self, chosen_key: tuple | None) -> None:
        table = self.dialog.checkTagsList
        table.setSortingEnabled(False)  # or rows move while they are being filled
        table.setRowCount(len(self._issues))
        for row, issue in enumerate(self._issues):
            title = QTableWidgetItem(issue.title)
            title.setData(INDEX_ROLE, row)
            title.setToolTip(str(issue.folder))
            songs = QTableWidgetItem()
            songs.setData(Qt.ItemDataRole.DisplayRole, len(issue.songs))
            problems = "; ".join(issue.problems)
            problems_item = QTableWidgetItem(problems)
            problems_item.setToolTip(problems)
            table.setItem(row, ALBUM_COLUMN, title)
            table.setItem(row, ARTIST_COLUMN, QTableWidgetItem(issue.artist))
            table.setItem(row, SONGS_COLUMN, songs)
            table.setItem(row, PROBLEMS_COLUMN, problems_item)
        table.setSortingEnabled(True)
        table.clearSelection()
        if chosen_key is not None:
            for row in range(table.rowCount()):
                issue = self._issues[table.item(row, ALBUM_COLUMN).data(INDEX_ROLE)]
                if issue.key == chosen_key:
                    table.selectRow(row)
                    table.scrollToItem(table.item(row, ALBUM_COLUMN))
                    break
        self._show_issue()

    def _selected_issue(self) -> Issue | None:
        table = self.dialog.checkTagsList
        if not table.selectionModel().hasSelection():
            return None
        item = table.item(table.currentRow(), ALBUM_COLUMN)
        if item is None:
            return None
        index = item.data(INDEX_ROLE)
        return self._issues[index] if 0 <= index < len(self._issues) else None

    def _show_issue(self) -> None:
        """Show the chosen entry's songs, with the tags that need a look marked."""
        d = self.dialog
        issue = self._selected_issue()
        songs_table = d.checkTagsSongs
        songs_table.setRowCount(0)
        mark_colour = QColor(songs_table.palette().color(QPalette.ColorRole.Highlight))
        mark_colour.setAlpha(90)
        if issue is not None:
            songs_table.setRowCount(len(issue.songs))
            for row, song in enumerate(issue.songs):
                marks = issue.marks.get(song.path, {})
                for column, tag in enumerate(SONG_COLUMNS):
                    if tag is None:
                        text = song.path.name
                    elif tag == "tracknumber":
                        text = "" if song.track is None else str(song.track)
                    elif tag == "discnumber":
                        text = "" if song.disc is None else str(song.disc)
                    else:
                        text = song.values[tag]
                    item = QTableWidgetItem(text)
                    if tag in marks:
                        item.setBackground(mark_colour)
                        item.setToolTip(marks[tag] or f"Check the {LABELS[tag]}")
                    elif tag is None:
                        item.setToolTip(str(song.path))
                    songs_table.setItem(row, column, item)
            songs_table.resizeColumnsToContents()

        combo = d.checkTagsField
        combo.blockSignals(True)
        combo.clear()
        if issue is not None:
            for tag in issue.fields:
                combo.addItem(LABELS[tag].capitalize(), tag)
        combo.blockSignals(False)
        self._fill_values()

        box = d.checkTagsSkipBox
        skipped = [song.skipped for song in issue.songs] if issue is not None else []
        if not any(skipped):
            box.setCheckState(Qt.CheckState.Unchecked)
        elif all(skipped):
            box.setCheckState(Qt.CheckState.Checked)
        else:
            box.setCheckState(Qt.CheckState.PartiallyChecked)

        chosen = issue is not None
        d.checkTagsEditButton.setEnabled(chosen)
        d.checkTagsOpenFolderButton.setEnabled(chosen)
        d.checkTagsSkipBox.setEnabled(chosen)
        d.checkTagsField.setEnabled(chosen and combo.count() > 0)
        d.checkTagsValue.setEnabled(chosen and combo.count() > 0)
        d.checkTagsApplyButton.setEnabled(chosen and combo.count() > 0)
        d.checkTagsNumberButton.setEnabled(chosen and issue.kind == "album")
        d.checkTagsLookUpButton.setEnabled(chosen and issue.kind == "album")
        has_rows = d.checkTagsList.rowCount() > 0
        d.checkTagsLookUpAllButton.setEnabled(any(i.kind == "album" for i in self._issues))
        d.checkTagsCopyButton.setEnabled(has_rows)
        d.checkTagsSaveButton.setEnabled(has_rows)

    def _fill_values(self) -> None:
        """Offer the values the songs have for the chosen tag, the most common first."""
        d = self.dialog
        values = d.checkTagsValue
        values.clear()
        issue = self._selected_issue()
        tag = d.checkTagsField.currentData()
        if issue is None or tag is None:
            return
        counts = Counter(
            song.values[tag]
            for song in issue.songs
            if song.values[tag] and issue.applies_to(song, tag)
        )
        for value, count in counts.most_common():
            values.addItem(f"{value}  ({_songs(count)})", value)
        values.setCurrentIndex(0 if counts else -1)

    def _chosen_value(self) -> str:
        """The value picked from the list, or the one typed."""
        values = self.dialog.checkTagsValue
        index = values.currentIndex()
        if index >= 0 and values.currentText() == values.itemText(index):
            return str(values.itemData(index))
        return values.currentText().strip()

    # ----- changing songs -----

    def _confirmed(self, title: str, question: str) -> bool:
        answer = QMessageBox.question(
            self.dialog,
            title,
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _apply_value(self) -> None:
        issue = self._selected_issue()
        tag = self.dialog.checkTagsField.currentData()
        if issue is None or tag is None:
            return
        value = self._chosen_value()
        if not value:
            QMessageBox.information(self.dialog, "Make the same", "Choose or type a value first.")
            return
        if tag == "date":
            value = _year(value)
        targets = [
            song for song in issue.songs
            if song.values[tag] != value and issue.applies_to(song, tag)
        ]
        if not targets:
            self._show_status(f"All of these songs already have that {LABELS[tag]}")
            return
        if not self._confirmed(
            "Make the same",
            f"Set the {LABELS[tag]} to “{value}” in {_songs(len(targets))}?",
        ):
            return
        self._save_tags([TagJob(song.path, TagChanges({tag: value})) for song in targets])

    def _number_tracks(self) -> None:
        issue = self._selected_issue()
        if issue is None or issue.kind != "album":
            return
        changes = numbered_again(issue.songs)
        if not changes:
            self._show_status("The track numbers already follow the file names")
            return
        lines = [
            f"{song.path.name}: {song.track if song.track is not None else '–'} → {number}"
            for song, number in changes[:PREVIEW_LINES]
        ]
        if len(changes) > PREVIEW_LINES:
            lines.append(f"… and {len(changes) - PREVIEW_LINES} more")
        if not self._confirmed(
            "Number tracks",
            f"Give {_songs(len(changes))} of “{issue.title}” a new track number?\n\n"
            + "\n".join(lines),
        ):
            return
        self._save_tags(
            [TagJob(song.path, TagChanges({"tracknumber": str(number)})) for song, number in changes]
        )

    def _look_up(self, every: bool) -> None:
        """Look the chosen album up on MusicBrainz, or every album in the list,
        in the order shown."""
        if every:
            table = self.dialog.checkTagsList
            issues = [
                self._issues[table.item(row, ALBUM_COLUMN).data(INDEX_ROLE)]
                for row in range(table.rowCount())
            ]
        else:
            issue = self._selected_issue()
            issues = [issue] if issue is not None else []
        albums = []
        for issue in issues:
            if issue.kind != "album":
                continue
            years = Counter(song.values["date"] for song in issue.songs if song.values["date"])
            year = years.most_common(1)[0][0] if years else ""
            albums.append(AlbumToLookUp(issue.title, issue.artist, year, list(issue.songs)))
        if not albums:
            return
        lookup = MusicBrainzLookup(self._musicbrainz_ui_path, self.dialog, albums, self._save_tags)
        lookup.run()
        if lookup.saved_songs:
            self._show_status(
                f"Saving tags from MusicBrainz in {_songs(lookup.saved_songs)} of "
                f"{lookup.saved_albums} album{'' if lookup.saved_albums == 1 else 's'}…"
            )

    def _set_skipped(self) -> None:
        """Write or remove the skip mark in every song of the chosen entry."""
        issue = self._selected_issue()
        if issue is None:
            return
        box = self.dialog.checkTagsSkipBox
        skip = box.checkState() != Qt.CheckState.Unchecked  # a click on "partly" means all
        box.setCheckState(Qt.CheckState.Checked if skip else Qt.CheckState.Unchecked)
        jobs = [
            TagJob(song.path, TagChanges({SKIP_TAG: "true" if skip else ""}))
            for song in issue.songs
            if song.skipped != skip
        ]
        if jobs:
            self._save_tags(jobs)

    def _edit_all(self) -> None:
        issue = self._selected_issue()
        if issue is not None:
            self._edit_songs([song.path for song in issue.songs], False)

    def _edit_song(self, row: int) -> None:
        issue = self._selected_issue()
        if issue is not None and 0 <= row < len(issue.songs):
            self._edit_songs([issue.songs[row].path], False)

    def _open_folder(self) -> None:
        issue = self._selected_issue()
        if issue is None:
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(issue.folder))):
            self._show_status(f"Cannot open {issue.folder}")

    # ----- handing the list on -----

    def _rows_as_text(self) -> list[list[str]]:
        table = self.dialog.checkTagsList
        rows = [["Album", "Artist", "Songs", "Problems", "Folder"]]
        for row in range(table.rowCount()):
            issue = self._issues[table.item(row, ALBUM_COLUMN).data(INDEX_ROLE)]
            cells = [table.item(row, column).text() for column in range(PROBLEMS_COLUMN + 1)]
            rows.append(cells + [str(issue.folder)])
        return rows

    def _copy_list(self) -> None:
        text = "\n".join("\t".join(cells) for cells in self._rows_as_text())
        QApplication.clipboard().setText(text + "\n")
        self._show_status("The list of tags to tidy up was copied")

    def _save_list(self) -> None:
        start = str(Path.home() / "tags to tidy.csv")
        path, _ = QFileDialog.getSaveFileName(
            self.dialog, "Save list", start, "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as file:
                csv.writer(file).writerows(self._rows_as_text())
        except OSError as error:
            QMessageBox.warning(self.dialog, "Save list", f"The list could not be saved: {error}")
            return
        self._show_status(f"Saved the list to {path}")
