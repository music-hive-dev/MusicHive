"""Synchronising a library to a device: the options in the Devices tab, and the work.

Afterwards the device's music folder holds the library: songs are converted or
copied there, tags are brought up to date, and music that is not in the library
is deleted, after the user has confirmed exactly which files that means.
"""

import json
import os
import shutil
import tempfile
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThread, QThreadPool, Signal
from PySide6.QtWidgets import QListWidgetItem, QMessageBox, QWidget

from src.convert_files import (
    CONVERTED,
    FAILED,
    FORMATS,
    TAGS_UPDATED,
    UP_TO_DATE,
    ConvertSignals,
    ConvertSong,
    ConvertTask,
    ConvertWorker,
    device_path_key,
    make_tasks,
)
from src.music_library import SUPPORTED_EXTENSIONS
from src.number_field import NumberField
from src.organise_files import sanitise_name, template_problem
from src.track_metadata import find_folder_cover

# What can be deleted from the music folder. Anything else there is left alone.
AUDIO_EXTENSIONS = SUPPORTED_EXTENSIONS | {
    ".aac", ".aif", ".aiff", ".ape", ".m4a", ".m4b", ".mka", ".mp2", ".mp4", ".mpc", ".opus",
    ".wma", ".wv",
}  # fmt: skip
FREE_CORES = 2  # left alone so that the music and the window stay smooth
MAX_WORKERS = 8
MAX_FILES_LISTED = 10
SETTINGS_PREFIX = "deviceSync"


@dataclass
class SyncOptions:
    music_folder: str
    playlist_folder: str
    all_playlists: bool
    playlists: list[str] = field(default_factory=list)
    format_index: int = 0
    profile_index: int = 0
    format_subfolders: bool = False
    template: str = ""
    skip_missing: bool = True
    resize_art: bool = False
    art_size: int = 200


def device_subfolder(text: str) -> str | None:
    """A folder typed by the user, relative to the device. None for the device itself.

    The whole device is refused on purpose: extra files get deleted from the
    music folder, and the device's root holds its firmware and settings.
    """
    parts = [part for part in text.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def find_extra_files(music_root: Path, tasks: list[ConvertTask]) -> list[Path]:
    """Music in the folder that the library does not have: what a sync would delete.

    Hidden files and folders are never touched. Besides songs, this finds their
    .lrc lyrics, and the cover image of a folder that is losing all its songs.
    """
    wanted = [task.target.relative_to(music_root) for task in tasks]
    wanted_songs = {device_path_key(path.as_posix()) for path in wanted}
    wanted_stems = {device_path_key(path.with_suffix("").as_posix()) for path in wanted}
    wanted_folders = {device_path_key(path.parent.as_posix()) for path in wanted}

    extra: list[Path] = []
    for current, folders, files in os.walk(music_root):
        folders[:] = [name for name in folders if not name.startswith(".")]
        relative = Path(current).relative_to(music_root)
        losing_songs = False
        for name in files:
            if name.startswith("."):
                continue
            path, suffix = relative / name, Path(name).suffix.lower()
            if suffix in AUDIO_EXTENSIONS and device_path_key(path.as_posix()) not in wanted_songs:
                extra.append(Path(current) / name)
                losing_songs = True
            elif suffix == ".lrc":
                if device_path_key(path.with_suffix("").as_posix()) not in wanted_stems:
                    extra.append(Path(current) / name)
        if losing_songs and device_path_key(relative.as_posix()) not in wanted_folders:
            cover = find_folder_cover(current)
            if cover is not None:
                extra.append(Path(cover))
    return extra


def write_device_playlist(file: Path, entries: list[str]) -> None:
    """Write a playlist, swapped in only when complete. Raises OSError on failure."""
    temp = file.with_name(f".musichive-playlist-{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text("\n".join(["#EXTM3U", *entries]) + "\n", encoding="utf-8")
        os.replace(temp, file)
    finally:
        temp.unlink(missing_ok=True)


class _Signals(QObject):
    planned = Signal(object, str)  # the extra files, what went wrong
    deleted = Signal(int, int)  # files deleted, files that could not be


class _PlanTask(QRunnable):
    """Looks through the device, which can take a while on a slow one."""

    def __init__(self, music_root: Path, tasks: list[ConvertTask], signals: _Signals) -> None:
        super().__init__()
        self._music_root = music_root
        self._tasks = tasks
        self._signals = signals

    def run(self) -> None:
        try:
            self._signals.planned.emit(find_extra_files(self._music_root, self._tasks), "")
        except Exception as error:  # never let a thread die silently
            self._signals.planned.emit([], str(error))


class _DeleteTask(QRunnable):
    """Deletes the confirmed files, then the folders that leaves empty."""

    def __init__(
        self, music_root: Path, files: list[Path], cancel: threading.Event, signals: _Signals
    ) -> None:
        super().__init__()
        self._music_root = music_root
        self._files = files
        self._cancel = cancel
        self._signals = signals

    def run(self) -> None:
        deleted = failed = 0
        for file in self._files:
            if self._cancel.is_set():
                break
            try:
                file.unlink()
                deleted += 1
            except OSError:
                failed += 1
        for current, _folders, _files in os.walk(self._music_root, topdown=False):
            parts = Path(current).relative_to(self._music_root).parts
            if parts and not any(part.startswith(".") for part in parts):
                try:
                    os.rmdir(current)  # refuses a folder that still holds anything
                except OSError:
                    pass
        self._signals.deleted.emit(deleted, failed)


class DeviceSyncPanel(QObject):
    """Connects the sync options of the Devices tab and runs a synchronisation."""

    def __init__(
        self,
        window: QWidget,
        settings: QSettings,
        get_source: Callable[[], tuple[list[ConvertSong], dict[str, list[Path]]] | None],
        show_status: Callable[[str], None],
    ) -> None:
        super().__init__(window)
        self._window = w = window
        self._settings = settings
        self._get_source = get_source
        self._show_status = show_status
        self._folder = ""
        self._device_id = ""
        self._playlist_names: list[str] = []
        self._checked_playlists: set[str] = set()
        self._art_size = NumberField(w.syncArtSize)
        self._defaults = self._read_widgets()  # as set in player.ui

        self._running = False
        self._cancel = threading.Event()
        self._workers: set[ConvertWorker] = set()
        self._queue_tasks: list[ConvertTask] = []
        self._music_root = Path()
        self._playlist_folder = Path()
        self._playlists: dict[str, list[Path]] = {}
        self._left_out = self._deleted = self._delete_failed = self._done = 0
        self._counts: dict[str, int] = {}
        self._first_problem = ""
        self._device_lost = False
        self._work_dir: Path | None = None  # where songs are prepared, on the computer

        self._signals = _Signals(self)
        self._signals.planned.connect(self._on_planned)
        self._signals.deleted.connect(self._on_deleted)
        self._convert_signals = ConvertSignals(self)
        self._convert_signals.song_done.connect(self._on_song_done)
        self._convert_signals.worker_done.connect(self._on_worker_done)
        self._pool = QThreadPool(self)
        workers = min(MAX_WORKERS, QThread.idealThreadCount() - FREE_CORES)
        self._pool.setMaxThreadCount(max(1, workers))

        w.syncFormat.currentIndexChanged.connect(lambda _index: self._show_options())
        w.syncFormatSubfolders.toggled.connect(lambda _on: self._show_options())
        w.syncAllPlaylists.toggled.connect(lambda _on: self._show_options())
        w.syncResizeArt.toggled.connect(lambda _on: self._show_options())
        w.syncInsertTag.activated.connect(self._insert_tag)
        w.syncButton.clicked.connect(lambda _checked=False: self._start())
        w.syncStopButton.clicked.connect(lambda _checked=False: self._stop())
        w.syncStopButton.setEnabled(False)
        self._show_options()

    @property
    def is_running(self) -> bool:
        return self._running

    def shutdown(self) -> None:
        """The app is closing: remember the options and stop."""
        self._save_options()
        self._cancel.set()
        self._pool.waitForDone(5000)

    # ----- the options -----

    def set_device(self, folder: str, device_id: str) -> None:
        """Show the options of this device ("" means there is no device)."""
        if self._running:
            return
        self._window.syncPanel.setVisible(bool(folder))
        if (folder, device_id) == (self._folder, self._device_id):
            return
        self._save_options()
        self._folder, self._device_id = folder, device_id
        if folder:
            self._apply_options(self._load_options())

    def set_playlists(self, names: list[str]) -> None:
        """The open library's playlists changed."""
        self._remember_checked_playlists()
        self._playlist_names = names
        self._fill_playlists()

    def _fill_playlists(self) -> None:
        playlist_list = self._window.syncPlaylistList
        playlist_list.clear()
        for name in self._playlist_names:
            item = QListWidgetItem(name)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            checked = name in self._checked_playlists
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            playlist_list.addItem(item)

    def _remember_checked_playlists(self) -> None:
        playlist_list = self._window.syncPlaylistList
        for row in range(playlist_list.count()):
            item = playlist_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                self._checked_playlists.add(item.text())
            else:
                self._checked_playlists.discard(item.text())

    def _read_widgets(self) -> SyncOptions:
        w = self._window
        self._remember_checked_playlists()
        return SyncOptions(
            w.syncMusicFolder.text(),
            w.syncPlaylistFolder.text(),
            w.syncAllPlaylists.isChecked(),
            sorted(self._checked_playlists),
            w.syncFormat.currentIndex(),
            w.syncProfile.currentIndex(),
            w.syncFormatSubfolders.isChecked(),
            w.syncTemplate.text(),
            w.syncSkipMissing.isChecked(),
            w.syncResizeArt.isChecked(),
            self._art_size.value(),
        )

    def _apply_options(self, options: SyncOptions) -> None:
        w = self._window
        w.syncMusicFolder.setText(options.music_folder)
        w.syncPlaylistFolder.setText(options.playlist_folder)
        w.syncAllPlaylists.setChecked(options.all_playlists)
        w.syncFormat.setCurrentIndex(options.format_index)
        w.syncProfile.setCurrentIndex(options.profile_index)
        w.syncFormatSubfolders.setChecked(options.format_subfolders)
        w.syncTemplate.setText(options.template)
        w.syncSkipMissing.setChecked(options.skip_missing)
        w.syncResizeArt.setChecked(options.resize_art)
        self._art_size.setValue(options.art_size)
        self._checked_playlists = set(options.playlists)
        self._fill_playlists()
        self._show_options()

    def _settings_key(self) -> str:
        return f"{SETTINGS_PREFIX}/{self._device_id}"

    def _load_options(self) -> SyncOptions:
        try:
            data = json.loads(self._settings.value(self._settings_key(), "", type=str))
            return SyncOptions(**data)
        except (ValueError, TypeError):
            return self._defaults

    def _save_options(self) -> None:
        if self._device_id:
            self._settings.setValue(self._settings_key(), json.dumps(asdict(self._read_widgets())))

    def _show_options(self) -> None:
        w = self._window
        output_index = w.syncFormat.currentIndex() - 1
        has_profiles = output_index >= 0 and len(FORMATS[output_index].encoders) > 1
        w.syncProfile.setEnabled(has_profiles)
        w.syncProfileLabel.setEnabled(has_profiles)
        w.syncPlaylistList.setEnabled(not w.syncAllPlaylists.isChecked())
        formatted = w.syncFormatSubfolders.isChecked()
        w.syncTemplate.setEnabled(formatted)
        w.syncInsertTag.setEnabled(formatted)
        w.syncSkipMissing.setEnabled(formatted)
        w.syncArtSize.setEnabled(w.syncResizeArt.isChecked())

    def _insert_tag(self, index: int) -> None:
        w = self._window
        if index > 0:  # the first entry is only the combo box's caption
            w.syncTemplate.insert(w.syncInsertTag.itemText(index))
            w.syncTemplate.setFocus()
        w.syncInsertTag.setCurrentIndex(0)

    # ----- running -----

    def _report(self, text: str) -> None:
        self._window.syncStatus.setText(text)
        self._show_status(text)

    def _set_running(self, running: bool) -> None:
        w = self._window
        self._running = running
        w.syncOptions.setEnabled(not running)
        w.syncButton.setEnabled(not running)
        w.syncStopButton.setEnabled(running)
        w.deviceCombo.setEnabled(not running)
        if not running:
            w.syncProgress.hide()
            if self._work_dir is not None:
                shutil.rmtree(self._work_dir, ignore_errors=True)
                self._work_dir = None

    def _start(self) -> None:
        w = self._window
        music_folder = device_subfolder(w.syncMusicFolder.text())
        playlist_folder = device_subfolder(w.syncPlaylistFolder.text())
        if music_folder is None or playlist_folder is None:
            self._report("Enter a folder on the device for the music and for the playlists.")
            return
        template = w.syncTemplate.text() if w.syncFormatSubfolders.isChecked() else None
        problem = template_problem(template) if template is not None else None
        if problem:
            self._report(problem)
            return
        source = self._get_source()
        if source is None:
            return
        songs, self._playlists = source
        self._save_options()

        device = Path(self._folder)
        self._music_root = device / music_folder
        self._playlist_folder = device / playlist_folder
        output_index = w.syncFormat.currentIndex() - 1
        output = FORMATS[output_index] if output_index >= 0 else None
        encoder = ""
        if output is not None:
            encoder = output.encoders[min(w.syncProfile.currentIndex(), len(output.encoders) - 1)]
        self._work_dir = Path(tempfile.mkdtemp(prefix="musichive-sync-"))
        tasks, self._left_out = make_tasks(
            songs,
            self._music_root,
            output,
            encoder,
            template,
            w.syncSkipMissing.isChecked(),
            skip_existing=True,
            for_device=True,
            cover_side=self._art_size.value() if w.syncResizeArt.isChecked() else 0,
            device_lock=threading.Lock(),
            work_dir=self._work_dir,
        )
        if not tasks:  # an empty plan would call everything on the device extra
            self._set_running(False)
            self._report("There are no songs to send to the device.")
            return
        self._queue_tasks = tasks
        self._done = self._deleted = self._delete_failed = 0
        self._counts = {}
        self._first_problem = ""
        self._device_lost = False
        self._cancel = threading.Event()
        self._set_running(True)
        self._report("Comparing the device with the library…")
        self._pool.start(_PlanTask(self._music_root, tasks, self._signals))

    def _stop(self) -> None:
        self._cancel.set()
        self._window.syncStopButton.setEnabled(False)
        self._report("Stopping…")

    def _on_planned(self, extra: list[Path], error: str) -> None:
        if error:
            self._set_running(False)
            self._report(f"Could not read the device: {error}")
        elif self._cancel.is_set():
            self._set_running(False)
            self._report("Stopped.")
        elif not extra:
            self._copy_songs()
        else:
            self._confirm_deleting(extra)

    def _confirm_deleting(self, extra: list[Path]) -> None:
        """Nothing is ever deleted from a device without this question."""
        names = [path.relative_to(self._music_root).as_posix() for path in extra]
        listed = "\n".join(names[:MAX_FILES_LISTED])
        more = len(names) - MAX_FILES_LISTED
        if more > 0:
            listed += f"\n…and {more} more"
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            "Delete files from the device?",
            f"{len(names)} files in the device's music folder are not in this library and "
            f"would be deleted. This cannot be undone.\n\n{listed}",
            parent=self._window,
        )
        box.setDetailedText("\n".join(names))
        delete = box.addButton("Delete and Synchronise", QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("Synchronise Without Deleting", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is delete:
            self._report(f"Deleting {len(names)} files from the device…")
            self._pool.start(_DeleteTask(self._music_root, extra, self._cancel, self._signals))
        elif box.clickedButton() is keep:
            self._copy_songs()
        else:
            self._set_running(False)
            self._report("Synchronisation cancelled. Nothing was changed.")

    def _on_deleted(self, deleted: int, failed: int) -> None:
        self._deleted, self._delete_failed = deleted, failed
        if self._cancel.is_set():
            self._finish()
        else:
            self._copy_songs()

    def _copy_songs(self) -> None:
        w = self._window
        tasks = self._queue_tasks
        w.syncProgress.setRange(0, len(tasks))
        w.syncProgress.setValue(0)
        w.syncProgress.show()
        queue = deque(tasks)
        for _ in range(min(self._pool.maxThreadCount(), len(tasks))):
            worker = ConvertWorker(queue, self._cancel, self._convert_signals)
            self._workers.add(worker)
            self._pool.start(worker)

    def _on_song_done(self, outcome: str, name: str, error: str) -> None:
        self._done += 1
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        if error and not self._first_problem:
            self._first_problem = f"{name}: {error}"
        if outcome == FAILED and not os.path.ismount(self._folder):
            self._device_lost = True  # every remaining song would only fail too
            self._cancel.set()
        self._window.syncProgress.setValue(self._done)
        self._report(f"Synchronising {self._done} of {len(self._queue_tasks)}: {name}")

    def _on_worker_done(self, worker: ConvertWorker) -> None:
        self._workers.discard(worker)
        if not self._workers:
            self._finish()

    def _write_playlists(self) -> int:
        """Write the chosen playlists. Returns how many could not be written."""
        w = self._window
        self._remember_checked_playlists()
        names = (
            list(self._playlists)
            if w.syncAllPlaylists.isChecked()
            else [name for name in self._playlists if name in self._checked_playlists]
        )
        device = Path(self._folder)
        targets = {task.source: task.target for task in self._queue_tasks}
        failed = 0
        try:
            self._playlist_folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return len(names)
        for name in names:
            entries = [
                f"/{targets[song].relative_to(device).as_posix()}"
                for song in self._playlists[name]
                if song in targets
            ]
            try:
                write_device_playlist(
                    self._playlist_folder / f"{sanitise_name(name)}.m3u8", entries
                )
            except OSError:
                failed += 1
        return failed

    def _finish(self) -> None:
        counts = self._counts
        parts = [f"Copied {counts.get(CONVERTED, 0)}"]
        if counts.get(TAGS_UPDATED):
            parts.append(f"updated the tags of {counts[TAGS_UPDATED]}")
        if counts.get(UP_TO_DATE):
            parts.append(f"{counts[UP_TO_DATE]} already up to date")
        if self._deleted:
            parts.append(f"deleted {self._deleted}")
        if self._delete_failed:
            parts.append(f"{self._delete_failed} could not be deleted")
        if self._left_out:
            parts.append(f"{self._left_out} skipped for missing tags")
        if counts.get(FAILED):
            parts.append(f"{counts[FAILED]} failed")
        stopped = self._cancel.is_set()
        if not stopped:
            failed_playlists = self._write_playlists()
            if failed_playlists:
                parts.append(f"{failed_playlists} playlists could not be written")
        message = ", ".join(parts)
        if self._device_lost:
            message = f"The device disconnected, so the synchronisation stopped. {message}"
        elif stopped:
            message = f"Stopped. {message}"
        if self._first_problem:
            message += f"\nFirst problem: {self._first_problem}"
        self._set_running(False)
        self._report(message)
