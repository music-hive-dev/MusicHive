"""Tools > Convert Files: make copies of songs in another format, in another folder.

The originals are never changed. Each copy is written under a temporary name and
only takes its real name once it is complete, so a stopped or failed conversion
never leaves a half-finished song that would look already converted.
"""

import os
import shutil
import threading
import unicodedata
import uuid
from collections import deque
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from PySide6.QtCore import (  # noqa: E402
    QBuffer,
    QIODevice,
    QObject,
    QRunnable,
    QSettings,
    Qt,
    QThread,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QDialogButtonBox,
    QFileDialog,
    QWidget,
)

from src.lastfm_dialog import load_dialog  # noqa: E402
from src.organise_files import (  # noqa: E402
    render_stem,
    sanitise_name,
    tag_values,
    template_parts,
    template_problem,
    template_tags,
)
from src.tag_writer import edit_file_safely  # noqa: E402
from src.track_metadata import (  # noqa: E402
    EditableFile,
    TagChanges,
    TrackInfo,
    can_read_audio,
    find_folder_cover,
    read_editable_file,
    write_tag_changes,
)

Gst.init(None)


@dataclass(frozen=True)
class OutputFormat:
    extension: str
    encoders: tuple[str, ...]  # one per profile; a single one means there are no profiles


# In the order of the choices in convert_files.ui: small, standard, high, max.
FORMATS = (
    OutputFormat(
        ".mp3",
        (
            "lamemp3enc target=quality quality=6 ! xingmux",  # about 115 kbit/s
            "lamemp3enc target=quality quality=2 ! xingmux",  # about 190 kbit/s
            "lamemp3enc target=quality quality=0 ! xingmux",  # about 245 kbit/s
            "lamemp3enc target=bitrate bitrate=320 cbr=true ! xingmux",
        ),
    ),
    OutputFormat(
        ".ogg",
        (
            "vorbisenc quality=0.3 ! oggmux",  # about 112 kbit/s
            "vorbisenc quality=0.5 ! oggmux",  # about 160 kbit/s
            "vorbisenc quality=0.7 ! oggmux",  # about 224 kbit/s
            "vorbisenc quality=1.0 ! oggmux",  # about 500 kbit/s
        ),
    ),
    OutputFormat(
        ".opus",
        (
            "opusenc bitrate=96000 bitrate-type=vbr ! oggmux",
            "opusenc bitrate=128000 bitrate-type=vbr ! oggmux",
            "opusenc bitrate=192000 bitrate-type=vbr ! oggmux",
            "opusenc bitrate=256000 bitrate-type=vbr ! oggmux",
        ),
    ),
    OutputFormat(".flac", ("flacenc quality=8",)),
)

_WAIT_NS = 200 * Gst.MSECOND
FREE_CORES = 2  # left alone so that the music and the window stay smooth
MAX_WORKERS = 8
# Written by the encoders themselves, so a copy having them does not make it out of date.
_ENCODER_TAGS = {"encoder", "encodersettings", "encoderoptions"}

CONVERTED, TAGS_UPDATED, UP_TO_DATE, FAILED = "converted", "updated", "current", "failed"
_OUTCOME_TEXT = {
    CONVERTED: "Converted",
    TAGS_UPDATED: "Updated tags of",
    UP_TO_DATE: "Already up to date:",
    FAILED: "Could not convert",
}

FORMAT_KEY = "convert/format"
PROFILE_KEY = "convert/profile"
FOLDER_KEY = "convert/folder"
FORMAT_SUBFOLDERS_KEY = "convert/formatSubfolders"
TEMPLATE_KEY = "convert/template"
SKIP_MISSING_KEY = "convert/skipMissing"
SKIP_EXISTING_KEY = "convert/skipExisting"


@dataclass(frozen=True)
class ConvertSong:
    source: Path
    rel_path: str  # relative to the library folder
    info: TrackInfo
    has_title: bool  # the file really has a title tag


@dataclass(frozen=True)
class ConvertTask:
    source: Path
    target: Path
    encoder: str  # "" copies the file as it is
    skip_existing: bool
    cover_side: int = 0  # embedded artwork is shrunk to this many pixels; 0 leaves it
    # For a device: songs are prepared in `work_dir`, and only one at a time may touch it.
    device_lock: threading.Lock | None = None
    work_dir: Path | None = None


def device_path_key(path: str) -> str:
    """A path as a device's FAT storage sees it: case and accent encoding do not count."""
    return unicodedata.normalize("NFC", path).casefold()


def make_tasks(
    songs: Sequence[ConvertSong],
    export_folder: Path,
    output: OutputFormat | None,
    encoder: str,
    template: str | None,
    skip_missing: bool,
    skip_existing: bool,
    for_device: bool = False,
    cover_side: int = 0,
    device_lock: threading.Lock | None = None,
    work_dir: Path | None = None,
) -> tuple[list[ConvertTask], int]:
    """What to convert, and how many songs were left out for missing tags.

    With no `template`, songs keep the folders they have in the library. With no
    `output`, they keep their format. `for_device` also copies songs already in
    the target format instead of encoding them again, and makes names safe for FAT.
    """
    key = device_path_key if for_device else str
    parts = template_parts(template) if template else []
    used = template_tags(template) if template else []
    tasks: list[ConvertTask] = []
    taken: set[str] = set()
    left_out = 0
    for song in songs:
        if template:
            values = tag_values(song.info, song.has_title)
            if skip_missing and any(not values[name].strip() for name in used):
                left_out += 1
                continue
            stem = render_stem(parts, values, song.info.title)
        else:
            stem = song.rel_path.removesuffix(Path(song.rel_path).suffix)
            if for_device:
                stem = "/".join(sanitise_name(part) for part in stem.split("/"))
        extension = output.extension if output is not None else Path(song.rel_path).suffix
        name = stem + extension
        number = 2
        while key(name) in taken:  # always the same outcome, so a later run finds its files
            name = f"{stem} ({number}){extension}"
            number += 1
        taken.add(key(name))
        copy = output is None or (for_device and song.source.suffix.lower() == extension)
        tasks.append(
            ConvertTask(
                song.source,
                export_folder / name,
                "" if copy else encoder,
                skip_existing,
                cover_side,
                device_lock,
                work_dir,
            )
        )
    return tasks, left_out


def shrink_cover(data: bytes, side: int) -> bytes:
    """Artwork no larger than `side` pixels. Smaller or unreadable artwork is left alone."""
    image = QImage.fromData(data)
    if image.isNull() or max(image.width(), image.height()) <= side:
        return data
    scaled = image.scaled(
        side,
        side,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    return bytes(buffer.data()) if scaled.save(buffer, "JPEG", 90) else data


def _encode(source: Path, temp: Path, encoder: str, cancel: threading.Event) -> str | None:
    """Convert one song. Returns what went wrong, or None if it worked."""
    chain = f"audioconvert ! audioresample ! {encoder} ! filesink name=out"
    pipeline = None
    try:
        output = Gst.parse_bin_from_description(chain, True)
        output.get_by_name("out").set_property("location", str(temp))
        pipeline = Gst.parse_launch("playbin flags=audio")
        pipeline.set_property("audio-sink", output)
        pipeline.set_property("uri", source.as_uri())
        bus = pipeline.get_bus()
        pipeline.set_state(Gst.State.PLAYING)
        finished = Gst.MessageType.EOS | Gst.MessageType.ERROR
        while not cancel.is_set():
            message = bus.timed_pop_filtered(_WAIT_NS, finished)
            if message is None:
                continue
            if message.type == Gst.MessageType.ERROR:
                return message.parse_error()[0].message
            return None
        return "stopped"
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)  # this also finishes writing the file


def _tag_differences(wanted: EditableFile, current: EditableFile) -> TagChanges:
    """What must change in `current` for its tags and cover to match `wanted`."""
    tags = {name: value for name, value in wanted.tags.items() if current.tags.get(name) != value}
    tags.update(
        (name, "")
        for name in current.tags
        if name not in wanted.tags and name not in _ENCODER_TAGS
    )
    changes = TagChanges(tags=tags)
    if wanted.cover != current.cover:
        changes.cover = wanted.cover
        changes.cover_changed = True
    if wanted.rating != current.rating:
        changes.rating = wanted.rating
    return changes


def _copy_files_beside(source: Path, target: Path) -> None:
    """Bring a song's lyrics file along, and its folder's cover image."""
    try:
        lyrics, new_lyrics = source.with_suffix(".lrc"), target.with_suffix(".lrc")
        if lyrics.exists() and (
            not new_lyrics.exists() or lyrics.read_bytes() != new_lyrics.read_bytes()
        ):
            shutil.copy2(lyrics, new_lyrics)
        cover = find_folder_cover(str(source.parent))
        if cover is not None and find_folder_cover(str(target.parent)) is None:
            shutil.copy2(cover, target.parent / Path(cover).name)
    except OSError:
        pass  # the song itself is safely converted


def _put_in_place(temp: Path, target: Path) -> None:
    """Give a finished song its real name, which only happens once it is complete."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if temp.parent == target.parent:
        os.replace(temp, target)
        return
    partial = target.with_name(f".musichive-convert-{uuid.uuid4().hex}{target.suffix}")
    try:
        shutil.copyfile(temp, partial)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)


def convert_song(task: ConvertTask, cancel: threading.Event) -> tuple[str, str]:
    """Do one song. Returns (outcome, what went wrong)."""
    source, target = task.source, task.target
    try:
        if target.resolve() == source.resolve():
            return FAILED, "the export would replace the original"
        wanted = read_editable_file(source)
        if task.cover_side and wanted.cover:
            wanted.cover = shrink_cover(wanted.cover, task.cover_side)
        # A device is reached one song at a time: small players can't take more.
        device_access = task.device_lock or nullcontext()
        with device_access:
            # An empty or damaged copy has no tags to update, so it is made again below.
            if task.skip_existing and target.exists() and can_read_audio(target):
                changes = _tag_differences(wanted, read_editable_file(target))
                _copy_files_beside(source, target)
                if changes.is_empty:
                    return UP_TO_DATE, ""
                edit_file_safely(target, lambda temp: write_tag_changes(temp, changes))
                return TAGS_UPDATED, ""

        temp_folder = task.work_dir or target.parent
        temp_folder.mkdir(parents=True, exist_ok=True)
        # Hidden, and short enough that a long song name still fits.
        temp = temp_folder / f".musichive-convert-{uuid.uuid4().hex}{target.suffix}"
        try:
            if task.encoder:
                error = _encode(source, temp, task.encoder, cancel)
                if error:
                    return FAILED, error
            else:
                shutil.copyfile(source, temp)
            changes = _tag_differences(wanted, read_editable_file(temp))
            if not changes.is_empty:
                write_tag_changes(temp, changes)
            with device_access:
                _put_in_place(temp, target)
                _copy_files_beside(source, target)
        finally:
            temp.unlink(missing_ok=True)
        return CONVERTED, ""
    except Exception as error:  # one bad song must not stop the others
        return FAILED, str(error)


class ConvertSignals(QObject):
    song_done = Signal(str, str, str)  # outcome, song name, what went wrong
    worker_done = Signal(object)  # the ConvertWorker


class ConvertWorker(QRunnable):
    """Converts songs from the shared list until there are none left."""

    def __init__(self, tasks: deque, cancel: threading.Event, signals: ConvertSignals) -> None:
        super().__init__()
        self.setAutoDelete(False)  # Python keeps ownership of this object
        self._tasks = tasks
        self._cancel = cancel
        self._signals = signals

    def run(self) -> None:
        try:
            while not self._cancel.is_set():
                try:
                    task = self._tasks.popleft()
                except IndexError:
                    break
                outcome, error = convert_song(task, self._cancel)
                if not self._cancel.is_set():
                    self._signals.song_done.emit(outcome, task.source.name, error)
        finally:
            self._signals.worker_done.emit(self)


class ConvertFilesTool(QObject):
    """Connects the widgets of convert_files.ui and runs the conversion."""

    def __init__(self, ui_path: Path, parent: QWidget, settings: QSettings, show_status) -> None:
        super().__init__(parent)
        self.dialog = d = load_dialog(ui_path, parent)
        self._settings = settings
        self._show_status = show_status
        self._all: list[ConvertSong] = []
        self._selected: list[ConvertSong] = []
        self._cancel = threading.Event()
        self._workers: set[ConvertWorker] = set()
        self._total = self._done = self._left_out = 0
        self._counts: dict[str, int] = {}
        self._first_problem = ""

        self._signals = ConvertSignals(self)
        self._signals.song_done.connect(self._on_song_done)
        self._signals.worker_done.connect(self._on_worker_done)
        self._pool = QThreadPool(self)

        d.convertFormat.setCurrentIndex(
            settings.value(FORMAT_KEY, d.convertFormat.currentIndex(), type=int)
        )
        d.convertProfile.setCurrentIndex(
            settings.value(PROFILE_KEY, d.convertProfile.currentIndex(), type=int)
        )
        d.convertFolder.setText(settings.value(FOLDER_KEY, "", type=str))
        d.convertFormatSubfolders.setChecked(
            settings.value(FORMAT_SUBFOLDERS_KEY, d.convertFormatSubfolders.isChecked(), type=bool)
        )
        d.convertTemplate.setText(settings.value(TEMPLATE_KEY, d.convertTemplate.text(), type=str))
        d.convertSkipMissing.setChecked(
            settings.value(SKIP_MISSING_KEY, d.convertSkipMissing.isChecked(), type=bool)
        )
        d.convertSkipExisting.setChecked(
            settings.value(SKIP_EXISTING_KEY, d.convertSkipExisting.isChecked(), type=bool)
        )

        d.convertFormat.currentIndexChanged.connect(lambda _index: self._show_options())
        d.convertFormatSubfolders.toggled.connect(lambda _on: self._show_options())
        d.convertInsertTag.activated.connect(self._insert_tag)
        d.convertBrowseButton.clicked.connect(lambda _checked=False: self._browse())
        d.convertStartButton.clicked.connect(lambda _checked=False: self._start())
        d.convertStopButton.clicked.connect(lambda _checked=False: self._stop())
        d.convertButtonBox.rejected.connect(d.reject)
        # Enter in a text field must not press Close.
        d.convertButtonBox.button(QDialogButtonBox.StandardButton.Close).setAutoDefault(False)
        self._show_idle()

    @property
    def is_running(self) -> bool:
        return bool(self._workers)

    def show(self, songs: list[ConvertSong], selected: list[ConvertSong]) -> None:
        d = self.dialog
        if not self.is_running:
            self._all, self._selected = songs, selected
            d.convertScope.setCurrentIndex(1 if selected else 0)
            d.convertStatus.setText(f"{len(songs)} songs in this library, {len(selected)} selected.")
        d.show()
        d.raise_()
        d.activateWindow()

    def shutdown(self) -> None:
        """The app is closing: stop, leaving no half-finished files behind."""
        self._cancel.set()
        self._pool.waitForDone(5000)

    # ----- the window -----

    def _show_options(self) -> None:
        d = self.dialog
        lossy = len(FORMATS[d.convertFormat.currentIndex()].encoders) > 1
        d.convertProfile.setEnabled(lossy and not self.is_running)
        d.convertProfileLabel.setEnabled(lossy)
        formatted = d.convertFormatSubfolders.isChecked() and not self.is_running
        d.convertTemplate.setEnabled(formatted)
        d.convertInsertTag.setEnabled(formatted)
        d.convertSkipMissing.setEnabled(formatted)

    def _set_controls_enabled(self, enabled: bool) -> None:
        d = self.dialog
        for widget in (
            d.convertScope,
            d.convertFormat,
            d.convertFolder,
            d.convertBrowseButton,
            d.convertFormatSubfolders,
            d.convertSkipExisting,
            d.convertStartButton,
        ):
            widget.setEnabled(enabled)
        d.convertStopButton.setEnabled(not enabled)
        self._show_options()

    def _show_idle(self) -> None:
        self._set_controls_enabled(True)
        self.dialog.convertProgress.hide()

    def _insert_tag(self, index: int) -> None:
        d = self.dialog
        if index > 0:  # the first entry is only the combo box's caption
            d.convertTemplate.insert(d.convertInsertTag.itemText(index))
            d.convertTemplate.setFocus()
        d.convertInsertTag.setCurrentIndex(0)

    def _browse(self) -> None:
        d = self.dialog
        folder = QFileDialog.getExistingDirectory(d, "Export to", d.convertFolder.text())
        if folder:
            d.convertFolder.setText(folder)

    def _save_settings(self) -> None:
        d, settings = self.dialog, self._settings
        settings.setValue(FORMAT_KEY, d.convertFormat.currentIndex())
        settings.setValue(PROFILE_KEY, d.convertProfile.currentIndex())
        settings.setValue(FOLDER_KEY, d.convertFolder.text())
        settings.setValue(FORMAT_SUBFOLDERS_KEY, d.convertFormatSubfolders.isChecked())
        settings.setValue(TEMPLATE_KEY, d.convertTemplate.text())
        settings.setValue(SKIP_MISSING_KEY, d.convertSkipMissing.isChecked())
        settings.setValue(SKIP_EXISTING_KEY, d.convertSkipExisting.isChecked())

    # ----- running -----

    def _start(self) -> None:
        d = self.dialog
        folder = d.convertFolder.text().strip()
        if not folder:
            d.convertStatus.setText("Choose a folder to export to.")
            return
        songs = self._selected if d.convertScope.currentIndex() == 1 else self._all
        if not songs:
            d.convertStatus.setText("There are no songs to convert. Select some first.")
            return
        template = d.convertTemplate.text() if d.convertFormatSubfolders.isChecked() else None
        problem = template_problem(template) if template is not None else None
        if problem:
            d.convertStatus.setText(problem)
            return
        self._save_settings()

        output = FORMATS[d.convertFormat.currentIndex()]
        encoder = output.encoders[min(d.convertProfile.currentIndex(), len(output.encoders) - 1)]
        tasks, self._left_out = make_tasks(
            songs,
            Path(folder),
            output,
            encoder,
            template,
            d.convertSkipMissing.isChecked(),
            d.convertSkipExisting.isChecked(),
        )
        self._total, self._done = len(tasks), 0
        self._counts = {}
        self._first_problem = ""
        self._cancel = threading.Event()
        if not tasks:
            self._finish()
            return

        d.convertProgress.setRange(0, self._total)
        d.convertProgress.setValue(0)
        d.convertProgress.show()
        queue = deque(tasks)
        count = min(MAX_WORKERS, max(1, QThread.idealThreadCount() - FREE_CORES), len(tasks))
        self._pool.setMaxThreadCount(count)
        for _ in range(count):
            worker = ConvertWorker(queue, self._cancel, self._signals)
            self._workers.add(worker)
            self._pool.start(worker)
        self._set_controls_enabled(False)
        self._report(f"Converting {self._total} songs…")

    def _stop(self) -> None:
        self._cancel.set()
        self.dialog.convertStopButton.setEnabled(False)
        self._report("Stopping…")

    def _report(self, text: str) -> None:
        self.dialog.convertStatus.setText(text)
        self._show_status(text)

    def _on_song_done(self, outcome: str, name: str, error: str) -> None:
        self._done += 1
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        if error and not self._first_problem:
            self._first_problem = f"{name}: {error}"
        self.dialog.convertProgress.setValue(self._done)
        self._report(f"{self._done} of {self._total}. {_OUTCOME_TEXT[outcome]} {name}")

    def _on_worker_done(self, worker: ConvertWorker) -> None:
        self._workers.discard(worker)
        if not self._workers:
            self._finish()

    def _finish(self) -> None:
        counts = self._counts
        parts = [f"Converted {counts.get(CONVERTED, 0)}"]
        if counts.get(TAGS_UPDATED):
            parts.append(f"updated the tags of {counts[TAGS_UPDATED]}")
        if counts.get(UP_TO_DATE):
            parts.append(f"{counts[UP_TO_DATE]} already up to date")
        if self._left_out:
            parts.append(f"{self._left_out} skipped for missing tags")
        if counts.get(FAILED):
            parts.append(f"{counts[FAILED]} failed")
        message = ", ".join(parts)
        if self._cancel.is_set():
            message = f"Stopped. {message}"
        if self._first_problem:
            message += f"\nFirst problem: {self._first_problem}"
        self._show_idle()
        self._report(message)
