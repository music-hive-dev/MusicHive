"""Listening to a radio station through MusicHive's own connection.

    reader thread ──> StreamBuffer (the last minutes of the station) ──> feeder thread ──> player
                                                                     └─> Tape (taping)

The reader downloads the stream, takes out the song titles the station sends
along (ICY metadata) and keeps the sound in a buffer. The feeder hands the
sound to the player at the pace it plays, so pausing the player simply leaves
the feeder waiting while the reader carries on: that is the time-shift. Titles
are remembered with their place in the sound, so a title change is announced
when it is heard, not when it is downloaded.
"""

import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

USER_AGENT = "MusicHive/1.0 (radio)"
CONNECT_TIMEOUT_SECONDS = 15
READ_BYTES = 16 * 1024
FEED_BYTES = 8 * 1024
RECONNECT_DELAYS = (1, 2, 4, 8, 15, 30)  # seconds between tries, the last one repeats
PLAYLIST_TYPES = ("audio/x-scpls", "audio/scpls", "audio/x-mpegurl", "audio/mpegurl")
HLS_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl")
SUFFIX_FOR_TYPE = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/aac": ".aac",
    "audio/aacp": ".aac",
    "audio/x-aac": ".aac",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/flac": ".flac",
}


class NotStreamable(Exception):
    """The address cannot be read by MusicHive itself (HLS): the player must play it."""


def _request(url: str, icy: bool = True):
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("Only http and https addresses can be played")
    headers = {"User-Agent": USER_AGENT}
    if icy:
        headers["Icy-MetaData"] = "1"
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=CONNECT_TIMEOUT_SECONDS
    )


def resolve_playlist(url: str, depth: int = 0) -> str:
    """The stream an address leads to: playlists (.pls, .m3u) are opened and their
    first entry is used. HLS (.m3u8) raises NotStreamable."""
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        raise NotStreamable(url)
    if not path.endswith((".pls", ".m3u")) or depth > 3:
        return url
    with _request(url, icy=False) as response:
        text = response.read(256 * 1024).decode("utf-8", "replace")
    if "#EXT-X-" in text:
        raise NotStreamable(url)
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("file") and "=" in line:
            line = line.split("=", 1)[1].strip()
        if line.startswith(("http://", "https://")):
            return resolve_playlist(line, depth + 1)
    raise ValueError("The playlist names no stream")


def parse_icy_title(metadata: bytes) -> str | None:
    """StreamTitle='Artist - Title'; from an ICY metadata block."""
    text = metadata.rstrip(b"\0").decode("utf-8", "replace")
    if "Ã" in text:  # it was Latin-1 after all
        text = metadata.rstrip(b"\0").decode("latin-1")
    match = re.search(r"StreamTitle='(.*?)';", text, re.S)
    return match.group(1).strip() if match else None


def split_title(title: str) -> tuple[str, str]:
    """ "Artist - Title" as (artist, title); ("", title) when it cannot be split."""
    for separator in (" - ", " – ", " — "):
        if separator in title:
            artist, song = title.split(separator, 1)
            return artist.strip(), song.strip()
    return "", title.strip()


@dataclass
class _Chunk:
    offset: int  # where it starts in the station's sound, counted from the first byte
    data: bytes
    arrived: float  # time.monotonic() when it was downloaded


class StreamBuffer:
    """The station's sound of the last few minutes, and where its titles change."""

    def __init__(self, keep_seconds: float) -> None:
        self.keep_seconds = keep_seconds
        self._chunks: deque[_Chunk] = deque()
        self._end = 0
        self.titles: list[tuple[int, str]] = []  # (offset, title), in order
        self._changed = threading.Condition()

    @property
    def start(self) -> int:
        with self._changed:
            return self._chunks[0].offset if self._chunks else self._end

    @property
    def end(self) -> int:
        with self._changed:
            return self._end

    def append(self, data: bytes) -> None:
        now = time.monotonic()
        with self._changed:
            self._chunks.append(_Chunk(self._end, data, now))
            self._end += len(data)
            # Keep only the last minutes (time-shift beyond that is not possible).
            while self._chunks and now - self._chunks[0].arrived > self.keep_seconds:
                self._chunks.popleft()
            first = self._chunks[0].offset if self._chunks else self._end
            # Titles before the buffer are forgotten, except the one still playing there.
            while len(self.titles) > 1 and self.titles[1][0] <= first:
                self.titles.pop(0)
            self._changed.notify_all()

    def add_title(self, title: str) -> None:
        with self._changed:
            if not self.titles or self.titles[-1][1] != title:
                self.titles.append((self._end, title))

    def read(self, position: int, most: int, wait: float) -> tuple[bytes, int]:
        """Up to `most` bytes from `position` on; waits up to `wait` seconds for some.
        Returns the bytes and where they start (later than `position` if that was
        already dropped from the buffer)."""
        with self._changed:
            if position >= self._end:
                self._changed.wait(wait)
            if not self._chunks or position >= self._end:
                return b"", position
            position = max(position, self._chunks[0].offset)
            parts = []
            needed = most
            for chunk in self._chunks:
                chunk_end = chunk.offset + len(chunk.data)
                if chunk_end <= position:
                    continue
                begin = max(0, position - chunk.offset)
                piece = chunk.data[begin:begin + needed]
                parts.append(piece)
                needed -= len(piece)
                if needed <= 0:
                    break
            return b"".join(parts), position

    def arrival_of(self, position: int) -> float | None:
        """When the sound at `position` was downloaded."""
        with self._changed:
            for chunk in self._chunks:
                if chunk.offset + len(chunk.data) > position:
                    return chunk.arrived
            return None

    def title_at(self, position: int) -> tuple[int, str] | None:
        """The title that is playing at `position`, and where it began."""
        with self._changed:
            found = None
            for offset, title in self.titles:
                if offset > position:
                    break
                found = (offset, title)
            return found

    def wake(self) -> None:
        with self._changed:
            self._changed.notify_all()


class Tape:
    """Writes the sound that is heard to files, one per song or one for everything."""

    def __init__(self, folder: Path, station: str, suffix: str, per_song: bool,
                 on_file_done: Callable[[Path, str], None]) -> None:
        self._folder = folder
        self._station = station
        self._suffix = suffix
        self._per_song = per_song
        self._on_file_done = on_file_done
        self._file = None
        self._path: Path | None = None
        self._title = ""
        self.written = 0

    def write(self, data: bytes, title: str) -> None:
        if self._file is None or (self._per_song and title != self._title):
            self._close()
            self._open(title)
        self._file.write(data)
        self.written += len(data)

    def _open(self, title: str) -> None:
        self._title = title
        stamp = time.strftime("%Y-%m-%d %H-%M-%S")
        name = f"{title} ({self._station})" if self._per_song and title else f"{self._station} {stamp}"
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name).strip(" .")[:150] or "Tape"
        self._folder.mkdir(parents=True, exist_ok=True)
        path = self._folder / f"{name}{self._suffix}"
        number = 2
        while path.exists():
            path = self._folder / f"{name} ({number}){self._suffix}"
            number += 1
        self._path = path
        self._file = open(path, "wb")

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._on_file_done(self._path, self._title)
            self._file = None

    def stop(self) -> None:
        self._close()


class RadioStream(QObject):
    """One station: its connection, its buffer and what goes to the player."""

    title_heard = Signal(str)  # the title the listener hears now changed ("" for none)
    status = Signal(str)  # connecting, reconnecting… for the status bar
    info_ready = Signal(dict)  # the station's own details: type, bitrate, name
    failed = Signal(str)  # it cannot be played at all
    tape_file_done = Signal(object, str)  # a tape file was finished: its Path, the title

    def __init__(self, url: str, station: str, keep_seconds: float) -> None:
        super().__init__()
        self.url = url
        self.station = station
        self.buffer = StreamBuffer(keep_seconds)
        self.content_type = ""
        self._running = threading.Event()
        self._running.set()
        self._reader = threading.Thread(target=self._read_loop, name="radio reader", daemon=True)
        self._feeder: threading.Thread | None = None
        self._appsrc = None
        self._feed_position = 0
        self._feed_generation = 0  # a new feeder makes the old one stop
        self._lock = threading.Lock()
        self._heard_title: str | None = None
        self._tape: Tape | None = None
        self._tape_position = 0  # what has been taped, up to the feeder
        self._tape_lock = threading.Lock()  # the window starts and stops it, the feeder writes
        self._connected = threading.Event()

    # ----- the connection -----

    def start(self) -> None:
        self._reader.start()

    def stop(self) -> None:
        self._running.clear()
        self.stop_taping()
        self.buffer.wake()
        with self._lock:
            self._feed_generation += 1
            self._appsrc = None

    def _read_loop(self) -> None:
        attempt = 0
        url = self.url
        while self._running.is_set():
            try:
                url = resolve_playlist(self.url)
                with _request(url) as response:
                    kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    if kind in HLS_TYPES:
                        raise NotStreamable(url)
                    if kind in PLAYLIST_TYPES:
                        raise ValueError("The station sent a playlist instead of sound")
                    self.content_type = kind
                    self.info_ready.emit({
                        "type": kind,
                        "bitrate": response.headers.get("icy-br") or "",
                        "name": response.headers.get("icy-name") or "",
                    })
                    if attempt:
                        self.status.emit(f"{self.station}: connected again")
                    attempt = 0
                    self._connected.set()
                    self._read_from(response, int(response.headers.get("icy-metaint") or 0))
            except NotStreamable:
                self.failed.emit("hls")
                return
            except (urllib.error.URLError, OSError, ValueError) as error:
                if not self._running.is_set():
                    return
                if not self._connected.is_set() and attempt >= 2:
                    self.failed.emit(str(getattr(error, "reason", None) or error))
                    return
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                self.status.emit(f"{self.station}: connection lost, trying again in {delay} s")
                attempt += 1
                for _ in range(delay * 10):
                    if not self._running.is_set():
                        return
                    time.sleep(0.1)
            else:
                if self._running.is_set():  # the station ended the stream: reconnect
                    attempt += 1
                    time.sleep(1)

    def _read_from(self, response, metaint: int) -> None:
        """Copy sound into the buffer, taking out the title blocks every `metaint` bytes."""
        until_meta = metaint
        while self._running.is_set():
            if metaint:
                data = response.read(min(READ_BYTES, until_meta))
                if not data:
                    return
                self.buffer.append(data)
                until_meta -= len(data)
                if until_meta == 0:
                    length_byte = response.read(1)
                    if not length_byte:
                        return
                    length = length_byte[0] * 16
                    metadata = b""
                    while len(metadata) < length:
                        more = response.read(length - len(metadata))
                        if not more:
                            return
                        metadata += more
                    title = parse_icy_title(metadata) if metadata else None
                    if title is not None:
                        self.buffer.add_title(title)
                    until_meta = metaint
            else:
                data = response.read(READ_BYTES)
                if not data:
                    return
                self.buffer.append(data)

    # ----- feeding the player -----

    def attach(self, appsrc) -> None:
        """The player's source asks for sound: start (or restart) feeding it."""
        appsrc.set_property("block", True)
        appsrc.set_property("max-bytes", 64 * 1024)  # a few seconds; keeps pausing exact
        appsrc.set_property("stream-type", 0)  # a stream, not seekable
        with self._lock:
            self._feed_generation += 1
            generation = self._feed_generation
            self._appsrc = appsrc
        self._feeder = threading.Thread(
            target=self._feed_loop, args=(appsrc, generation), name="radio feeder", daemon=True
        )
        self._feeder.start()

    def go_live(self) -> None:
        """Skip the time-shift: the next sound fed is the newest there is."""
        with self._lock:
            self._feed_position = self.buffer.end

    def _feed_loop(self, appsrc, generation: int) -> None:
        from gi.repository import Gst  # the player has set GStreamer up already

        while self._running.is_set():
            with self._lock:
                if generation != self._feed_generation:
                    return
                position = self._feed_position
            data, start = self.buffer.read(position, FEED_BYTES, wait=0.5)
            if not data:
                continue
            result = appsrc.emit("push-buffer", Gst.Buffer.new_wrapped(data))  # waits while paused
            if result != Gst.FlowReturn.OK:
                return  # the player stopped or changed source
            with self._lock:
                if generation != self._feed_generation:
                    return
                if self._feed_position == position:  # not moved by go_live meanwhile
                    self._feed_position = start + len(data)
                end = self._feed_position
            self._after_feeding(start, data, end)

    def _after_feeding(self, start: int, data: bytes, end: int) -> None:
        heard = self.buffer.title_at(end)
        title = heard[1] if heard else ""
        if title != self._heard_title:
            self._heard_title = title
            self.title_heard.emit(title)
        with self._tape_lock:
            if self._tape is not None:
                self._tape_up_to(self._tape, end)

    def behind_seconds(self) -> float:
        """How far behind the live broadcast the listener is."""
        with self._lock:
            position = self._feed_position
        if position >= self.buffer.end:
            return 0.0
        arrived = self.buffer.arrival_of(position)
        return max(0.0, time.monotonic() - arrived) if arrived is not None else 0.0

    @property
    def heard_title(self) -> str:
        return self._heard_title or ""

    # ----- taping -----

    def start_taping(self, folder: Path, per_song: bool, from_song_start: bool) -> None:
        suffix = SUFFIX_FOR_TYPE.get(self.content_type, ".mp3")
        with self._lock:
            position = self._feed_position
        start = position
        if from_song_start:
            heard = self.buffer.title_at(position)
            if heard is not None:
                start = max(heard[0], self.buffer.start)
        with self._tape_lock:
            self._tape_position = start
            self._tape = Tape(folder, self.station, suffix, per_song, self.tape_file_done.emit)
            self._tape_up_to(self._tape, position)  # what was heard of the song already

    def _tape_up_to(self, tape: Tape, end: int) -> None:
        """Write from the tape's place up to `end`, starting a new file at each title."""
        while self._tape_position < end:
            data, start = self.buffer.read(self._tape_position, end - self._tape_position, wait=0)
            if not data:
                return
            heard = self.buffer.title_at(start)
            title = heard[1] if heard else ""
            # Stop the piece at the next title change, so each song gets its own file.
            next_change = next((o for o, _t in self.buffer.titles if o > start), None)
            if next_change is not None and start + len(data) > next_change:
                data = data[: next_change - start]
            try:
                tape.write(data, title)
            except OSError as error:
                self.status.emit(f"Taping stopped: {error}")
                self._tape = None
                return
            self._tape_position = start + len(data)

    def stop_taping(self) -> None:
        with self._tape_lock:
            tape, self._tape = self._tape, None
            if tape is not None:
                tape.stop()

    @property
    def taping(self) -> bool:
        return self._tape is not None
