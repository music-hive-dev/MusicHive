"""MusicHive: application startup and UI wiring."""

import sqlite3
import sys
from pathlib import Path

from PySide6.QtCore import (
    QFile,
    QIODevice,
    QModelIndex,
    QSettings,
    QStandardPaths,
    Qt,
    QThreadPool,
)
from PySide6.QtGui import QIcon
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSlider,
    QApplication,
    QFileDialog,
    QMessageBox,
    QProxyStyle,
    QStyle,
)

from src import library_db
from src.audio_player import AudioPlayer
from src.library_db import LibraryInfo, SavedSession
from src.library_scanner import ScanSignals, ScanTask
from src.play_order import PlayOrder
from src.track_list import QueueModel, TrackDelegate, TrackListModel

UI_FILE = Path(__file__).parent / "player.ui"
ASSETS_DIR = Path(__file__).parent / "assets"

# If the track is further in than this, Previous restarts it instead of
# going back to the previous track.
PREVIOUS_RESTART_THRESHOLD_MS = 5000

REPEAT_STATUS = {
    "off": "Repeat off",
    "all": "Repeat all tracks",
    "one": "Repeat current track",
}


def load_ui(path: Path):
    """Load a Qt Designer .ui file and return the window it describes."""
    ui_file = QFile(str(path))
    if not ui_file.open(QIODevice.ReadOnly):
        raise RuntimeError(f"Cannot open UI file {path}: {ui_file.errorString()}")
    try:
        loader = QUiLoader()
        window = loader.load(ui_file)
    finally:
        ui_file.close()
    if window is None:
        raise RuntimeError(f"Failed to load {path}: {loader.errorString()}")
    return window


def format_time(milliseconds: int) -> str:
    """Format a time in milliseconds as m:ss (or h:mm:ss for long tracks)."""
    total_seconds = max(0, milliseconds) // 1000
    hours, rest = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def asset_icon(name: str) -> QIcon:
    """An icon from the assets folder: asset_icon("play") loads assets/play.svg."""
    path = ASSETS_DIR / f"{name}.svg"
    icon = QIcon(str(path))
    if icon.isNull():
        print(f"MusicHive: missing or unreadable icon: {path}", file=sys.stderr)
    return icon


def setup_track_view(view, model) -> None:
    """Give a list view a model and the cover-and-two-lines row painter."""
    view.setModel(model)
    view.setItemDelegate(TrackDelegate(view))
    view.setUniformItemSizes(True)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)


class JumpToClickStyle(QProxyStyle):
    """Makes a left click on a slider's groove jump straight to that position."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_Slider_AbsoluteSetButtons:
            return Qt.MouseButton.LeftButton.value
        return super().styleHint(hint, option, widget, returnData)


class MusicHive:
    """Connects the widgets from player.ui to the application logic."""

    def __init__(self) -> None:
        self.window = load_ui(UI_FILE)
        self.player = AudioPlayer()
        self.settings = QSettings("MusicHive", "MusicHive")

        # Libraries
        self.libraries: list[LibraryInfo] = []
        self.current_library: LibraryInfo | None = None  # chosen in the drop-down
        self.loaded_library: LibraryInfo | None = None  # whose tracks are shown
        self.tracks: list[Path] = []  # same order as the rows of the library list
        self.current_index = -1  # library row of the playing track, or -1 if none

        # Which track plays next (normal order, shuffle, repeat)
        self.play_order = PlayOrder()

        # Playback
        self.playback_state = "stopped"  # "playing", "paused" or "stopped"
        self.position_ms = 0  # last reported playback position

        # Background scanning (one scan at a time)
        self.scan_pool = QThreadPool(self.window)
        self.scan_pool.setMaxThreadCount(1)
        self.scan_signals = ScanSignals(self.window)
        self.scan_task: ScanTask | None = None
        self.scan_library: LibraryInfo | None = None

        w = self.window


        # Icons that change while the app runs. (The rest are set in Designer.)
        self.icon_play = asset_icon("play")
        self.icon_pause = asset_icon("pause")
        self.repeat_icons = {
            "off": asset_icon("repeat-off"),
            "all": asset_icon("repeat"),
            "one": asset_icon("repeat-1"),
        }

        # Left-click on a slider bar jumps to that position (instead of nudging).
        self._slider_style = JumpToClickStyle()
        w.progressSlider.setStyle(self._slider_style)
        w.volumeSlider.setStyle(self._slider_style)

        # Library list: a model holds the tracks, a delegate paints the rows.
        self.library_model = TrackListModel(w)
        setup_track_view(w.libraryList, self.library_model)
        w.libraryList.doubleClicked.connect(self.play_item)

        # Upcoming tracks: the playing track first, then the tracks that will
        # follow it in play order. It reads its data from the library model.
        self.queue_model = QueueModel(self.library_model, w)
        setup_track_view(w.upcomingList, self.queue_model)
        w.upcomingList.doubleClicked.connect(self.play_upcoming_item)
        w.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, w.upcomingDock)
        w.menuView.addAction(w.upcomingDock.toggleViewAction())

        # Shuffle and repeat buttons (settings are remembered between sessions)
        w.shuffleButton.setCheckable(True)
        w.repeatButton.setCheckable(True)

        shuffle_on = self.settings.value("shuffle", False, type=bool)
        w.shuffleButton.setChecked(shuffle_on)
        self.play_order.set_shuffle(shuffle_on)
        w.shuffleButton.toggled.connect(self.on_shuffle_toggled)

        self.play_order.set_repeat(self.settings.value("repeat", "off", type=str))
        self.update_repeat_button()
        w.repeatButton.clicked.connect(self.cycle_repeat)

        # Library selector and buttons
        w.libraryCombo.setPlaceholderText("No library yet")
        w.libraryCombo.activated.connect(self.on_library_chosen)
        w.addLibraryButton.clicked.connect(self.add_library)
        w.rescanButton.clicked.connect(self.rescan)

        # Menu actions (created in Qt Designer)
        w.actionAddLibrary.triggered.connect(self.add_library)
        w.actionRescanLibrary.triggered.connect(self.rescan)
        w.actionRemoveLibrary.triggered.connect(self.remove_library)
        w.actionQuit.triggered.connect(w.close)
        w.actionPlayPause.triggered.connect(self.on_play_pause)
        w.actionPrevious.triggered.connect(self.play_previous)
        w.actionNext.triggered.connect(self.play_next)
        w.actionAbout.triggered.connect(self.show_about)

        # Transport buttons
        w.playButton.clicked.connect(self.on_play_pause)
        w.previousButton.clicked.connect(self.play_previous)
        w.nextButton.clicked.connect(self.play_next)

        # Seek slider (user interaction)
        w.progressSlider.sliderMoved.connect(self.on_slider_moved)
        w.progressSlider.sliderReleased.connect(self.on_slider_released)
        w.progressSlider.actionTriggered.connect(self.on_slider_action)

        # Volume slider
        w.volumeSlider.valueChanged.connect(self.player.set_volume)
        self.player.set_volume(w.volumeSlider.value())

        # Player -> UI
        self.player.track_started.connect(self.on_track_started)
        self.player.track_finished.connect(self.on_track_finished)
        self.player.state_changed.connect(self.on_state_changed)
        self.player.position_changed.connect(self.on_position_changed)
        self.player.duration_changed.connect(self.on_duration_changed)
        self.player.error_occurred.connect(self.on_player_error)

        # Scanner -> UI
        self.scan_signals.progress.connect(self.on_scan_progress)
        self.scan_signals.finished.connect(self.on_scan_finished)
        self.scan_signals.failed.connect(self.on_scan_failed)
        self.scan_signals.cancelled.connect(self.on_scan_cancelled)

        self.on_state_changed("stopped")
        self.restore_libraries()

    def show(self) -> None:
        self.window.show()

    def shutdown(self) -> None:
        """Save the session, stop background work and playback. Called on quit."""
        if self.scan_task is not None:
            self.scan_task.cancel()
        self.scan_pool.waitForDone()  # a scan must be finished before we save
        self.save_session()
        self.player.shutdown()

    def show_status(self, text: str) -> None:
        self.window.statusBar().showMessage(text)

    def show_about(self) -> None:
        QMessageBox.about(
            self.window,
            "About MusicHive",
            "<b>MusicHive</b><br>A simple local music player built with "
            "PySide6 and Qt Multimedia.",
        )

    # ----- play order, shuffle and repeat -----

    def refresh_upcoming(self) -> None:
        """Show the playing track and the ones after it in the Upcoming list."""
        self.queue_model.set_rows(self.play_order.upcoming_rows())
        if self.queue_model.rowCount() > 0:
            # The playing track is always the first row of the Upcoming list.
            self.window.upcomingList.setCurrentIndex(self.queue_model.index(0, 0))
            self.window.upcomingList.scrollToTop()

    def reset_play_order(self) -> None:
        """Nothing is playing: start a fresh play order for the current tracks."""
        self.current_index = -1
        self.play_order.reset(len(self.tracks))
        self.refresh_upcoming()

    def on_shuffle_toggled(self, enabled: bool) -> None:
        self.play_order.set_shuffle(enabled)
        self.settings.setValue("shuffle", enabled)
        self.refresh_upcoming()
        self.show_status("Shuffle on" if enabled else "Shuffle off")

    def cycle_repeat(self) -> None:
        """Repeat button: off -> all -> one -> off."""
        modes = PlayOrder.REPEAT_MODES
        mode = modes[(modes.index(self.play_order.repeat) + 1) % len(modes)]
        self.play_order.set_repeat(mode)
        self.settings.setValue("repeat", mode)
        self.update_repeat_button()
        self.show_status(REPEAT_STATUS[mode])

    def update_repeat_button(self) -> None:
        """Make the repeat button show the current mode."""
        button = self.window.repeatButton
        mode = self.play_order.repeat
        button.setChecked(mode != "off")
        button.setIcon(self.repeat_icons[mode])
        button.setToolTip(REPEAT_STATUS[mode])

    # ----- saving and restoring the session -----

    def relative_path(self, path: Path) -> str:
        """A track's path relative to the library folder, as stored in the database."""
        return path.relative_to(self.loaded_library.root).as_posix()

    def save_session(self) -> None:
        """Remember the playing track, its position and the play order."""
        library = self.loaded_library
        if library is None:
            return
        try:
            if 0 <= self.current_index < len(self.tracks):
                order, order_position = self.play_order.snapshot()
                library_db.save_session(
                    library.db_path,
                    SavedSession(
                        rel_path=self.relative_path(self.tracks[self.current_index]),
                        position_ms=self.position_ms,
                        order=order,
                        order_position=order_position,
                    ),
                )
            else:
                library_db.clear_session(library.db_path)
        except (sqlite3.Error, OSError):
            pass  # failing to save must never get in the way of quitting

    def restore_session(self, info: LibraryInfo) -> bool:
        """Put back what was playing when the app last quit, paused.

        Returns False if there is nothing usable to restore.
        """
        try:
            session = library_db.load_session(info.db_path)
        except sqlite3.Error:
            return False
        if session is None or not self.tracks:
            return False

        # Normal case: the saved order still fits this library exactly.
        row = -1
        if self.play_order.restore(session.order, session.order_position):
            row = self.play_order.current_row
            if self.relative_path(self.tracks[row]) != session.rel_path:
                row = -1

        # Otherwise the library changed since then: keep the song if it still
        # exists, and rebuild the order from it.
        if row < 0:
            self.play_order.reset(len(self.tracks))
            row = next(
                (
                    index
                    for index, path in enumerate(self.tracks)
                    if self.relative_path(path) == session.rel_path
                ),
                -1,
            )
            if row < 0:
                return False
            self.play_order.start_at(row)

        self.current_index = row
        self.position_ms = session.position_ms
        model_index = self.library_model.index(row)
        self.window.libraryList.setCurrentIndex(model_index)
        self.window.libraryList.scrollTo(model_index)
        self.refresh_upcoming()

        if not self.player.load_file(self.tracks[row], session.position_ms):
            self.reset_play_order()  # the song file is gone
            return False
        return True

    # ----- libraries -----

    def restore_libraries(self) -> None:
        """At startup: fill the drop-down and reopen the last used library."""
        library_db.migrate_old_libraries()
        libraries = library_db.list_libraries()
        ids = [library.id for library in libraries]
        last = self.settings.value("lastLibrary", "", type=str)
        select = last if last in ids else (ids[0] if ids else None)

        self.refresh_libraries(select)
        if self.current_library is not None:
            self.open_library(self.current_library, restore=True)
        else:
            self.show_status("Add a library to get started (File → Add Library…)")

    def refresh_libraries(self, select_id: str | None = None) -> None:
        """Re-read the saved libraries and rebuild the drop-down."""
        self.libraries = library_db.list_libraries()
        combo = self.window.libraryCombo
        combo.clear()
        for index, library in enumerate(self.libraries):
            combo.addItem(library.name, library.id)
            combo.setItemData(index, str(library.root), Qt.ItemDataRole.ToolTipRole)

        selected = next(
            (i for i, library in enumerate(self.libraries) if library.id == select_id),
            -1,
        )
        combo.setCurrentIndex(selected)
        self.current_library = self.libraries[selected] if selected >= 0 else None
        self.update_controls()

    def on_library_chosen(self, combo_index: int) -> None:
        """The user picked a library from the drop-down."""
        library_id = self.window.libraryCombo.itemData(combo_index)
        info = next((l for l in self.libraries if l.id == library_id), None)
        if info is not None:
            self.open_library(info)

    def open_library(self, info: LibraryInfo, restore: bool = False) -> None:
        """Show a library's saved tracks (scanning first if it never was).

        With `restore`, also put back the session saved when the app last quit.
        """
        self.player.unload()
        self.window.currentTrackLabel.setText("Nothing playing")
        self.current_library = info
        self.loaded_library = None

        try:
            loaded = library_db.load_library(info)
        except sqlite3.Error as error:
            self.tracks = []
            self.library_model.clear()
            self.reset_play_order()
            self.update_controls()
            self.show_status(f"Cannot read library '{info.name}': {error}")
            return

        self.loaded_library = info
        self.tracks = loaded.paths
        self.library_model.set_library(loaded.paths, loaded.infos, loaded.covers)
        self.reset_play_order()
        self.settings.setValue("lastLibrary", info.id)
        self.update_controls()

        if info.scanned_at is None:
            self.start_scan(info)
        elif not (restore and self.restore_session(info)):
            self.show_status(self.describe(info, len(self.tracks)))

    def reload_current_library(self) -> None:
        """Re-read the current library after a scan, without stopping playback."""
        info = self.current_library
        if info is None:
            return
        try:
            loaded = library_db.load_library(info)
        except sqlite3.Error as error:
            self.show_status(f"Cannot read library '{info.name}': {error}")
            return

        playing = (
            self.tracks[self.current_index]
            if 0 <= self.current_index < len(self.tracks)
            else None
        )
        self.loaded_library = info
        self.tracks = loaded.paths
        self.library_model.set_library(loaded.paths, loaded.infos, loaded.covers)
        self.play_order.reset(len(self.tracks))

        # If the playing track is still there, rebuild the order from it so
        # Next/Previous keep working; otherwise nothing is "playing" any more.
        if playing in self.tracks:
            row = self.tracks.index(playing)
            self.play_order.start_at(row)
            self.current_index = row
            model_index = self.library_model.index(row)
            self.window.libraryList.setCurrentIndex(model_index)
            self.window.libraryList.scrollTo(model_index)
        else:
            self.current_index = -1
        self.refresh_upcoming()

    def add_library(self) -> None:
        start_dir = QStandardPaths.writableLocation(QStandardPaths.MusicLocation)
        folder = QFileDialog.getExistingDirectory(
            self.window, "Choose a music folder to add as a library", start_dir
        )
        if not folder:  # the user cancelled the dialog
            return

        try:
            info, created = library_db.add_library(Path(folder))
        except (OSError, sqlite3.Error) as error:
            QMessageBox.warning(
                self.window,
                "Cannot add library",
                f"The library could not be created:\n{error}",
            )
            return

        self.refresh_libraries(select_id=info.id)
        self.open_library(info)  # starts the first scan if it was never scanned
        if not created:
            self.show_status(f"'{info.name}' is already in your libraries")

    def remove_library(self) -> None:
        info = self.current_library
        if info is None:
            return
        answer = QMessageBox.question(
            self.window,
            "Remove library",
            f"Remove the library '{info.name}'?\n\n"
            "This only deletes MusicHive's saved data for it. "
            "Your music files are not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            library_db.remove_library(info)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot remove library", str(error))
            return

        self.player.unload()
        self.window.currentTrackLabel.setText("Nothing playing")
        self.loaded_library = None  # its database is gone; nothing to save at quit
        self.tracks = []
        self.library_model.clear()
        self.reset_play_order()
        self.settings.remove("lastLibrary")
        self.refresh_libraries(select_id=None)
        self.show_status(f"Removed library '{info.name}'")

    def describe(self, info: LibraryInfo, track_count: int) -> str:
        scanned = info.scanned_at.replace("T", " ") if info.scanned_at else "never"
        return f"{info.name}: {track_count} tracks · last scanned {scanned}"

    def update_controls(self) -> None:
        """Enable or disable the library controls to match the current state."""
        w = self.window
        scanning = self.scan_task is not None
        has_library = self.current_library is not None

        w.libraryCombo.setEnabled(not scanning and bool(self.libraries))
        w.addLibraryButton.setEnabled(not scanning)
        w.actionAddLibrary.setEnabled(not scanning)
        w.rescanButton.setEnabled(has_library and not scanning)
        w.actionRescanLibrary.setEnabled(has_library and not scanning)
        w.actionRemoveLibrary.setEnabled(has_library and not scanning)

    # ----- scanning -----

    def rescan(self) -> None:
        if self.current_library is None:
            self.show_status("Add a library first (File → Add Library…)")
            return
        self.start_scan(self.current_library)

    def start_scan(self, info: LibraryInfo) -> None:
        if self.scan_task is not None:
            return  # one scan at a time
        self.scan_library = info
        self.scan_task = ScanTask(info, self.scan_signals)
        self.update_controls()
        self.show_status(f"Scanning '{info.name}'…")
        self.scan_pool.start(self.scan_task)

    def on_scan_progress(self, library_id: str, done: int, total: int) -> None:
        name = self.scan_library.name if self.scan_library else "library"
        self.show_status(f"Scanning '{name}': {done} of {total} tracks")

    def on_scan_finished(self, library_id: str, count: int) -> None:
        name = self.scan_library.name if self.scan_library else "library"
        self.scan_task = None
        self.scan_library = None

        current_id = self.current_library.id if self.current_library else None
        self.refresh_libraries(select_id=current_id)  # also refreshes the saved info
        if self.current_library is not None and self.current_library.id == library_id:
            self.reload_current_library()

        if count:
            self.show_status(f"Scan complete: {count} tracks in '{name}'")
        else:
            self.show_status(f"Scan complete: no supported audio files in '{name}'")

    def on_scan_failed(self, library_id: str, message: str) -> None:
        self.scan_task = None
        self.scan_library = None
        self.update_controls()
        self.show_status("Scan failed")
        QMessageBox.warning(self.window, "Scan failed", message)

    def on_scan_cancelled(self, library_id: str) -> None:
        self.scan_task = None
        self.scan_library = None
        self.update_controls()

    # ----- choosing what to play -----

    def play_index(self, row: int) -> None:
        """Play library row `row`. The play order must already point at it."""
        if not 0 <= row < len(self.tracks):
            return
        self.current_index = row
        self.position_ms = 0
        model_index = self.library_model.index(row)
        self.window.libraryList.setCurrentIndex(model_index)
        self.window.libraryList.scrollTo(model_index)
        self.refresh_upcoming()
        self.player.play_file(self.tracks[row])

    def play_item(self, model_index: QModelIndex) -> None:
        """Called when the user double-clicks a song in the library list."""
        row = model_index.row()
        self.play_order.start_at(row)  # a fresh order, reshuffled if shuffle is on
        self.play_index(row)

    def play_upcoming_item(self, model_index: QModelIndex) -> None:
        """Called when the user double-clicks a song in the Upcoming list."""
        row = self.play_order.jump_to_upcoming(model_index.row())
        if row >= 0:
            self.play_index(row)

    def on_play_pause(self) -> None:
        """One button for both: pause while playing, resume while paused,
        otherwise start the highlighted track (or the first one)."""
        if self.playback_state == "playing":
            self.player.pause()
        elif self.playback_state == "paused":
            self.player.play()
        else:
            selected = self.window.libraryList.currentIndex()
            row = selected.row() if selected.isValid() else -1
            if row < 0 and self.tracks:
                row = 0
            if row < 0:
                self.show_status("Choose a library with music first")
                return
            self.play_order.start_at(row)
            self.play_index(row)

    def play_next(self) -> None:
        if self.current_index < 0:
            if self.tracks:  # nothing playing yet: start from the top
                self.play_order.start_at(0)
                self.play_index(0)
            return
        row = self.play_order.next()
        if row is None:
            self.show_status("This is the last track")
        else:
            self.play_index(row)

    def play_previous(self) -> None:
        """Restart the track if it is well underway, otherwise go back one."""
        if self.current_index < 0:
            return
        if self.position_ms > PREVIOUS_RESTART_THRESHOLD_MS:
            self.player.seek(0)  # keeps the current play/pause state
            return
        row = self.play_order.previous()
        if row is None:  # already at the start of the play order
            self.player.seek(0)
        else:
            self.play_index(row)

    # ----- seek slider -----

    def on_slider_moved(self, value: int) -> None:
        """While dragging, only update the time label; seek on release."""
        self.window.currentTimeLabel.setText(format_time(value))

    def on_slider_released(self) -> None:
        self.player.seek(self.window.progressSlider.value())

    def on_slider_action(self, action: int) -> None:
        """Handle keyboard and wheel changes (arrows, PageUp/Down, Home/End).

        Slider-move actions come from clicks and drags; those are handled
        once, on release, by on_slider_released.
        """
        if action == QAbstractSlider.SliderAction.SliderMove.value:
            return
        slider = self.window.progressSlider
        if not slider.isSliderDown():
            self.player.seek(slider.sliderPosition())

    # ----- player -> UI -----

    def on_track_started(self, track: Path) -> None:
        self.window.currentTrackLabel.setText(track.stem)

    def on_track_finished(self) -> None:
        row = self.play_order.next(ended_naturally=True)
        if row is None:
            self.show_status("Reached the end of the library")
        else:
            self.play_index(row)

    def on_state_changed(self, state: str) -> None:
        self.playback_state = state
        w = self.window

        # The single transport button shows what clicking it will do.
        if state == "playing":
            w.playButton.setIcon(self.icon_pause)
            w.playButton.setToolTip("Pause")
            self.show_status(f"Playing {w.currentTrackLabel.text()}")
        else:
            w.playButton.setIcon(self.icon_play)
            w.playButton.setToolTip("Play")
            if state == "paused":
                self.show_status("Paused")
            else:  # stopped
                self.position_ms = 0
                w.progressSlider.setValue(0)
                w.currentTimeLabel.setText(format_time(0))

    def on_position_changed(self, position_ms: int) -> None:
        self.position_ms = position_ms
        slider = self.window.progressSlider
        if slider.isSliderDown():  # the user is dragging; don't fight them
            return
        slider.setValue(position_ms)
        self.window.currentTimeLabel.setText(format_time(position_ms))

    def on_duration_changed(self, duration_ms: int) -> None:
        self.window.progressSlider.setRange(0, duration_ms)
        self.window.durationLabel.setText(format_time(duration_ms))

    def on_player_error(self, message: str) -> None:
        self.window.currentTrackLabel.setText("Nothing playing")
        self.show_status(message)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("MusicHive")
    app.setDesktopFileName("musichive")  # must match musichive.desktop
    music_hive = MusicHive()
    app.aboutToQuit.connect(music_hive.shutdown)
    music_hive.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())