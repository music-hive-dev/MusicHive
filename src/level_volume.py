"""Tools > Level Volume: measure how loud songs are, so a library plays at one volume.

Each song's loudness is stored in its standard ReplayGain tags, so it is only
measured once and other players can use it too. Those tags bring a song to a
fixed reference level, which is quieter than most music. So the library also
remembers its own volume, the middle of all its songs' gains: the player plays
every song at that volume instead. New songs are measured the same way and join it.
"""

import sqlite3
import statistics
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal  # noqa: E402
from PySide6.QtWidgets import QMessageBox, QWidget  # noqa: E402

from src import library_db  # noqa: E402
from src.lastfm_dialog import load_dialog  # noqa: E402
from src.library_db import LibraryInfo  # noqa: E402
from src.tag_writer import TagJob, TagWriter  # noqa: E402
from src.track_metadata import TagChanges, parse_gain_text  # noqa: E402

Gst.init(None)

GAIN_TAG = "replaygaintrackgain"
PEAK_TAG = "replaygaintrackpeak"
# The analyser only knows some sample rates and at most two channels.
_MEASURE_CHAIN = (
    "audioconvert ! audioresample ! audio/x-raw,format=F32LE,rate=44100,channels=2 "
    "! rganalysis ! fakesink sync=false"
)
_WAIT_NS = 200 * Gst.MSECOND
FREE_CORES = 2  # left alone so that the music and the window stay smooth
MAX_WORKERS = 8
SAVE_BATCH = 100  # finished songs are written in groups, so stopping loses little


def measure_loudness(track: Path, cancel: threading.Event) -> tuple[float, float] | None:
    """Decode a song to find its (gain in dB, peak). None if that cannot be done."""
    pipeline = None
    gain = peak = None
    try:
        output = Gst.parse_bin_from_description(_MEASURE_CHAIN, True)
        pipeline = Gst.parse_launch("playbin flags=audio")
        pipeline.set_property("audio-sink", output)
        pipeline.set_property("uri", track.as_uri())
        bus = pipeline.get_bus()
        pipeline.set_state(Gst.State.PLAYING)
        wanted = Gst.MessageType.EOS | Gst.MessageType.ERROR | Gst.MessageType.TAG
        while not cancel.is_set():
            message = bus.timed_pop_filtered(_WAIT_NS, wanted)
            if message is None:
                continue
            if message.type == Gst.MessageType.ERROR:
                return None
            if message.type == Gst.MessageType.EOS:
                return (gain, peak) if gain is not None and peak is not None else None
            # The analyser reports last, so its values replace any found earlier.
            tags = message.parse_tag()
            found, value = tags.get_double(Gst.TAG_TRACK_GAIN)
            if found:
                gain = value
            found, value = tags.get_double(Gst.TAG_TRACK_PEAK)
            if found:
                peak = value
        return None
    except Exception as error:  # one bad song must not stop the others
        print(f"MusicHive: cannot measure {track.name}: {error}")
        return None
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)


class _Signals(QObject):
    measured = Signal(object, object)  # the Path, (gain, peak) or None
    worker_done = Signal(object)  # the _Worker


class _Worker(QRunnable):
    """Measures songs from the shared list until there are none left."""

    def __init__(self, tracks: deque, cancel: threading.Event, signals: _Signals) -> None:
        super().__init__()
        self.setAutoDelete(False)  # Python keeps ownership of this object
        self._tracks = tracks
        self._cancel = cancel
        self._signals = signals

    def run(self) -> None:
        try:
            while not self._cancel.is_set():
                try:
                    track = self._tracks.popleft()
                except IndexError:
                    break
                result = measure_loudness(track, self._cancel)
                if not self._cancel.is_set():
                    self._signals.measured.emit(track, result)
        finally:
            self._signals.worker_done.emit(self)


class LevelVolumeTool(QObject):
    """Connects the widgets of level_volume.ui and runs the measuring."""

    def __init__(
        self,
        ui_path: Path,
        parent: QWidget,
        tag_writer: TagWriter,
        busy_reason: Callable[[], str | None],
        on_finished: Callable[[LibraryInfo, str], None],
        show_status: Callable[[str], None],
    ) -> None:
        super().__init__(parent)
        self.dialog = d = load_dialog(ui_path, parent)
        self._tag_writer = tag_writer
        self._busy_reason = busy_reason
        self._on_finished = on_finished
        self._show_status = show_status

        self._library: LibraryInfo | None = None
        self._cancel = threading.Event()
        self._workers: set[_Worker] = set()
        self._gains: list[float] = []  # of every song: known before, and measured now
        self._jobs: list[TagJob] = []  # measured, not written yet
        self._total = self._done = self._measured = self._failed = 0
        self._set_target = False
        self._saving: list[list[TagJob]] = []  # groups handed over, not written yet
        self._saved = self._save_failed = 0
        self._resetting = False
        self._final_message = ""  # shown once the last group has been written

        self._signals = _Signals(self)
        self._signals.measured.connect(self._on_measured)
        self._signals.worker_done.connect(self._on_worker_done)
        self._pool = QThreadPool(self)
        tag_writer.tags_saved.connect(self._on_tags_saved)

        d.levelStartButton.clicked.connect(lambda _checked=False: self._start())
        d.levelRescanButton.clicked.connect(lambda _checked=False: self._rescan_everything())
        d.levelResetButton.clicked.connect(lambda _checked=False: self._reset())
        d.levelStopButton.clicked.connect(lambda _checked=False: self._stop())
        d.levelButtonBox.rejected.connect(d.reject)
        d.levelStopButton.setEnabled(False)

    @property
    def is_running(self) -> bool:
        return bool(self._workers or self._saving)

    def owns(self, result) -> bool:
        """True if these saved tags are part of this tool's work."""
        return any(result.jobs is batch for batch in self._saving)

    def show(self, library: LibraryInfo) -> None:
        if not self.is_running:
            self._library = library
            self._show_summary()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def shutdown(self) -> None:
        """The app is closing: stop measuring and save what is finished."""
        self._cancel.set()
        self._pool.waitForDone(5000)
        self._save_jobs()

    # ----- what the library looks like -----

    def _read_state(self) -> tuple[list[str], dict[str, float], float | None]:
        """All songs, the known gains of those measured, and the remembered volume."""
        db_path = self._library.db_path
        paths = library_db.track_paths(db_path)
        gains = {}
        for rel_path, tags in library_db.load_tag_values(db_path, [GAIN_TAG]).items():
            gain = parse_gain_text(tags[GAIN_TAG])
            if gain is not None:
                gains[rel_path] = gain
        return paths, gains, library_db.load_level_target(db_path)

    def _show_summary(self) -> None:
        d = self.dialog
        try:
            paths, gains, target = self._read_state()
        except sqlite3.Error as error:
            d.levelSummary.setText(f"Cannot read this library: {error}")
            self._show_busy(False)
            return
        new = sum(1 for path in paths if path not in gains)
        volume = f"{target:.1f} dB" if target is not None else "set when you first level it"
        d.levelSummary.setText(
            f"{self._library.name}: {len(paths)} songs, {len(paths) - new} levelled, {new} new.\n"
            f"Library volume: {volume}"
        )
        d.levelStartButton.setEnabled(bool(paths) and (new > 0 or target is None))
        d.levelRescanButton.setEnabled(bool(paths))
        d.levelResetButton.setEnabled(bool(gains) or target is not None)

    # ----- running -----

    def _show_busy(self, can_stop: bool) -> None:
        d = self.dialog
        d.levelStartButton.setEnabled(False)
        d.levelRescanButton.setEnabled(False)
        d.levelResetButton.setEnabled(False)
        d.levelStopButton.setEnabled(can_stop)

    def _show_progress(self, total: int) -> None:
        d = self.dialog
        d.levelStatus.setText("")
        d.levelProgress.setRange(0, total)
        d.levelProgress.setValue(0)
        d.levelProgress.show()

    def _confirmed(self, title: str, question: str) -> bool:
        answer = QMessageBox.question(
            self.dialog,
            title,
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _rescan_everything(self) -> None:
        if self._confirmed(
            "Re-scan entire library",
            "Measure every song in this library again?\n\n"
            "This replaces all of their volume tags and sets a new library volume.",
        ):
            self._start(everything=True)

    def _start(self, everything: bool = False) -> None:
        d = self.dialog
        reason = self._busy_reason()
        if reason:
            d.levelStatus.setText(reason)
            return
        try:
            paths, gains, target = self._read_state()
        except sqlite3.Error as error:
            d.levelStatus.setText(f"Cannot read this library: {error}")
            return
        root = self._library.root
        tracks = deque(root / path for path in paths if everything or path not in gains)
        self._gains = [] if everything else list(gains.values())
        self._jobs = []
        self._total, self._done, self._measured, self._failed = len(tracks), 0, 0, 0
        self._saved = self._save_failed = 0
        self._set_target = everything or target is None
        self._cancel = threading.Event()
        if not tracks:
            self._finish()
            return

        self._show_busy(True)
        self._show_progress(self._total)
        count = min(MAX_WORKERS, max(1, QThread.idealThreadCount() - FREE_CORES), len(tracks))
        self._pool.setMaxThreadCount(count)
        for _ in range(count):
            worker = _Worker(tracks, self._cancel, self._signals)
            self._workers.add(worker)
            self._pool.start(worker)

    def _stop(self) -> None:
        self._cancel.set()
        self.dialog.levelStopButton.setEnabled(False)
        self.dialog.levelStatus.setText("Stopping…")

    def _on_measured(self, track: Path, result: tuple[float, float] | None) -> None:
        self._done += 1
        self.dialog.levelProgress.setValue(self._done)
        if result is None:
            self._failed += 1
            return
        gain, peak = result
        self._measured += 1
        self._gains.append(gain)
        changes = TagChanges({GAIN_TAG: f"{gain:+.2f} dB", PEAK_TAG: f"{peak:.6f}"})
        self._jobs.append(TagJob(track, changes))
        if len(self._jobs) >= SAVE_BATCH:
            self._save_jobs()

    def _on_worker_done(self, worker: _Worker) -> None:
        self._workers.discard(worker)
        if not self._workers:
            self._finish()

    def _on_tags_saved(self, result) -> None:
        """Songs have reached the database: the counts can be shown right."""
        for batch in self._saving:
            if result.jobs is batch:
                self._saving.remove(batch)
                self._saved += len(batch)
                self._save_failed += len(result.failures)
                self._report_saving()
                if not self._saving and not self._workers:
                    self._publish_final()
                return
        if not self.is_running and self._library is not None and self.dialog.isVisible():
            self._show_summary()

    # ----- resetting -----

    def _reset(self) -> None:
        d = self.dialog
        reason = self._busy_reason()
        if reason:
            d.levelStatus.setText(reason)
            return
        library = self._library
        try:
            paths, gains, _target = self._read_state()
        except sqlite3.Error as error:
            d.levelStatus.setText(f"Cannot read this library: {error}")
            return
        tagged = [library.root / path for path in paths if path in gains]
        if not self._confirmed(
            "Reset volume levels",
            f"Remove the volume tags from {len(tagged)} songs and forget this library's volume?"
            "\n\nSongs go back to their original volume. All of them would have to be "
            "measured again to level them.",
        ):
            return
        try:
            library_db.clear_level_target(library.db_path)
        except sqlite3.Error as error:
            d.levelStatus.setText(f"Could not reset this library: {error}")
            return
        self._total = self._measured = len(tagged)
        self._failed = self._saved = self._save_failed = 0
        self._resetting = True
        if not tagged:
            self._publish_final()
            return
        self._show_busy(False)
        self._show_progress(len(tagged))
        removal = {GAIN_TAG: "", PEAK_TAG: ""}
        for start in range(0, len(tagged), SAVE_BATCH):
            batch = tagged[start : start + SAVE_BATCH]
            jobs = [TagJob(track, TagChanges(dict(removal))) for track in batch]
            self._saving.append(jobs)
            self._tag_writer.save_tags(jobs, library.root, library.db_path)

    def _save_jobs(self) -> None:
        if self._jobs:
            library = self._library
            self._saving.append(self._jobs)
            self._tag_writer.save_tags(self._jobs, library.root, library.db_path)
            self._jobs = []

    def _report_saving(self) -> None:
        d = self.dialog
        # While songs are still being measured, some of the rest may yet fail.
        measuring = self._workers and not self._cancel.is_set()
        total = self._total - self._failed if measuring else self._measured
        text = f"Saving volume tags: {self._saved} of {total}"
        d.levelStatus.setText(text)
        self._show_status(text)
        if self._resetting:
            d.levelProgress.setValue(self._saved)

    def _publish_final(self) -> None:
        """Everything has been written: say how it went."""
        d = self.dialog
        if self._resetting:
            count = self._total - self._save_failed
            message = f"Reset {count} song{'' if count == 1 else 's'}"
            self._resetting = False
        else:
            message = self._final_message
        if self._save_failed:
            message += f", {self._save_failed} could not be saved"
        d.levelProgress.hide()
        d.levelStatus.setText(message)
        self._show_summary()
        self._on_finished(self._library, message)

    def _finish(self) -> None:
        d = self.dialog
        self._save_jobs()
        stopped = self._cancel.is_set()
        library = self._library
        target = None
        message = f"Levelled {self._measured} song{'' if self._measured == 1 else 's'}"
        if self._failed:
            message += f", {self._failed} could not be measured"
        if self._set_target and not stopped and self._gains:
            target = round(statistics.median(self._gains), 2)
            try:
                library_db.save_level_target(library.db_path, target)
            except sqlite3.Error as error:
                target = None
                message += f". The library volume could not be saved: {error}"
        if stopped:
            message += ". Stopped: the rest can be done later"
        d.levelStopButton.setEnabled(False)
        self._final_message = message
        if self._saving:
            d.levelProgress.hide()
            self._report_saving()
        else:
            self._publish_final()
