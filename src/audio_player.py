"""Audio playback built on Qt Multimedia."""

from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

# Translate Qt's playback states into plain strings so the rest of the app
# does not need to know about Qt Multimedia enums.
_STATE_NAMES = {
    QMediaPlayer.PlaybackState.PlayingState: "playing",
    QMediaPlayer.PlaybackState.PausedState: "paused",
    QMediaPlayer.PlaybackState.StoppedState: "stopped",
}


class AudioPlayer(QObject):
    """Thin wrapper around QMediaPlayer/QAudioOutput.

    The rest of the app talks to this class and listens to its signals,
    so it never needs to touch Qt Multimedia directly.
    """

    track_started = Signal(object)  # the Path of the track now playing
    track_finished = Signal()  # the track reached its natural end
    state_changed = Signal(str)  # "playing", "paused" or "stopped"
    position_changed = Signal(int)  # playback position in milliseconds
    duration_changed = Signal(int)  # track length in milliseconds
    error_occurred = Signal(str)  # human-readable error message

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._audio_output.setVolume(0.7)

        self._current_track: Path | None = None

        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._player.errorOccurred.connect(self._on_error)

    # ----- state -----

    @property
    def has_track(self) -> bool:
        """True if a track is loaded (playing, paused or stopped)."""
        return self._current_track is not None

    @property
    def is_paused(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PausedState

    # ----- controls -----

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

    def play(self) -> None:
        """Resume a paused track, or restart a stopped one."""
        if self._current_track is not None:
            self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def stop(self) -> None:
        self._player.stop()

    def seek(self, position_ms: int) -> None:
        self._player.setPosition(position_ms)

    def set_volume(self, percent: int) -> None:
        """Set the volume from 0 to 100."""
        self._audio_output.setVolume(max(0, min(100, percent)) / 100)

    def unload(self) -> None:
        """Stop playback and forget the current track."""
        self._player.stop()
        self._player.setSource(QUrl())
        self._current_track = None

    def shutdown(self) -> None:
        """Release everything. Call before the app exits."""
        self.unload()

    # ----- internal slots -----
    def _on_position_changed(self, position_ms: int) -> None:
        self.position_changed.emit(int(position_ms))

    def _on_duration_changed(self, duration_ms: int) -> None:
        self.duration_changed.emit(int(duration_ms))

    def _on_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.state_changed.emit(_STATE_NAMES[state])

    def _on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.track_finished.emit()

    def _on_error(self, error: QMediaPlayer.Error, error_string: str) -> None:
        name = self._current_track.name if self._current_track else "track"
        self.error_occurred.emit(f"Cannot play {name}: {error_string}")