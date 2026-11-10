"""Tools > Organise Files: move a library's songs to where a naming template says.

A template such as "<albumartist>/<album>/<trackno> - <title>" is filled in from
each song's tags. Only a "/" typed in the template makes a folder; one inside a
tag, as in "AC/DC", is replaced like any other character a file name cannot hold.
"""

import os
import re
import shutil
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QHeaderView, QTableWidgetItem, QWidget

from src.lastfm_dialog import load_dialog
from src.music_library import find_audio_files
from src.track_metadata import TrackInfo, find_folder_cover, forget_folder_covers

TAG_NAMES = ("title", "artist", "albumartist", "album", "trackno", "discno", "year", "genre")
UNKNOWN_VALUE = "Unknown"  # stands in for a tag a song does not have
EMPTY_NAME = "_"  # a folder or file name with nothing usable left in it

# Room is left below the usual limit of 255 bytes for the extension and a " (2)".
MAX_NAME_BYTES = 240
# Not allowed by at least one common file system, such as those on USB drives.
_ILLEGAL_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_TAG = re.compile(r"<([^<>]*)>")

PREVIEW_DELAY_MS = 250
MOVES_PER_BATCH = 50  # the window and the music get a turn between batches

TEMPLATE_KEY = "organise/template"
SKIP_MISSING_KEY = "organise/skipMissing"
REMOVE_EMPTY_KEY = "organise/removeEmpty"


@dataclass(frozen=True)
class FileMove:
    row: int  # the song's library row
    old: str  # relative to the library folder
    new: str  # relative to the library folder; "" if the song is skipped
    missing: tuple[str, ...] = ()  # the template's tags this song does not have


@dataclass
class Plan:
    rows: list[FileMove]  # the songs to move or skip, in library order
    unchanged: int  # songs already where the template puts them


@dataclass
class OrganiseResult:
    moved: list[FileMove]
    failures: list[tuple[str, str]]  # file, what went wrong
    stopped: bool  # the user closed the window before the end


def sanitise_name(text: str) -> str:
    """Make `text` safe to use as one file or folder name."""
    text = " ".join(unicodedata.normalize("NFC", text).split())
    text = _ILLEGAL_CHARACTERS.sub("_", text)
    # Going through bytes also drops anything that cannot be stored in a file name.
    text = text.encode("utf-8", "ignore")[:MAX_NAME_BYTES].decode("utf-8", "ignore")
    # A name starting with a dot would be hidden, and the library scan skips those.
    return text.strip(" .") or EMPTY_NAME


def template_problem(template: str) -> str | None:
    """Why this template cannot be used, or None if it can."""
    template = template.strip()
    if not template:
        return "Enter a naming template."
    if template.endswith("/"):
        return "The template must end with a file name, not a folder."
    unknown = [name for name in _TAG.findall(template) if name.lower() not in TAG_NAMES]
    if unknown:
        return f"Unknown tag: <{unknown[0]}>"
    if re.search("[<>]", _TAG.sub("", template)):
        return "A tag is missing its < or >."
    if not _TAG.search(template.rsplit("/", 1)[-1]):
        return "The file name needs at least one tag, or every song would get the same name."
    return None


def tag_values(info: TrackInfo, has_title: bool) -> dict[str, str]:
    """What each template tag stands for in this song ("" if the song does not have it)."""

    def number(value: int | None) -> str:
        return "" if value is None else str(value)

    return {
        # Without a title tag, info.title is only the name of the file.
        "title": info.title if has_title else "",
        "artist": info.artist,
        "albumartist": info.album_artist,
        "album": info.album,
        "trackno": number(info.track_number),
        "discno": number(info.disc_number),
        "year": info.year,
        "genre": info.genre,
    }


def template_parts(template: str) -> list[str]:
    """A template's folder levels and file name, in order."""
    return [part for part in template.strip().split("/") if part.strip()]


def template_tags(template: str) -> list[str]:
    """The tags a template uses, each once."""
    return list(dict.fromkeys(name.lower() for name in _TAG.findall(template)))


def render_stem(parts: list[str], values: dict[str, str], title_fallback: str) -> str:
    """A song's relative path under a template, without its extension."""

    def fill_in(match: re.Match) -> str:
        name = match[1].lower()
        if values[name].strip():
            return values[name]
        return title_fallback if name == "title" else UNKNOWN_VALUE

    return "/".join(sanitise_name(_TAG.sub(fill_in, part)) for part in parts)


def make_plan(
    template: str,
    root: Path,
    rel_paths: Sequence[str],
    infos: Sequence[TrackInfo],
    titled: set[str],
    skip_missing: bool,
) -> Plan:
    """Work out where each song goes. `titled` holds the songs that have a title tag.

    Every destination is free when this runs, so the moves can never depend on
    each other. Songs that would land on the same name get " (2)" and so on.
    """
    parts = template_parts(template)
    used = template_tags(template)
    taken = set(rel_paths)
    rows: list[FileMove] = []
    unchanged = 0

    for row, (old, info) in enumerate(zip(rel_paths, infos)):
        values = tag_values(info, old in titled)
        missing = tuple(name for name in used if not values[name].strip())
        if missing and skip_missing:
            rows.append(FileMove(row, old, "", missing))
            continue

        stem = render_stem(parts, values, info.title)
        extension = Path(old).suffix
        new = stem + extension
        number = 2
        while new != old and (new in taken or os.path.lexists(root / new)):
            new = f"{stem} ({number}){extension}"
            number += 1
        if new == old:
            unchanged += 1
        else:
            taken.add(new)
            rows.append(FileMove(row, old, new, missing))
    return Plan(rows, unchanged)


def move_song(root: Path, move: FileMove) -> None:
    """Move one song and its .lrc lyrics. Raises OSError if the song cannot be moved."""
    source, target = root / move.old, root / move.new
    if os.path.lexists(target):  # it appeared after the preview was made
        raise FileExistsError(f"{move.new} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(source, target)
    lyrics, new_lyrics = source.with_suffix(".lrc"), target.with_suffix(".lrc")
    if lyrics.exists() and not os.path.lexists(new_lyrics):
        try:
            shutil.move(lyrics, new_lyrics)
        except OSError:
            pass  # the song has moved; its lyrics file is still safe where it was


def _carry_cover(source: Path, targets: set[Path]) -> None:
    """Bring a folder's cover image along, if its songs all went to one new folder.

    Songs that were split over several folders may be different albums.
    """
    found = find_folder_cover(str(source))
    if found is None or len(targets) != 1 or source in targets:
        return
    cover, target = Path(found), next(iter(targets))
    try:
        if find_folder_cover(str(target)) is None:
            shutil.copy2(cover, target / cover.name)
            if not find_audio_files(source, recursive=False):
                cover.unlink()
    except OSError:
        pass  # a missing cover image is not worth reporting over the moved songs


def _remove_empty_folders(folder: Path, root: Path) -> None:
    """Remove this folder and the parents above it that are empty, up to the library."""
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()  # refuses a folder that still holds anything
        except OSError:
            return
        folder = folder.parent


def tidy_up(root: Path, moved: list[FileMove], remove_empty: bool) -> None:
    """After moving songs: bring cover images along and remove left-over folders."""
    destinations: dict[Path, set[Path]] = {}
    for move in moved:
        destinations.setdefault((root / move.old).parent, set()).add((root / move.new).parent)
    for source, targets in destinations.items():
        _carry_cover(source, targets)
    forget_folder_covers()
    if remove_empty:
        for source in destinations:
            _remove_empty_folders(source, root)


class OrganiseDialog:
    """Connects the widgets of organise_files.ui and carries out the moves.

    `on_moved` is called with each batch of moved songs. It returns False if the
    library could not be updated, which stops the moving.
    """

    def __init__(
        self,
        ui_path: Path,
        parent: QWidget,
        settings: QSettings,
        root: Path,
        rel_paths: Sequence[str],
        infos: Sequence[TrackInfo],
        titled: set[str],
        on_moved: Callable[[list[FileMove]], bool],
    ) -> None:
        self.dialog = d = load_dialog(ui_path, parent)
        self._settings = settings
        self._root = root
        self._rel_paths = rel_paths
        self._infos = infos
        self._titled = titled
        self._on_moved = on_moved
        self._plan: Plan | None = None
        self._stop = False
        self._result = OrganiseResult([], [], False)

        d.organiseTemplate.setText(
            settings.value(TEMPLATE_KEY, d.organiseTemplate.text(), type=str)
        )
        d.organiseSkipMissing.setChecked(
            settings.value(SKIP_MISSING_KEY, d.organiseSkipMissing.isChecked(), type=bool)
        )
        d.organiseRemoveEmpty.setChecked(
            settings.value(REMOVE_EMPTY_KEY, d.organiseRemoveEmpty.isChecked(), type=bool)
        )
        d.organisePreview.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        self._preview_timer = QTimer(d)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(PREVIEW_DELAY_MS)
        self._preview_timer.timeout.connect(self._refresh)
        d.organiseTemplate.textChanged.connect(lambda _text: self._preview_timer.start())
        d.organiseInsertTag.activated.connect(self._insert_tag)
        d.organiseSkipMissing.toggled.connect(lambda _on: self._refresh())
        d.organiseMoveButton.clicked.connect(lambda _checked=False: self._move_files())
        d.organiseButtonBox.rejected.connect(d.reject)
        d.rejected.connect(self._request_stop)
        self._refresh()

    def run(self) -> OrganiseResult:
        """Show the window. Returns what was moved."""
        d = self.dialog
        d.exec()
        self._preview_timer.stop()
        self._settings.setValue(TEMPLATE_KEY, d.organiseTemplate.text())
        self._settings.setValue(SKIP_MISSING_KEY, d.organiseSkipMissing.isChecked())
        self._settings.setValue(REMOVE_EMPTY_KEY, d.organiseRemoveEmpty.isChecked())
        d.deleteLater()
        return self._result

    def _request_stop(self) -> None:
        self._stop = True

    def _insert_tag(self, index: int) -> None:
        d = self.dialog
        if index > 0:  # the first entry is only the combo box's caption
            d.organiseTemplate.insert(d.organiseInsertTag.itemText(index))
            d.organiseTemplate.setFocus()
        d.organiseInsertTag.setCurrentIndex(0)

    def _refresh(self) -> None:
        """Work out the moves for the template as it is now, and show them."""
        d = self.dialog
        self._preview_timer.stop()
        template = d.organiseTemplate.text()
        problem = template_problem(template)
        self._plan = None
        if problem is None:
            self._plan = make_plan(
                template,
                self._root,
                self._rel_paths,
                self._infos,
                self._titled,
                d.organiseSkipMissing.isChecked(),
            )
        rows = self._plan.rows if self._plan else []
        self._show_rows(rows)
        moving = sum(1 for row in rows if row.new)
        d.organiseMoveButton.setEnabled(moving > 0)
        if self._plan is None:
            d.organiseSummary.setText(problem)
        else:
            d.organiseSummary.setText(
                f"{moving} to move, {len(rows) - moving} skipped, "
                f"{self._plan.unchanged} already in place"
            )

    def _show_rows(self, rows: list[FileMove]) -> None:
        table = self.dialog.organisePreview
        dim = table.palette().placeholderText()
        table.setUpdatesEnabled(False)
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            new = QTableWidgetItem(row.new or f"Skipped: no {', '.join(row.missing)}")
            if not row.new:
                new.setForeground(dim)
            table.setItem(index, 0, QTableWidgetItem(row.old))
            table.setItem(index, 1, new)
        table.setUpdatesEnabled(True)

    def _move_files(self) -> None:
        d = self.dialog
        self._refresh()  # the preview may be waiting for typing to stop
        if self._plan is None:
            return
        moves = [row for row in self._plan.rows if row.new]
        for widget in (
            d.organiseTemplate,
            d.organiseInsertTag,
            d.organiseSkipMissing,
            d.organiseRemoveEmpty,
            d.organiseMoveButton,
        ):
            widget.setEnabled(False)
        d.organiseProgress.setRange(0, len(moves))
        d.organiseProgress.setValue(0)
        d.organiseProgress.show()

        result = self._result
        for start in range(0, len(moves), MOVES_PER_BATCH):
            QApplication.processEvents()  # keeps the window alive; closing it stops the moving
            if self._stop:
                break
            moved: list[FileMove] = []
            for move in moves[start : start + MOVES_PER_BATCH]:
                try:
                    move_song(self._root, move)
                except OSError as error:
                    result.failures.append((move.old, str(error)))
                else:
                    moved.append(move)
            result.moved.extend(moved)
            d.organiseProgress.setValue(min(start + MOVES_PER_BATCH, len(moves)))
            if moved and not self._on_moved(moved):
                break

        result.stopped = len(result.moved) + len(result.failures) < len(moves)
        tidy_up(self._root, result.moved, d.organiseRemoveEmpty.isChecked())
        d.accept()
