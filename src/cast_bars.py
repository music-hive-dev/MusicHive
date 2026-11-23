"""Bar maps for the browser page cast to (assets/tv/): how loud each part of a
song's sound is, FRAMES_PER_SECOND times a second, worked out on this computer.

With one, the page shows the visualiser from the first moment of the song and
does no sound analysis of its own (a TV's browser is slow). GStreamer's spectrum
element does the arithmetic. The next song is mapped while one plays, and maps
are kept on disk, so most songs have theirs before they start.

A map is FINE_BANDS bytes per frame. Each byte is the loudest part of that
band, in steps of DB_STEP above FLOOR_DB, on the same scale as MusicHive's own
visualiser, so the page turns them into bars the way now_playing.py does.
"""

import hashlib
import math
import re
import threading
import zlib
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from PySide6.QtCore import QObject, QRunnable, QStandardPaths, QThreadPool, Signal  # noqa: E402

Gst.init(None)

FRAMES_PER_SECOND = 30
RATE = 32000  # the sound is analysed at this rate: up to 16 kHz, as the bars go
LINEAR_BANDS = 512  # what spectrum measures: RATE / 2 in equal steps of about 31 Hz
FINE_BANDS = 96  # what a map keeps: steps that grow with the pitch, as the bars do
LOWEST_HZ, HIGHEST_HZ = 40, 16000  # as in now_playing.py
FLOOR_DB = -100.0
DB_STEP = 0.5
# spectrum uses a Hamming window and divides by the FFT size; MusicHive's own
# visualiser a Hann window and 4 / size. A full-scale tone comes out this much
# lower here, so it is added to read the same.
SCALE_OFFSET_DB = 20 * math.log10(1 / 0.27)
CACHE_VERSION = "1"
CACHE_FILES = 400  # about 300 KB each, compressed; the oldest go first

EDGES = [LOWEST_HZ * (HIGHEST_HZ / LOWEST_HZ) ** (i / FINE_BANDS) for i in range(FINE_BANDS + 1)]
_FFT_SIZE = 2 * LINEAR_BANDS - 2  # how spectrum sizes its FFT
_MAGNITUDES = re.compile(r"magnitude=\(float\)\{([^}]*)\}")


def _bins_of_bands() -> list[list[int]]:
    """For each fine band, the spectrum bands (linear) that fall in it."""
    width = RATE / _FFT_SIZE
    bands = []
    for low, high in zip(EDGES, EDGES[1:]):
        bins = [i for i in range(LINEAR_BANDS) if low <= i * width < high]
        bands.append(bins or [min(LINEAR_BANDS - 1, round((low + high) / 2 / width))])
    return bands


_BINS = _bins_of_bands()


def describe(url: str) -> dict:
    """What the page needs to read a map at `url`."""
    return {
        "url": url,
        "fps": FRAMES_PER_SECOND,
        "edges": EDGES,
        "floorDb": FLOOR_DB,
        "step": DB_STEP,
    }


def supported() -> bool:
    return Gst.ElementFactory.find("spectrum") is not None


def _cache_folder() -> Path:
    return Path(QStandardPaths.writableLocation(QStandardPaths.CacheLocation)) / "cast-bars"


def _cache_file(track: Path) -> Path | None:
    try:
        stat = track.stat()
    except OSError:
        return None
    key = f"{CACHE_VERSION}|{track}|{stat.st_mtime_ns}|{stat.st_size}"
    return _cache_folder() / f"{hashlib.sha1(key.encode()).hexdigest()}.bars"


class _Signals(QObject):
    done = Signal(object, bool)  # the track, and whether a map was made


class _MapJob(QRunnable):
    """Decodes a track as fast as it can and keeps what spectrum measures."""

    def __init__(self, track: Path, target: Path, signals: _Signals) -> None:
        super().__init__()
        self.setAutoDelete(False)  # Python keeps ownership of this object
        self.track = track
        self._target = target
        self._signals = signals
        self.cancel = threading.Event()

    def run(self) -> None:
        made = False
        try:
            made = self._make()
        except Exception as error:  # the page then works it out itself
            print(f"MusicHive: no bar map for {self.track.name}: {error}")
        finally:
            self._signals.done.emit(self.track, made)

    def _make(self) -> bool:
        pipeline = Gst.parse_launch(
            "filesrc name=source ! decodebin ! audioconvert ! audioresample "
            f"! audio/x-raw,format=F32LE,channels=1,rate={RATE} "
            f"! spectrum bands={LINEAR_BANDS} interval={Gst.SECOND // FRAMES_PER_SECOND} "
            f"threshold={int(FLOOR_DB)} post-messages=true message-magnitude=true "
            "! fakesink sync=false"
        )
        pipeline.get_by_name("source").set_property("location", str(self.track))
        frames: dict[int, bytes] = {}
        bus = pipeline.get_bus()
        wanted = Gst.MessageType.ELEMENT | Gst.MessageType.EOS | Gst.MessageType.ERROR
        pipeline.set_state(Gst.State.PLAYING)
        try:
            while not self.cancel.is_set():
                message = bus.timed_pop_filtered(100 * Gst.MSECOND, wanted)
                if message is None:
                    continue
                if message.type == Gst.MessageType.EOS:
                    break
                if message.type == Gst.MessageType.ERROR:
                    error, _details = message.parse_error()
                    raise RuntimeError(error.message)
                structure = message.get_structure()
                if structure is None or structure.get_name() != "spectrum":
                    continue
                found, timestamp = structure.get_uint64("timestamp")
                magnitudes = _MAGNITUDES.search(structure.to_string())
                if not found or magnitudes is None:
                    continue
                frames[round(timestamp * FRAMES_PER_SECOND / Gst.SECOND)] = self._frame(
                    [float(value) for value in magnitudes.group(1).split(",")]
                )
        finally:
            pipeline.set_state(Gst.State.NULL)
        if self.cancel.is_set() or not frames:
            return False
        silent = bytes(FINE_BANDS)
        data = b"".join(frames.get(i, silent) for i in range(max(frames) + 1))
        self._target.parent.mkdir(parents=True, exist_ok=True)
        partial = self._target.with_suffix(".part")
        partial.write_bytes(zlib.compress(data, 6))
        partial.replace(self._target)
        return True

    @staticmethod
    def _frame(magnitudes: list[float]) -> bytes:
        top = len(magnitudes) - 1
        values = []
        for bins in _BINS:
            loudest = max(magnitudes[min(i, top)] for i in bins) + SCALE_OFFSET_DB
            values.append(max(0, min(255, round((loudest - FLOOR_DB) / DB_STEP))))
        return bytes(values)


class BarMaps(QObject):
    """Makes bar maps in the background, one song at a time, and keeps them."""

    done = Signal(object, bool)  # the track, and whether its map is ready now

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._signals = _Signals(self)
        self._signals.done.connect(self._on_done)
        self._jobs: dict[Path, _MapJob] = {}
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)  # one at a time: the computer has music to play
        self.supported = supported()

    def get(self, track: Path) -> bytes | None:
        """The track's map, if it was made before."""
        target = _cache_file(track)
        if target is None:
            return None
        try:
            data = zlib.decompress(target.read_bytes())
            target.touch()  # recently used: kept longest
            return data
        except (OSError, zlib.error):
            return None

    def make(self, track: Path, urgent: bool = False) -> bool:
        """Start making the track's map (if it is not there or on its way).
        `urgent`: before the songs that are only being prepared. False if no
        map can be made here."""
        target = _cache_file(track)
        if not self.supported or target is None:
            return False
        if target.exists() or track in self._jobs:
            return True
        job = _MapJob(track, target, self._signals)
        self._jobs[track] = job
        self._pool.start(job, 1 if urgent else 0)
        return True

    def _on_done(self, track: Path, made: bool) -> None:
        self._jobs.pop(track, None)
        if made:
            self._tidy_cache()
        self.done.emit(track, made)

    @staticmethod
    def _tidy_cache() -> None:
        try:
            files = sorted(_cache_folder().glob("*.bars"), key=lambda f: f.stat().st_mtime)
            for old in files[:-CACHE_FILES]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    def shutdown(self) -> None:
        for job in self._jobs.values():
            job.cancel.set()
        self._pool.clear()
        self._pool.waitForDone(2000)
