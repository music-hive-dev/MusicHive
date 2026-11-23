"""Audio playback built on GStreamer, with an equaliser in the signal path.

There are two identical "decks", each a complete player. Normally one is in use;
during a crossfade the outgoing song keeps playing on one while the next starts
on the other, and the sound system mixes them.
"""

import math
import threading
import time
from array import array
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import GLib, Gst, GstApp  # noqa: E402, F401  (GstApp adds try_pull_sample)

from PySide6.QtCore import (  # noqa: E402
    QByteArray,
    QObject,
    QRunnable,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtMultimedia import QAudioBuffer, QAudioFormat  # noqa: E402

from src.cast import (  # noqa: E402
    CastDevice,
    CastError,
    CastMedia,
    CastSession,
    MediaServer,
    audio_type,
    open_session,
)
from src.equaliser import BAND_FREQUENCIES, BAND_OCTAVES, MAX_BANDS, EqSettings  # noqa: E402
from src.sound_settings import SoundSettings  # noqa: E402
from src.track_metadata import read_track_gain  # noqa: E402

Gst.init(None)
GLib.set_application_name("MusicHive")  # the name shown in the system's volume mixer

# Short pieces of sound let a volume change take effect smoothly and quickly.
_SHORT_BUFFERS = (
    "! audiobuffersplit output-buffer-duration=1/50 "
    if Gst.ElementFactory.find("audiobuffersplit")
    else ""
)
_VISUAL_RATE = 44100  # samples per second of the visualiser's copy of the sound
_VISUAL_HELD_MAX = 256  # pieces of sound waiting for their turn (about 5 seconds)
_VISUAL_LATE_NS = 200 * 1_000_000  # pieces this far behind the position are skipped
_VISUAL_AHEAD_NS = 5 * 1_000_000_000  # sound this far ahead is from before a seek back

# Decoded sound runs through this, in floating point so that boosts cannot clip
# on the way. Mono and surround files are converted to stereo.
_FILTER_CHAIN = (
    "audioconvert ! audio/x-raw,format=F32LE,channels=2,layout=interleaved "
    f"{_SHORT_BUFFERS}"
    "! volume name=preamp "
    f"! equalizer-nbands name=equalizer num-bands={MAX_BANDS} "
    "! audiopanorama name=balance method=simple"
)
# One branch goes to the speakers. The other is a copy for the visualiser, which
# drops what nobody collects so that it can never hold up the sound. The speaker
# queue is kept short (100 ms) because sound in it already has the old equaliser
# settings applied. The visualiser's copy arrives as soon as it is decoded
# (sync=false), ahead of the speakers; AudioPlayer holds each piece back until the
# playing position reaches it, so the bars match what is heard, sound system
# delay included. async=false: that sink takes sound at once, so it holds none when
# the song pauses, and pausing must not wait for it to get some (it never would,
# as the paused speakers hold up the sound for both branches).
_OUTPUT_CHAIN = (
    "tee name=split "
    "split. ! queue max-size-time=100000000 ! volume name=trackgain "
    "! volume name=fade ! volume name=master "
    "! audioconvert ! audioresample ! autoaudiosink "
    "split. ! queue leaky=downstream max-size-buffers=64 ! audioconvert ! audioresample "
    f"! audio/x-raw,format=S16LE,rate={_VISUAL_RATE},channels=1 "
    "! appsink name=samples sync=false async=false max-buffers=64 drop=true"
)
_SCAN_RATE = 16000  # samples per second; enough to tell silence from sound
_SCAN_CHAIN = (
    "audioconvert ! audioresample "
    f"! audio/x-raw,format=F32LE,channels=1,rate={_SCAN_RATE} "
    "! appsink name=sink sync=false max-buffers=32"
)

_TICK_MS = 33  # how often to look for news from GStreamer and for visualiser sound
_TICKS_PER_POSITION = 3  # the position is reported on every third look
_BUS_MESSAGES = (
    Gst.MessageType.EOS
    | Gst.MessageType.ERROR
    | Gst.MessageType.ASYNC_DONE
    | Gst.MessageType.DURATION_CHANGED
    | Gst.MessageType.TAG
)
RADIO_URI = "appsrc://"  # the player's sound comes from a RadioStream

PAUSE_FADE_SECONDS = 0.5
# Sound already handed to the speakers still has to play out after a fade ends.
OUTPUT_SETTLE_SECONDS = 0.25
SILENCE_PEAK = 0.001  # about -60 dB; anything quieter counts as silence
MIN_SILENCE_AT_START_MS = 100  # less than this is not worth a seek
MIN_SILENCE_AT_END_MS = 500  # less than this is left as part of the song
_SCAN_WAIT_NS = 200 * Gst.MSECOND


class _ScanSignals(QObject):
    head_found = Signal(object, int)  # the track, where its sound begins in ms
    finished = Signal(object)  # the _SilenceScan


class _SilenceScan(QRunnable):
    """Decodes a whole track as fast as it can to find where its sound starts and ends.

    Where the sound starts is reported as soon as it is known, because the
    track waits for that before it begins to play.
    """

    def __init__(self, track: Path, signals: _ScanSignals) -> None:
        super().__init__()
        self.setAutoDelete(False)  # Python keeps ownership of this object
        self.track = track
        self.head_ms: int | None = None  # None if the track is silent all the way
        self.tail_ms: int | None = None
        self._signals = signals
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        pipeline = None
        try:
            output = Gst.parse_bin_from_description(_SCAN_CHAIN, True)
            sink = output.get_by_name("sink")
            pipeline = Gst.parse_launch("playbin flags=audio")
            pipeline.set_property("audio-sink", output)
            pipeline.set_property("uri", self.track.as_uri())
            bus = pipeline.get_bus()
            pipeline.set_state(Gst.State.PLAYING)
            finished = Gst.MessageType.EOS | Gst.MessageType.ERROR
            while not self.cancelled:
                sample = sink.try_pull_sample(_SCAN_WAIT_NS)
                if sample is not None:
                    self._look_at(sample)
                elif bus.timed_pop_filtered(20 * Gst.MSECOND, finished) is not None:
                    break
        except Exception as error:  # a track that cannot be scanned just plays in full
            print(f"MusicHive: cannot scan {self.track.name}: {error}")
        finally:
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)
            self._signals.finished.emit(self)

    def _look_at(self, sample) -> None:
        buffer = sample.get_buffer()
        data = buffer.extract_dup(0, buffer.get_size())
        values = array("f")
        values.frombytes(data[: len(data) - len(data) % values.itemsize])
        if not values or (max(values) < SILENCE_PEAK and min(values) > -SILENCE_PEAK):
            return
        # Whole buffers are kept, so that no sound at either end can be cut off.
        start_ms = buffer.pts // Gst.MSECOND
        self.tail_ms = start_ms + len(values) * 1000 // _SCAN_RATE
        if self.head_ms is None:
            self.head_ms = start_ms
            self._signals.head_found.emit(self.track, start_ms)


@dataclass
class _Fade:
    start: float  # time.monotonic()
    seconds: float
    from_gain: float
    rising: bool
    hold_seconds: float  # how long to wait after the fade before calling `done`
    done: Callable[[], None] | None


class _Deck:
    """One complete player: a pipeline, its equaliser and its volume controls."""

    def __init__(self) -> None:
        filters = Gst.parse_bin_from_description(_FILTER_CHAIN, True)
        output = Gst.parse_bin_from_description(_OUTPUT_CHAIN, True)
        self._preamp = filters.get_by_name("preamp")
        self._equalizer = filters.get_by_name("equalizer")
        self._balance = filters.get_by_name("balance")
        self._track_gain = output.get_by_name("trackgain")
        self._fader = output.get_by_name("fade")
        self._master = output.get_by_name("master")
        self.samples = output.get_by_name("samples")

        # "flags=audio": embedded cover art must not open a video window.
        self.pipeline = Gst.parse_launch("playbin flags=audio")
        self.pipeline.set_property("audio-filter", filters)
        self.pipeline.set_property("audio-sink", output)
        self.bus = self.pipeline.get_bus()
        self._master.set_property("volume", 0.7)
        self.radio_stream = None  # the RadioStream that feeds this deck, while it plays radio
        self.pipeline.connect("source-setup", self._on_source_setup)

        self.track: Path | None = None
        self.ready = False  # the track is loaded far enough to seek in it
        self.pending_seek: int | None = None  # where to jump to once it is ready
        self.duration_ms = 0
        self.tail_ms: int | None = None  # where the last sound ends, once known
        self.ending_announced = False  # track_ending was sent for this track
        self._gain = 1.0
        self._fade: _Fade | None = None

    # ----- transport -----

    def _on_source_setup(self, _playbin, source) -> None:
        """GStreamer made the source; for radio, the RadioStream starts feeding it."""
        stream = self.radio_stream
        factory = source.get_factory()
        if stream is not None and factory is not None and factory.get_name() == "appsrc":
            stream.attach(source)

    def open(self, track: Path | str, position_ms: int | None) -> None:
        """Open a file, or a web address (str) to stream."""
        self.halt(Gst.State.READY)
        if track != RADIO_URI:
            self.radio_stream = None
        self.track = track
        self.pending_seek = position_ms
        self.duration_ms = 0
        self.tail_ms = None
        self.ending_announced = False
        self.set_gain(1.0)
        self.pipeline.set_property("uri", track if isinstance(track, str) else track.as_uri())

    def set_playing(self) -> None:
        self.pipeline.set_state(Gst.State.PLAYING)

    def set_paused(self) -> None:
        self.pipeline.set_state(Gst.State.PAUSED)

    def halt(self, level: Gst.State) -> None:
        self._fade = None
        self.ready = False
        self.pipeline.set_state(level)

    def restart_at(self, position_ms: int) -> None:
        """Go back to not playing, ready to start from this position.

        Seeking in a track that is already running leaves GStreamer stuck in
        this pipeline, whereas a seek right as a track starts works.
        """
        self.halt(Gst.State.READY)
        self.pending_seek = position_ms
        self.ending_announced = False

    def on_ready(self) -> None:
        self.ready = True
        self.read_duration()
        if self.pending_seek:
            position = self.pending_seek
            self.pending_seek = None
            self.pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                position * Gst.MSECOND,
            )

    def read_duration(self) -> None:
        found, duration = self.pipeline.query_duration(Gst.Format.TIME)
        if found:
            self.duration_ms = duration // Gst.MSECOND

    def position_ms(self) -> int | None:
        found, position = self.pipeline.query_position(Gst.Format.TIME)
        return position // Gst.MSECOND if found else None

    # ----- volume -----

    def set_volume(self, volume: float) -> None:
        self._master.set_property("volume", volume)

    def set_track_gain(self, gain_db: float) -> None:
        """Level this song, from its own tag. The element goes up to 10 times (+20 dB)."""
        self._track_gain.set_property("volume", min(10 ** (gain_db / 20), 10.0))

    def set_muted(self, muted: bool) -> None:
        self._master.set_property("mute", muted)

    def set_gain(self, gain: float) -> None:
        self._gain = gain
        self._fader.set_property("volume", gain)

    @property
    def is_fading_out(self) -> bool:
        return self._fade is not None and not self._fade.rising

    def fade_in(self, seconds: float) -> None:
        self._start_fade(seconds, True, 0.0, None)

    def fade_out(
        self, seconds: float, hold_seconds: float = 0.0, done: Callable[[], None] | None = None
    ) -> None:
        self._start_fade(seconds, False, hold_seconds, done)

    def _start_fade(
        self, seconds: float, rising: bool, hold_seconds: float, done: Callable[[], None] | None
    ) -> None:
        self._fade = _Fade(
            time.monotonic(), max(seconds, 0.01), self._gain, rising, hold_seconds, done
        )

    def advance_fade(self, now: float) -> None:
        fade = self._fade
        if fade is None:
            return
        # Quarter sine waves: two songs crossing like this keep an even loudness.
        progress = min((now - fade.start) / fade.seconds, 1.0)
        angle = progress * math.pi / 2
        if fade.rising:
            self.set_gain(fade.from_gain + (1.0 - fade.from_gain) * math.sin(angle))
        else:
            self.set_gain(fade.from_gain * math.cos(angle))
        if now >= fade.start + fade.seconds + fade.hold_seconds:
            self._fade = None
            if fade.done is not None:
                fade.done()

    # ----- equaliser -----

    def set_equalizer(self, settings: EqSettings) -> None:
        self._preamp.set_property("volume", 10 ** (settings.preamp / 20))
        self._balance.set_property("panorama", settings.balance_position)
        frequencies = BAND_FREQUENCIES[settings.band_count]
        octaves = BAND_OCTAVES[settings.band_count]
        width_factor = 2 ** (octaves / 2) - 2 ** (-octaves / 2)
        for index in range(MAX_BANDS):
            band = self._equalizer.get_child_by_index(index)
            if index < settings.band_count:
                band.set_property("freq", float(frequencies[index]))
                band.set_property("bandwidth", frequencies[index] * width_factor)
                band.set_property("gain", float(settings.gains[index]))
            else:
                band.set_property("gain", 0.0)  # a band not in use leaves the sound alone


@dataclass
class _CastDetails:
    """What a device cast to shows for the track: the app fills it in at each start."""

    title: str = ""
    artist: str = ""
    album: str = ""
    cover: bytes | None = None


class AudioPlayer(QObject):
    """Plays one track at a time, overlapping two during a crossfade.

    The rest of the app talks to this class and listens to its signals,
    so it never needs to touch GStreamer directly.
    """

    track_started = Signal(object)  # the Path of the track now playing
    track_finished = Signal()  # the track reached its natural end
    # Time to start the next track for a crossfade. If nothing is played in
    # response, the track simply plays on and track_finished follows as usual.
    track_ending = Signal()
    state_changed = Signal(str)  # "playing", "paused" or "stopped"
    position_changed = Signal(int)  # playback position in milliseconds
    duration_changed = Signal(int)  # track length in milliseconds
    error_occurred = Signal(str)  # human-readable error message
    stream_title = Signal(str)  # a title the stream itself announced (radio played directly)
    audio_buffer_ready = Signal(object)  # a QAudioBuffer of the sound, for the visualiser
    cast_volume = Signal(int)  # the volume (0-100) of the device cast to, once known
    cast_ended = Signal(str)  # casting stopped by itself; the reason, for the user

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._active = _Deck()
        self._other = _Deck()  # idle, or fading out the previous track
        self._sound = SoundSettings()
        self._level_offset_db = 0.0  # brings tagged songs to the library's own volume
        self._level_enabled = True  # False plays every song at its own volume

        self._visual_format = QAudioFormat()
        self._visual_format.setSampleRate(_VISUAL_RATE)
        self._visual_format.setChannelCount(1)
        self._visual_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        # Visualiser sound that is not playing yet: (stream time in ns, bytes).
        self._visual_held: deque[tuple[int, bytes]] = deque(maxlen=_VISUAL_HELD_MAX)
        self._visual_deck: _Deck | None = None  # the deck the held sound came from

        self._state = "stopped"
        self._position = 0
        self._ticks = 0
        self._overlap_seconds = 0.0  # a crossfade waiting for its new track to start
        self._handing_over = False  # track_ending is being sent
        self._time_left_ms = 0  # of the track that is handing over

        self._heads: dict[Path, int] = {}  # where each scanned track's sound begins
        self._tails: dict[Path, int | None] = {}  # and where it ends
        self._waiting_for_head: Path | None = None  # held back until its scan reports
        self._scan: _SilenceScan | None = None
        self._running_scans: set[_SilenceScan] = set()
        self._scan_signals = _ScanSignals(self)
        self._scan_signals.head_found.connect(self._on_head_found)
        self._scan_signals.finished.connect(self._on_scan_finished)
        self._scan_pool = QThreadPool(self)
        self._scan_pool.setMaxThreadCount(2)  # one may still be cancelling

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)

        # Casting: while a session is open, the device plays and the decks rest.
        self._cast: CastSession | None = None
        self._cast_server = MediaServer()
        self._cast_details = _CastDetails()
        self._cast_loaded = False  # the device has the current track
        self._cast_duration = 0
        self._cast_clock = 0.0  # time.monotonic() of the last look, for radio's position

    def _decks(self) -> tuple[_Deck, _Deck]:
        return self._active, self._other

    # ----- state -----

    @property
    def has_track(self) -> bool:
        """True if a track is loaded (playing, paused or stopped)."""
        return self._active.track is not None

    @property
    def is_paused(self) -> bool:
        return self._state == "paused"

    @property
    def sound_start_ms(self) -> int:
        """Where the current track begins for the listener, after any skipped silence."""
        track = self._active.track
        return (self._start_of_sound(track) or 0) if track is not None else 0

    # ----- controls -----

    def play_file(self, path: str | Path) -> bool:
        """Start playing `path`, replacing whatever is playing now.

        Returns False (and emits error_occurred) if the file is missing.
        """
        track = Path(path)
        if not track.is_file():
            self.error_occurred.emit(f"File not found: {track.name}")
            return False
        if self._cast is not None:
            self._cast_open(track, 0, "playing")
            return True

        overlap = self._crossfade_length()
        self._cancel_overlap()
        if overlap:
            self._active, self._other = self._other, self._active
            self._overlap_seconds = overlap
        else:
            self._active.halt(Gst.State.READY)
        self._set_state("stopped")  # to the app, this is a track change like any other
        self._report_position(0)
        self._open(track, None, "playing")
        return True

    def play_episode(self, source: Path | str, position_ms: int = 0) -> bool:
        """Play a podcast episode from `position_ms`: a downloaded file (Path) or a
        web address (str), which is streamed.

        Unlike songs, it never crossfades and is not searched for silence (an
        hour-long episode would take long to scan).
        """
        if isinstance(source, Path) and not source.is_file():
            self.error_occurred.emit(f"File not found: {source.name}")
            return False
        if self._cast is not None:
            self._cast_open(source, position_ms, "playing")
            return True
        self._cancel_overlap()
        self._active.halt(Gst.State.READY)
        self._set_state("stopped")  # to the app, this is a track change like any other
        self._report_position(position_ms)
        self._open(source, position_ms if position_ms > 0 else None, "playing", scan=False)
        return True

    def play_radio(self, stream) -> None:
        """Play a radio station through its RadioStream (which allows time-shift)."""
        if self._cast is not None:
            self._active.radio_stream = stream
            self._cast_open(RADIO_URI, 0, "playing")
            return
        self._cancel_overlap()
        self._active.halt(Gst.State.READY)
        self._set_state("stopped")
        self._report_position(0)
        self._active.radio_stream = stream
        self._open(RADIO_URI, None, "playing", scan=False)

    def load_file(self, path: str | Path, position_ms: int = 0, duration_ms: int = 0) -> bool:
        """Get `path` ready to resume from `position_ms` when play() is called.

        The file is not opened until then, so its length is taken from the caller.
        Returns False (and emits error_occurred) if the file is missing.
        """
        track = Path(path)
        if not track.is_file():
            self.error_occurred.emit(f"File not found: {track.name}")
            return False
        if self._cast is not None:
            self._cast_open(track, position_ms, "paused")
            if duration_ms > 0:
                self.duration_changed.emit(duration_ms)
            return True

        self._cancel_overlap()
        self._halt_active()
        self._open(track, position_ms if position_ms > 0 else None, "paused")
        if duration_ms > 0:
            self.duration_changed.emit(duration_ms)
        self._report_position(position_ms)
        return True

    def play(self) -> None:
        """Resume a paused track, or restart a stopped one."""
        deck = self._active
        if deck.track is None:
            return
        if self._cast is not None:
            self._set_state("playing")
            if self._cast_loaded:
                self._cast.play()
            else:
                self._cast_load(autoplay=True)
            return
        previous = self._state
        self._set_state("playing")
        if self._waiting_for_head is not None:
            return
        if deck.is_fading_out:  # on its way to a pause or stop: turn it around
            deck.fade_in(PAUSE_FADE_SECONDS)
        elif previous == "paused" and self._sound.fade_on_pause and deck.ready:
            deck.set_gain(0.0)
            deck.set_playing()
            deck.fade_in(PAUSE_FADE_SECONDS)
        else:
            deck.set_gain(1.0)
            if previous == "stopped":
                deck.ending_announced = False
                if deck.pending_seek is None:
                    deck.pending_seek = self._start_of_sound(deck.track)
            deck.set_playing()

    def pause(self) -> None:
        deck = self._active
        if deck.track is None:
            return
        if self._cast is not None:
            self._set_state("paused")
            if self._cast_loaded:
                self._cast.pause()
            return
        previous = self._state
        self._cancel_overlap()
        self._set_state("paused")
        if self._waiting_for_head is not None:
            return
        if previous == "playing" and self._sound.fade_on_pause and deck.ready:
            deck.fade_out(PAUSE_FADE_SECONDS, OUTPUT_SETTLE_SECONDS, deck.set_paused)
        else:
            deck.set_paused()

    def stop(self) -> None:
        if self._cast is not None:
            if self._cast_loaded:
                self._cast.stop()
                self._cast_loaded = False
            self._set_state("stopped")
            self._report_position(0)
            return
        self._cancel_overlap()
        deck = self._active
        if self._state == "playing" and self._sound.fade_on_pause and deck.ready:
            self._set_state("stopped")
            deck.fade_out(PAUSE_FADE_SECONDS, OUTPUT_SETTLE_SECONDS, self._halt_active)
        else:
            self._halt_active()

    def seek(self, position_ms: int) -> None:
        if self._cast is not None:
            if self._active.track is None:
                return
            if self._cast_loaded:
                self._cast.seek(position_ms)
            self._report_position(position_ms)
            return
        self._cancel_overlap()
        deck = self._active
        if deck.track is None:
            return
        if position_ms == 0:
            position_ms = self._start_of_sound(deck.track) or 0
        self._waiting_for_head = None
        deck.restart_at(position_ms)
        self._report_position(position_ms)  # nothing else will until it plays
        if self._state == "playing":
            deck.set_gain(1.0)
            deck.set_playing()

    def set_volume(self, percent: int) -> None:
        """Set the volume from 0 to 100. While casting, it is the device's volume."""
        if self._cast is not None:
            self._cast.set_volume(percent)
            return
        for deck in self._decks():
            deck.set_volume(max(0, min(100, percent)) / 100)

    def set_muted(self, muted: bool) -> None:
        """Silence or restore the sound without changing the volume."""
        if self._cast is not None:
            self._cast.set_muted(muted)
            return
        for deck in self._decks():
            deck.set_muted(muted)

    def set_equalizer(self, settings: EqSettings) -> None:
        """Apply equaliser settings; they are heard straight away."""
        for deck in self._decks():
            deck.set_equalizer(settings)

    def set_level_offset(self, offset_db: float) -> None:
        """Set how far the current library's volume is above the tags' reference level."""
        self._level_offset_db = offset_db
        self._apply_track_gains()

    def set_level_enabled(self, enabled: bool) -> None:
        """Turn volume adjustment on or off; it is heard straight away."""
        self._level_enabled = enabled
        self._apply_track_gains()

    def _gain_for(self, track) -> float:
        if not self._level_enabled or not isinstance(track, Path):  # a stream has no tags here
            return 0.0
        return read_track_gain(track, self._level_offset_db)

    def _apply_track_gains(self) -> None:
        for deck in self._decks():
            if deck.track is not None:
                deck.set_track_gain(self._gain_for(deck.track))

    def set_sound_settings(self, settings: SoundSettings) -> None:
        """Apply crossfade, fade and silence settings, also to the track playing now."""
        self._sound = settings
        track = self._active.track
        if settings.skip_silence and track is not None and track not in self._tails:
            self._request_scan(track)

    def unload(self) -> None:
        """Stop playback and forget the current track."""
        if self._cast is not None and self._cast_loaded:
            self._cast.stop()
        self._cast_loaded = False
        self._timer.stop()
        self._cancel_scan()
        self._waiting_for_head = None
        self._overlap_seconds = 0.0
        for deck in self._decks():
            deck.halt(Gst.State.NULL)
            deck.track = None
        self._set_state("stopped")
        self._report_position(0)

    def shutdown(self) -> None:
        """Release everything. Call before the app exits."""
        self.unload()
        if self._cast is not None:
            self._cast.close(wait=3.0)
            self._cast = None
        self._cast_server.stop()
        self._scan_pool.waitForDone(3000)

    # ----- casting -----

    @property
    def casting(self) -> bool:
        return self._cast is not None

    @property
    def cast_device(self) -> CastDevice | None:
        return self._cast.device if self._cast is not None else None

    def set_cast_details(
        self, title: str, artist: str, album: str, cover: bytes | None
    ) -> None:
        """What a device should show for the track that just started (also while
        not casting, for when casting begins mid-track)."""
        self._cast_details = _CastDetails(title, artist, album, cover or None)

    def start_casting(self, device: CastDevice) -> None:
        """Play on `device` from now on, carrying on where the computer was."""
        if self._cast is not None:
            self._end_cast_session()
        track = self._active.track
        self._cancel_overlap()
        self._cancel_scan()
        self._waiting_for_head = None
        for deck in self._decks():
            deck.halt(Gst.State.READY)
        self._visual_held.clear()
        self._cast = open_session(device)
        self._cast_duration = 0
        self._cast_clock = time.monotonic()
        self._timer.start()
        if track is not None and self._state != "stopped":
            self._cast_load(autoplay=self._state == "playing")

    def stop_casting(self) -> None:
        """Play on this computer again, from where the device was."""
        if self._cast is None:
            return
        self._end_cast_session()
        deck = self._active
        track = deck.track
        if track is None:
            return
        radio = track == RADIO_URI
        if radio and deck.radio_stream is None:
            deck.track = None
            self._set_state("stopped")
            return
        position = self._position
        deck.open(track, position if position > 0 and not radio else None)
        deck.set_track_gain(self._gain_for(track))
        if isinstance(track, Path) and track in self._tails:
            deck.tail_ms = self._tails[track]
        self._timer.start()
        if self._state == "playing":
            deck.set_playing()
        elif self._state == "paused":
            deck.set_paused()  # ready to go on at once

    def _end_cast_session(self) -> None:
        session, self._cast = self._cast, None
        self._cast_loaded = False
        session.close()  # the device stops playing
        self._cast_server.stop()  # and nothing is offered on the network any more

    def _cast_open(self, track: Path | str, position_ms: int, state: str) -> None:
        """Start a track on the device, telling the app as playing it here would."""
        self._cancel_scan()
        self._waiting_for_head = None
        deck = self._active
        deck.track = track
        deck.duration_ms = 0
        if track != RADIO_URI:
            deck.radio_stream = None
        self._cast_loaded = False
        self._cast_duration = 0
        self._timer.start()
        self._set_state("stopped")  # to the app, this is a track change like any other
        self._report_position(position_ms)
        self.track_started.emit(track)  # the app answers with set_cast_details()
        self._set_state(state)
        self._cast_load(autoplay=state == "playing", position_ms=position_ms)

    def _cast_load(self, autoplay: bool, position_ms: int | None = None) -> None:
        """Send the current track to the device."""
        session = self._cast
        track = self._active.track
        if session is None or track is None:
            return
        position = self._position if position_ms is None else position_ms
        server = self._cast_server
        host = session.device.host
        details = self._cast_details
        try:
            if track == RADIO_URI:
                stream = self._active.radio_stream
                if stream is None:
                    return
                kind = stream.content_type or "audio/mpeg"
                url = server.url(server.share_radio(stream, kind), host)
                live, position = True, 0
            elif isinstance(track, Path):
                kind = audio_type(track.name)
                url = server.url(server.share_file(track), host)
                live = False
            else:  # a web address: the device fetches it itself
                kind = audio_type(track)
                url = track
                live = kind == "application/x-mpegURL"
            cover = server.url(server.share_image(details.cover), host) if details.cover else ""
        except (CastError, OSError) as error:
            self.error_occurred.emit(f"Cannot cast: {error}")
            return
        title = details.title or (track.stem if isinstance(track, Path) else "")
        session.load(CastMedia(
            url=url,
            kind=kind,
            title=title,
            artist=details.artist,
            album=details.album,
            cover_url=cover,
            live=live,
            position_ms=position,
            autoplay=autoplay,
        ))
        self._cast_loaded = True
        self._cast_clock = time.monotonic()

    def _cast_tick(self) -> None:
        """Keep up with the device: its position, its end and its troubles."""
        session = self._cast
        now = time.monotonic()
        elapsed_ms = int((now - self._cast_clock) * 1000)
        self._cast_clock = now
        if session.lost:
            if self._state == "playing":
                self._set_state("paused")  # never burst out of the computer's speakers
            self.stop_casting()
            self.cast_ended.emit(session.lost)
            return
        for message in session.take_errors():
            self.error_occurred.emit(message)
        volume = session.take_volume()
        if volume is not None:
            self.cast_volume.emit(volume)
        if not self._cast_loaded:
            return
        radio = self._active.track == RADIO_URI
        if not radio and session.take_finished():
            self._cast_loaded = False
            self._set_state("stopped")
            self._report_position(0)
            self.track_finished.emit()
            return
        if session.take_interrupted() or (radio and session.take_finished()):
            # Stopped on the device itself, or by another app: wait, paused.
            self._cast_loaded = False
            if self._state == "playing":
                self._set_state("paused")
            return
        state, position, duration, settled = session.status()
        if radio:  # a live stream has no position of its own: count the time heard
            if self._state == "playing":
                self._report_position(self._position + elapsed_ms)
            return
        self._ticks += 1
        if not settled or self._ticks % _TICKS_PER_POSITION:
            return
        if duration > 0 and duration != self._cast_duration:
            self._cast_duration = duration
            self._active.duration_ms = duration
            self.duration_changed.emit(duration)
        if state in ("playing", "paused"):
            self._report_position(position)
            if self._state != "stopped" and state != self._state:
                self._set_state(state)  # paused or played with the device's own remote

    # ----- starting a track -----

    def _crossfade_length(self) -> float:
        """How long the playing track should overlap the next one; 0 for not at all."""
        sound = self._sound
        if not sound.crossfade or self._state != "playing" or not self._active.ready:
            return 0.0
        if self._handing_over:
            return min(sound.crossfade_seconds, self._time_left_ms / 1000)
        return sound.crossfade_seconds if sound.crossfade_on_skip else 0.0

    def _cancel_overlap(self) -> None:
        self._overlap_seconds = 0.0
        self._other.halt(Gst.State.READY)

    def _halt_active(self) -> None:
        self._waiting_for_head = None
        self._active.halt(Gst.State.READY)
        self._set_state("stopped")
        self._report_position(0)

    def _start_of_sound(self, track: Path) -> int | None:
        """Where to start this track, if silence is being skipped and it has some."""
        if not self._sound.skip_silence:
            return None
        head = self._heads.get(track, 0)
        return head if head >= MIN_SILENCE_AT_START_MS else None

    def _open(
        self, track: Path | str, position_ms: int | None, state: str, scan: bool = True
    ) -> None:
        deck = self._active
        deck.open(track, position_ms)
        deck.set_track_gain(self._gain_for(track))
        self._waiting_for_head = None
        self._timer.start()
        self.track_started.emit(track)  # first, so that the new state goes with the new track
        self._set_state(state)
        if self._sound.skip_silence and scan:
            if track not in self._tails:
                self._request_scan(track)
            else:
                deck.tail_ms = self._tails[track]
            if position_ms is None:
                if track not in self._heads and track not in self._tails:
                    self._waiting_for_head = track  # the scan says where to begin
                    return
                deck.pending_seek = self._start_of_sound(track)
        self._start_deck()

    def _start_deck(self) -> None:
        """Set the active deck going, as the player's state says it should be."""
        deck = self._active
        if self._state == "playing":  # a track loaded paused waits for play()
            overlap = self._overlap_seconds
            self._overlap_seconds = 0.0
            if overlap:
                other = self._other
                deck.set_gain(0.0)
                deck.fade_in(overlap)
                other.fade_out(overlap, 0.0, lambda: other.halt(Gst.State.READY))
            deck.set_playing()

    # ----- finding silence -----

    def _request_scan(self, track: Path) -> None:
        if self._scan is not None and self._scan.track == track:
            return
        self._cancel_scan()
        self._scan = _SilenceScan(track, self._scan_signals)
        self._running_scans.add(self._scan)
        self._scan_pool.start(self._scan)

    def _cancel_scan(self) -> None:
        if self._scan is not None:
            self._scan.cancel()
            self._scan = None

    def _on_head_found(self, track: Path, head_ms: int) -> None:
        self._heads[track] = head_ms
        self._release_waiting_track(track)

    def _on_scan_finished(self, scan: _SilenceScan) -> None:
        self._running_scans.discard(scan)
        if self._scan is scan:
            self._scan = None
        if scan.cancelled:
            return
        self._heads[scan.track] = scan.head_ms or 0
        self._tails[scan.track] = scan.tail_ms
        if self._active.track == scan.track:
            self._active.tail_ms = scan.tail_ms
        self._release_waiting_track(scan.track)

    def _release_waiting_track(self, track: Path) -> None:
        if self._waiting_for_head == track:
            self._waiting_for_head = None
            self._active.pending_seek = self._start_of_sound(track)
            self._start_deck()

    # ----- keeping up with GStreamer -----

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)

    def _report_position(self, position_ms: int) -> None:
        if position_ms != self._position:
            self._position = position_ms
            self.position_changed.emit(position_ms)

    def _tick(self) -> None:
        if self._cast is not None:
            self._cast_tick()
            return
        now = time.monotonic()
        for deck in self._decks():
            deck.advance_fade(now)
            self._read_messages(deck)
        self._collect_visualiser_sound()
        self._ticks += 1
        deck = self._active
        if deck.ready and self._ticks % _TICKS_PER_POSITION == 0:
            position = deck.position_ms()
            if position is not None:
                self._report_position(position)
                if self._state == "playing":
                    self._check_for_end(deck, position)

    def _check_for_end(self, deck: _Deck, position_ms: int) -> None:
        """Act when the track reaches its last sound, or the point to crossfade from."""
        sound = self._sound
        duration = deck.duration_ms
        if duration <= 0:
            return
        end = duration
        if (
            sound.skip_silence
            and deck.tail_ms is not None
            and duration - deck.tail_ms >= MIN_SILENCE_AT_END_MS
        ):
            end = deck.tail_ms
        if end < duration and position_ms >= end:
            self._end_of_track()
            return
        if sound.crossfade and not deck.ending_announced:
            lead_ms = min(sound.crossfade_seconds * 1000, duration / 2)
            if position_ms >= end - lead_ms:
                deck.ending_announced = True
                self._time_left_ms = end - position_ms
                self._handing_over = True
                try:
                    self.track_ending.emit()
                finally:
                    self._handing_over = False

    def _end_of_track(self) -> None:
        self._halt_active()
        self.track_finished.emit()

    def _read_messages(self, deck: _Deck) -> None:
        while (message := deck.bus.pop_filtered(_BUS_MESSAGES)) is not None:
            kind = message.type
            active = deck is self._active
            if kind == Gst.MessageType.EOS:
                if active:
                    self._end_of_track()
                else:
                    deck.halt(Gst.State.READY)
            elif kind == Gst.MessageType.ERROR:
                if active:
                    error, _details = message.parse_error()
                    name = deck.track.name if deck.track else "song"
                    self._halt_active()
                    self.error_occurred.emit(f"Cannot play {name}: {error.message}")
                else:
                    deck.halt(Gst.State.READY)
            elif kind == Gst.MessageType.ASYNC_DONE:
                deck.on_ready()
                self._report_duration(deck)
            elif kind == Gst.MessageType.TAG:
                if active:
                    found, title = message.parse_tag().get_string("title")
                    if found and title:
                        self.stream_title.emit(title)
            elif kind == Gst.MessageType.DURATION_CHANGED:
                deck.read_duration()
                self._report_duration(deck)

    def _report_duration(self, deck: _Deck) -> None:
        if deck is self._active and deck.duration_ms > 0:
            self.duration_changed.emit(deck.duration_ms)

    def _collect_visualiser_sound(self) -> None:
        """Hand the visualiser the sound that is being heard right now.

        The sound arrives early, with its place in the song. It waits here until
        the playing position (which the sound system keeps up to date with what
        comes out of the speakers) reaches it.
        """
        deck = self._active
        held = self._visual_held
        if deck is not self._visual_deck:  # a new deck after a crossfade: another timeline
            held.clear()
            self._visual_deck = deck
        while (sample := deck.samples.emit("try-pull-sample", 0)) is not None:
            buffer = sample.get_buffer()
            start = sample.get_segment().to_stream_time(Gst.Format.TIME, buffer.pts)
            if buffer.pts == Gst.CLOCK_TIME_NONE or start in (-1, Gst.CLOCK_TIME_NONE):
                continue
            held.append((start, buffer.extract_dup(0, buffer.get_size())))
        if not held:
            return
        found, position = deck.pipeline.query_position(Gst.Format.TIME)
        if not found:
            return
        if held[0][0] > position + _VISUAL_AHEAD_NS:
            held.clear()  # sound from before a seek backwards; it will not come up
            return
        while held and held[0][0] <= position:
            start, data = held.popleft()
            if start >= position - _VISUAL_LATE_NS:
                self.audio_buffer_ready.emit(QAudioBuffer(QByteArray(data), self._visual_format))
