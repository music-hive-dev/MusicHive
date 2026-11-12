"""The sleep timer: playback fades out and pauses after a while."""

import time

from PySide6.QtCore import QObject, QTimer, Signal

FADE_KEY = "sleepTimer/fadeSeconds"
DEFAULT_FADE_SECONDS = 30
TICK_MS = 250  # often enough for a smooth fade


class SleepTimer(QObject):
    """Counts down; in the last seconds it says how loud to play, then it is done."""

    volume_factor = Signal(float)  # 1.0 down to 0.0 while fading out
    expired = Signal()
    remaining_changed = Signal(int)  # whole minutes left (rounded up), or -1 when off

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._deadline = 0.0
        self._fade_seconds = DEFAULT_FADE_SECONDS
        self._last_minutes = -1
        self._fading = False
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    def start(self, minutes: float, fade_seconds: int) -> None:
        self._deadline = time.monotonic() + minutes * 60
        self._fade_seconds = max(0, fade_seconds)
        self._last_minutes = -1
        self._timer.start()
        self._tick()

    def cancel(self) -> None:
        was_fading = self._fading
        self._timer.stop()
        self._fading = False
        if was_fading:
            self.volume_factor.emit(1.0)  # back to full volume
        self._report(-1)

    def _tick(self) -> None:
        left = self._deadline - time.monotonic()
        if left <= 0:
            self._timer.stop()
            self._fading = False
            self._report(-1)
            self.expired.emit()
            return
        if self._fade_seconds and left <= self._fade_seconds:
            self._fading = True
            self.volume_factor.emit(left / self._fade_seconds)
        self._report(int(-(-left // 60)))  # minutes, rounded up

    def _report(self, minutes: int) -> None:
        if minutes != self._last_minutes:
            self._last_minutes = minutes
            self.remaining_changed.emit(minutes)
