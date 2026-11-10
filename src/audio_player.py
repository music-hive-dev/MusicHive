"""Audio playback built on GStreamer, with an equaliser in the signal path.

There are two identical "decks", each a complete player. Normally one is in use;
during a crossfade the outgoing song keeps playing on one while the next starts
on the other, and the sound system mixes them.
"""

import math
import threading
import time
from array import array
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
# settings applied.
_OUTPUT_CHAIN = (
    "tee name=split "
    "split. ! queue max-size-time=100000000 ! volume name=trackgain "
    "! volume name=fade ! volume name=master "
    "! audioconvert ! audioresample ! autoaudiosink "
    "split. ! queue leaky=downstream max-size-buffers=8 ! audioconvert ! audioresample "
    "! audio/x-raw,format=S16LE,rate=44100,channels=1 "
    "! appsink name=samples sync=true max-buffers=8 drop=true"
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
)

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

        self.track: Path | None = None
        self.ready = False  # the track is loaded far enough to seek in it
        self.pending_seek: int | None = None  # where to jump to once it is ready
        self.duration_ms = 0
        self.tail_ms: int | None = None  # where the last sound ends, once known
        self.ending_announced = False  # track_ending was sent for this track
        self._gain = 1.0
        self._fade: _Fade | None = None

    # ----- transport -----

    def open(self, track: Path, position_ms: int | None) -> None:
        self.halt(Gst.State.READY)
        self.track = track
        self.pending_seek = position_ms
        self.duration_ms = 0
        self.tail_ms = None
        self.ending_announced = False
        self.set_gain(1.0)
        self.pipeline.set_property("uri", track.as_uri())

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
    audio_buffer_ready = Signal(object)  # a QAudioBuffer of the sound, for the visualiser

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._active = _Deck()
        self._other = _Deck()  # idle, or fading out the previous track
        self._sound = SoundSettings()
        self._level_offset_db = 0.0  # brings tagged songs to the library's own volume
        self._level_enabled = True  # False plays every song at its own volume

        self._visual_format = QAudioFormat()
        self._visual_format.setSampleRate(44100)
        self._visual_format.setChannelCount(1)
        self._visual_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)

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

    def load_file(self, path: str | Path, position_ms: int = 0, duration_ms: int = 0) -> bool:
        """Get `path` ready to resume from `position_ms` when play() is called.

        The file is not opened until then, so its length is taken from the caller.
        Returns False (and emits error_occurred) if the file is missing.
        """
        track = Path(path)
        if not track.is_file():
            self.error_occurred.emit(f"File not found: {track.name}")
            return False

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
        self._cancel_overlap()
        deck = self._active
        if self._state == "playing" and self._sound.fade_on_pause and deck.ready:
            self._set_state("stopped")
            deck.fade_out(PAUSE_FADE_SECONDS, OUTPUT_SETTLE_SECONDS, self._halt_active)
        else:
            self._halt_active()

    def seek(self, position_ms: int) -> None:
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
        """Set the volume from 0 to 100."""
        for deck in self._decks():
            deck.set_volume(max(0, min(100, percent)) / 100)

    def set_muted(self, muted: bool) -> None:
        """Silence or restore the sound without changing the volume."""
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
        return read_track_gain(track, self._level_offset_db) if self._level_enabled else 0.0

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
        self._scan_pool.waitForDone(3000)

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

    def _open(self, track: Path, position_ms: int | None, state: str) -> None:
        deck = self._active
        deck.open(track, position_ms)
        deck.set_track_gain(self._gain_for(track))
        self._waiting_for_head = None
        self._timer.start()
        self.track_started.emit(track)  # first, so that the new state goes with the new track
        self._set_state(state)
        if self._sound.skip_silence:
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
            elif kind == Gst.MessageType.DURATION_CHANGED:
                deck.read_duration()
                self._report_duration(deck)

    def _report_duration(self, deck: _Deck) -> None:
        if deck is self._active and deck.duration_ms > 0:
            self.duration_changed.emit(deck.duration_ms)

    def _collect_visualiser_sound(self) -> None:
        samples = self._active.samples
        while (sample := samples.emit("try-pull-sample", 0)) is not None:
            buffer = sample.get_buffer()
            data = buffer.extract_dup(0, buffer.get_size())
            self.audio_buffer_ready.emit(QAudioBuffer(QByteArray(data), self._visual_format))
