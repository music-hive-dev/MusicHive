"""MusicHive: application startup and UI wiring."""

import sys
from pathlib import Path

from PySide6.QtCore import QFile, QIODevice, QStandardPaths
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import QApplication, QFileDialog, QListWidgetItem

from src.audio_player import AudioPlayer
from src.music_library import find_audio_files

UI_FILE = Path(__file__).parent / "player.ui"


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


class MusicHive:
    """Connects the widgets from player.ui to the application logic."""

    def __init__(self) -> None:
        self.window = load_ui(UI_FILE)
        self.player = AudioPlayer()
        self.tracks: list[Path] = []
        self.current_index = -1  # index into self.tracks, or -1 if none

        w = self.window

        # Library
        w.chooseFolderButton.clicked.connect(self.choose_folder)
        w.libraryList.itemDoubleClicked.connect(self.play_item)

        # Transport buttons
        w.playButton.clicked.connect(self.on_play)
        w.pauseButton.clicked.connect(self.player.pause)
        w.stopButton.clicked.connect(self.on_stop)
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

        self.on_state_changed("stopped")

    def show(self) -> None:
        self.window.show()

    # ----- library -----

    def choose_folder(self) -> None:
        start_dir = QStandardPaths.writableLocation(QStandardPaths.MusicLocation)
        folder = QFileDialog.getExistingDirectory(
            self.window, "Choose a music folder", start_dir
        )
        if not folder:  # the user cancelled the dialog
            return
        self.load_folder(Path(folder))

    def load_folder(self, folder: Path) -> None:
        # Playback stops when the folder changes, because the track list
        # that Next/Previous walk through is being replaced.
        self.player.unload()
        self.current_index = -1
        self.window.currentTrackLabel.setText("Nothing playing")
        self.window.libraryList.clear()
        self.tracks = []
        self.window.folderLabel.setText(str(folder))

        try:
            self.tracks = find_audio_files(folder)
        except OSError as error:
            self.window.statusBar().showMessage(f"Cannot read folder: {error}")
            return

        for track in self.tracks:
            self.window.libraryList.addItem(QListWidgetItem(track.name))

        if self.tracks:
            self.window.statusBar().showMessage(f"{len(self.tracks)} tracks found")
        else:
            self.window.statusBar().showMessage(
                "No supported audio files found in this folder"
            )

    # ----- choosing what to play -----

    def play_index(self, index: int) -> None:
        """Play the track at `index` in the list and highlight it."""
        if not 0 <= index < len(self.tracks):
            return
        self.current_index = index
        self.window.libraryList.setCurrentRow(index)
        self.player.play_file(self.tracks[index])

    def play_item(self, item: QListWidgetItem) -> None:
        """Called when the user double-clicks a song in the list."""
        self.play_index(self.window.libraryList.row(item))

    def on_play(self) -> None:
        if self.player.is_paused:
            self.player.play()
            return

        row = self.window.libraryList.currentRow()
        if row < 0 and self.tracks:
            row = 0
        if row < 0:
            self.window.statusBar().showMessage("Choose a folder with music first")
            return
        self.play_index(row)

    def on_stop(self) -> None:
        self.player.stop()
        self.window.statusBar().showMessage("Stopped")

    def play_next(self) -> None:
        if self.current_index + 1 < len(self.tracks):
            self.play_index(self.current_index + 1)
        else:
            self.window.statusBar().showMessage("This is the last track")

    def play_previous(self) -> None:
        if self.current_index >= 0:
            # On the first track this restarts it.
            self.play_index(max(0, self.current_index - 1))

    # ----- seek slider -----

    def on_slider_moved(self, value: int) -> None:
        """While dragging, only update the time label; seek on release."""
        self.window.currentTimeLabel.setText(format_time(value))

    def on_slider_released(self) -> None:
        self.player.seek(self.window.progressSlider.value())

    def on_slider_action(self, _action: int) -> None:
        """Handle clicks on the groove and keyboard changes (not drags)."""
        slider = self.window.progressSlider
        if not slider.isSliderDown():
            self.player.seek(slider.sliderPosition())

    # ----- player -> UI -----

    def on_track_started(self, track: Path) -> None:
        self.window.currentTrackLabel.setText(track.stem)

    def on_track_finished(self) -> None:
        if self.current_index + 1 < len(self.tracks):
            self.play_index(self.current_index + 1)
        else:
            self.window.statusBar().showMessage("Reached the end of the folder")

    def on_state_changed(self, state: str) -> None:
        w = self.window
        w.playButton.setEnabled(state != "playing")
        w.pauseButton.setEnabled(state == "playing")
        w.stopButton.setEnabled(state != "stopped")

        if state == "playing":
            w.statusBar().showMessage(f"Playing {w.currentTrackLabel.text()}")
        elif state == "paused":
            w.statusBar().showMessage("Paused")
        else:  # stopped
            w.progressSlider.setValue(0)
            w.currentTimeLabel.setText(format_time(0))

    def on_position_changed(self, position_ms: int) -> None:
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
        self.window.statusBar().showMessage(message)


def main() -> int:
    app = QApplication(sys.argv)
    music_hive = MusicHive()
    app.aboutToQuit.connect(music_hive.player.shutdown)
    music_hive.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())