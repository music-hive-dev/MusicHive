"""MusicHive: application startup and UI wiring."""

import sys
from pathlib import Path

from PySide6.QtCore import QFile, QIODevice, QStandardPaths, Qt
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QListWidgetItem,
)

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


class MusicHive:
    """Connects the widgets from player.ui to the application logic."""

    def __init__(self) -> None:
        self.window = load_ui(UI_FILE)
        self.player = AudioPlayer()
        self.tracks: list[Path] = []

        self.window.chooseFolderButton.clicked.connect(self.choose_folder)
        self.window.libraryList.itemDoubleClicked.connect(self.play_item)
        self.player.track_started.connect(self.on_track_started)
        self.player.error_occurred.connect(self.on_player_error)

    def show(self) -> None:
        self.window.show()

    def choose_folder(self) -> None:
        start_dir = QStandardPaths.writableLocation(QStandardPaths.MusicLocation)
        folder = QFileDialog.getExistingDirectory(
            self.window, "Choose a music folder", start_dir
        )
        if not folder:  # the user cancelled the dialog
            return
        self.load_folder(Path(folder))

    def load_folder(self, folder: Path) -> None:
        self.window.libraryList.clear()
        self.tracks = []
        self.window.folderLabel.setText(str(folder))

        try:
            self.tracks = find_audio_files(folder)
        except OSError as error:
            self.window.statusBar().showMessage(f"Cannot read folder: {error}")
            return

        for track in self.tracks:
            item = QListWidgetItem(track.name)
            item.setData(Qt.UserRole, str(track))
            self.window.libraryList.addItem(item)

        if self.tracks:
            self.window.statusBar().showMessage(f"{len(self.tracks)} tracks found")
        else:
            self.window.statusBar().showMessage(
                "No supported audio files found in this folder"
            )

    def play_item(self, item: QListWidgetItem) -> None:
        """Called when the user double-clicks a song in the list."""
        self.player.play_file(item.data(Qt.UserRole))

    def on_track_started(self, track: Path) -> None:
        self.window.currentTrackLabel.setText(track.stem)
        self.window.statusBar().showMessage(f"Playing {track.name}")

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