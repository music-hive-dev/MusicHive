"""Audio playback built on Qt Multimedia."""

from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer


class AudioPlayer(QObject):
    """Thin wrapper around QMediaPlayer/QAudioOutput.

    The rest of the app talks to this class and listens to its signals,
    so it never needs to touch Qt Multimedia directly.
    """

    track_started = Signal(object)  # emits the Path of the track now playing
    error_occurred = Signal(str)  # emits a human-readable error message

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._audio_output.setVolume(0.7)  # matches the slider's initial value of 70

        self._current_track: Path | None = None
        self._player.errorOccurred.connect(self._on_error)

    def play_file(self, path: str | Path) -> bool:
        """Start playing `path`, replacing whatever is playing now.

        Returns False (and emits error_occurred) if the file is missing.
        """
        track = Path(path)
        if not track.is_file():
            self.error_occurred.emit(f"File not found: {track.name}")
            return False

        self._player.stop()
        self._current_track = track
        self._player.setSource(QUrl.fromLocalFile(str(track)))
        self._player.play()
        self.track_started.emit(track)
        return True

    def shutdown(self) -> None:
        """Stop playback and release the current file. Call before the app exits."""
        self._player.stop()
        self._player.setSource(QUrl())

    def _on_error(self, error: QMediaPlayer.Error, error_string: str) -> None:
        name = self._current_track.name if self._current_track else "track"
        self.error_occurred.emit(f"Cannot play {name}: {error_string}")