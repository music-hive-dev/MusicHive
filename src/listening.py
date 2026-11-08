"""Deciding when a play counts, by Last.fm's scrobbling rules.

No GUI code lives here.
"""

import time

MIN_TRACK_SECONDS = 1  # shorter tracks never count
MAX_THRESHOLD_SECONDS = 240  # at most four minutes of listening are needed


class ListenTracker:
    """Measures how long the current track has actually been listened to.

    Pausing stops the clock and seeking does not change it, so jumping around
    in a song is not listening. A play counts once the listening time reaches
    half the track's length or four minutes, whichever comes first.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forget the current play: nothing is being measured."""
        self.duration_s = 0.0
        self.started_at: int | None = None  # Unix time the track first started playing
        self._listened = 0.0
        self._playing_since: float | None = None
        self._counted = True

    def begin(self, duration_ms: int, playing: bool, now: float | None = None) -> None:
        """A track was loaded (or restarted): start measuring from zero."""
        now = time.monotonic() if now is None else now
        self.duration_s = duration_ms / 1000
        self.started_at = None
        self._listened = 0.0
        self._playing_since = None
        self._counted = False
        if playing:
            self.set_playing(True, now)

    def set_duration(self, duration_ms: int) -> None:
        self.duration_s = duration_ms / 1000

    def set_playing(self, playing: bool, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if playing and self._playing_since is None:
            self._playing_since = now
            if self.started_at is None:
                self.started_at = int(time.time())
        elif not playing and self._playing_since is not None:
            self._listened += now - self._playing_since
            self._playing_since = None

    def listened_seconds(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        total = self._listened
        if self._playing_since is not None:
            total += now - self._playing_since
        return total

    def threshold_seconds(self) -> float | None:
        """How long the track must be listened to, or None if it can never count."""
        if self.duration_s < MIN_TRACK_SECONDS:
            return None
        return min(self.duration_s / 2, MAX_THRESHOLD_SECONDS)

    def check(self, now: float | None = None) -> bool:
        """True, once per play, when the listening time has reached the threshold."""
        if self._counted:
            return False
        threshold = self.threshold_seconds()
        if threshold is None or self.listened_seconds(now) < threshold:
            return False
        self._counted = True
        return True