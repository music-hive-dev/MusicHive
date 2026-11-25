"""Casting with pictures: the sound and the TV screen as one live video.

    player decks ──sound──> audiomixer ──> audio encoder ─┐
    TV screen (cast_screen.py) ──pictures──> video encoder ┴─> container ──> devices

While a video is cast, the player plays as it does on the computer, with the
equaliser, crossfade and level volume, but its sound goes into this stream
instead of to the speakers. The pictures are drawn at the moment their sound
goes in, so on the TV they stay together, however long the TV buffers.

Chromecasts get WebM (VP8 and Opus), which GStreamer can always make. DLNA TVs
get MPEG transport stream with H.264 (or MPEG-2 if no H.264 encoder is
installed) and AAC (or MP3), which nearly every TV plays.
"""

import queue
import threading
import time
from collections.abc import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

from PySide6.QtGui import QImage  # noqa: E402

from src.cast import CHROMECAST, CastDevice, CastError, log  # noqa: E402
from src.i18n import tr  # noqa: E402

Gst.init(None)

WIDTH, HEIGHT = 1280, 720
FRAMES_PER_SECOND = 30
AUDIO_RATE = 48000
# What the decks hand over: the same format for both, so they can be mixed.
AUDIO_CAPS = f"audio/x-raw,format=F32LE,rate={AUDIO_RATE},channels=2,layout=interleaved"
_BYTES_PER_SAMPLE = 8  # two channels of 32-bit floats
DECKS = 2
KEYFRAME_SECONDS = 2  # how long a device that joins may wait for a whole picture
_MIX_LATENCY_NS = 200 * Gst.MSECOND  # how long the mixer waits for late sound
_ON_TIME_NS = 60 * Gst.MSECOND  # sound this far from "now" still follows on
_CLIENT_PIECES = 1024  # pieces waiting for a slow device before it is let go
_JOIN_WAIT_SECONDS = 5  # a device starts anyway after this, keyframe or not

_CHROMECAST_KIND = "video/webm"
_DLNA_KIND = "video/mpeg"


def _element(factory: str, **properties) -> str | None:
    """A pipeline description of `factory` with those of the properties it has,
    or None if the element is not installed."""
    element = Gst.ElementFactory.make(factory, None)
    if element is None:
        return None
    parts = [factory]
    for name, value in properties.items():
        name = name.replace("_", "-")
        if element.find_property(name) is not None:
            if isinstance(value, bool):
                value = "true" if value else "false"
            parts.append(f"{name}={value}")
    return " ".join(parts)


def _first(choices: list[tuple[str, dict, str]], name: str) -> str | None:
    """The first installed encoder of `choices`, given `name`, with what follows it."""
    for factory, properties, after in choices:
        description = _element(factory, **properties)
        if description is not None:
            return f"{description} name={name} {after}".strip()
    return None


def _webm_parts() -> tuple[str, str, str]:
    video = _first([(
        "vp8enc",
        dict(
            deadline=1,  # real time
            cpu_used=8,
            keyframe_max_dist=FRAMES_PER_SECOND * KEYFRAME_SECONDS,
            target_bitrate=3_000_000,
            end_usage="cbr",
            lag_in_frames=0,
            threads=4,
        ),
        "",
    )], "enc")
    audio = _first([("opusenc", dict(bitrate=192_000), "")], "aenc")
    if video is None or audio is None:
        raise CastError(tr("casting pictures needs GStreamer's vp8enc and opusenc"))
    return video, audio, "webmmux name=mux streamable=true"


def _transport_stream_parts() -> tuple[str, str, str]:
    keyframes = FRAMES_PER_SECOND * KEYFRAME_SECONDS
    h264 = "! h264parse config-interval=-1"
    video = _first([
        ("x264enc", dict(tune="zerolatency", speed_preset="veryfast",
                         key_int_max=keyframes, bitrate=4000), h264),
        ("openh264enc", dict(bitrate=4_000_000, gop_size=keyframes), h264),
        ("vah264enc", dict(bitrate=4000, key_int_max=keyframes), h264),
        ("vaapih264enc", dict(bitrate=4000, keyframe_period=keyframes), h264),
        ("avenc_mpeg2video", dict(bitrate=8_000_000, gop_size=keyframes), "! mpegvideoparse"),
    ], "enc")
    audio = _first([
        ("fdkaacenc", dict(bitrate=192_000), "! aacparse"),
        ("avenc_aac", dict(bitrate=192_000), "! aacparse"),
        ("voaacenc", dict(bitrate=192_000), "! aacparse"),
        ("lamemp3enc", dict(target="bitrate", bitrate=192, cbr=True), "! mpegaudioparse"),
    ], "aenc")
    if video is None:
        raise CastError(
            tr(
                "casting pictures to this TV needs a video encoder: install "
                "gstreamer1-plugin-openh264 or gstreamer1-plugin-libav"
            )
        )
    if audio is None:
        raise CastError(tr("casting pictures to this TV needs an AAC or MP3 encoder"))
    return video, audio, "mpegtsmux name=mux alignment=7"


class _Client:
    """One device's connection: what is waiting to be sent to it."""

    def __init__(self) -> None:
        self.pieces: queue.Queue = queue.Queue(maxsize=_CLIENT_PIECES)
        self.joined = time.monotonic()
        self.waiting = True  # for a piece that starts with a whole picture
        self.dropped = False

    def offer(self, data: bytes, starts_picture: bool, header: bool) -> None:
        if self.dropped:
            return
        if self.waiting and not header:
            if not starts_picture and time.monotonic() - self.joined < _JOIN_WAIT_SECONDS:
                return  # a picture that builds on one it never got
            self.waiting = False
        try:
            self.pieces.put_nowait(data)
        except queue.Full:
            self.dropped = True  # it cannot keep up; it is hung up on


class VideoStream:
    """The live video for one device. Feed it with push_audio() and push_frame()."""

    def __init__(self, device: CastDevice) -> None:
        if device.kind == CHROMECAST:
            video, audio, mux = _webm_parts()
            self.kind = _CHROMECAST_KIND
        else:
            video, audio, mux = _transport_stream_parts()
            self.kind = _DLNA_KIND
        inputs = " ".join(
            f'appsrc name=audio{i} is-live=true format=time do-timestamp=false '
            f'caps="{AUDIO_CAPS}" ! queue ! mix. '
            for i in range(DECKS)
        )
        description = (
            f"{inputs}"
            # Silence underneath, so there is always sound, also while paused.
            # (Caps between links take no quotes; only property values may have them.)
            f"audiotestsrc is-live=true wave=silence ! {AUDIO_CAPS} ! mix. "
            f"audiomixer name=mix latency={_MIX_LATENCY_NS} "
            f"! audioconvert ! audioresample ! {audio} ! queue ! mux. "
            "appsrc name=video is-live=true format=time do-timestamp=true "
            f'caps="video/x-raw,format=BGRx,width={WIDTH},height={HEIGHT},'
            f'framerate={FRAMES_PER_SECOND}/1" '
            "! queue max-size-buffers=3 leaky=downstream ! videoconvert "
            f"! {video} ! queue ! mux. "
            f"{mux} ! appsink name=out sync=false emit-signals=true"
        )
        try:
            self._pipeline = Gst.parse_launch(description)
        except GLib.Error as error:
            raise CastError(tr("the video could not be set up: {message}").format(
                message=error.message,
            )) from None
        self._audio = [self._pipeline.get_by_name(f"audio{i}") for i in range(DECKS)]
        self._video = self._pipeline.get_by_name("video")
        self._encoder = self._pipeline.get_by_name("enc")
        self._bus = self._pipeline.get_bus()
        self._next_pts: list[int | None] = [None] * DECKS
        self._lock = threading.Lock()
        self._clients: list[_Client] = []
        self._headers: list[bytes] = []  # sent first to every device that joins
        self._running = False
        self._noticed: set[str] = set()  # first sound, first picture, first output
        self._pipeline.get_by_name("out").connect("new-sample", self._on_output)
        log(f"video pipeline: {description}")

    def _notice(self, what: str) -> None:
        if what not in self._noticed:
            self._noticed.add(what)
            log(what)

    def start(self) -> None:
        self._running = True
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._running = False
            raise CastError(tr("the video could not be started"))

    def stop(self) -> None:
        self._running = False
        self._pipeline.set_state(Gst.State.NULL)
        with self._lock:
            for client in self._clients:
                client.dropped = True

    def take_error(self) -> str:
        """A problem the encoders ran into since the last look, or ""."""
        message = self._bus.pop_filtered(Gst.MessageType.ERROR)
        if message is None:
            return ""
        error, details = message.parse_error()
        log(f"video error: {error.message} ({details})")
        return error.message

    # ----- what goes in -----

    def _running_time(self) -> int | None:
        clock = self._pipeline.get_clock()
        if clock is None:
            return None
        return clock.get_time() - self._pipeline.get_base_time()

    def push_audio(self, deck: int, data: bytes) -> None:
        """Sound from a deck, as it plays (called from GStreamer's threads).

        Its time follows on from the deck's last sound, so that small delays in
        getting here do not leave gaps; after a pause it starts again at now.
        """
        if not self._running or not data:
            return
        now = self._running_time()
        if now is None:
            return
        duration = len(data) // _BYTES_PER_SAMPLE * Gst.SECOND // AUDIO_RATE
        with self._lock:
            pts = self._next_pts[deck]
            if pts is None or not now - _ON_TIME_NS <= pts <= now + _MIX_LATENCY_NS:
                pts = now
            self._next_pts[deck] = pts + duration
        self._notice("the video gets sound from the player")
        buffer = Gst.Buffer.new_wrapped(data)
        buffer.pts = pts
        buffer.duration = duration
        self._audio[deck].emit("push-buffer", buffer)

    def push_frame(self, image: QImage) -> None:
        """A picture of the TV screen, drawn now (WIDTH × HEIGHT, Format_RGB32)."""
        if not self._running:
            return
        self._notice("the video gets pictures of the TV screen")
        data = bytes(image.constBits())[: WIDTH * HEIGHT * 4]
        self._video.emit("push-buffer", Gst.Buffer.new_wrapped(data))

    # ----- what comes out -----

    def _on_output(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        data = buffer.extract_dup(0, buffer.get_size())
        header = buffer.has_flags(Gst.BufferFlags.HEADER)
        starts_picture = not buffer.has_flags(Gst.BufferFlags.DELTA_UNIT)
        self._notice("the video is being made")
        if header:
            self._notice("the video has its header")
        with self._lock:
            if header:
                self._headers.append(data)
            for client in self._clients:
                client.offer(data, starts_picture, header)
        return Gst.FlowReturn.OK

    def serve(self, write: Callable[[bytes], object]) -> None:
        """Send the video to one device until it hangs up or casting stops
        (MediaServer calls this for each connection)."""
        client = _Client()
        with self._lock:
            headers = list(self._headers)
            self._clients.append(client)
        self._ask_for_whole_picture()
        try:
            for header in headers:
                write(header)
            while self._running and not client.dropped:
                try:
                    data = client.pieces.get(timeout=0.5)
                except queue.Empty:
                    continue
                write(data)
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)

    def _ask_for_whole_picture(self) -> None:
        """A device that joins can start straight away from the next picture."""
        pad = self._encoder.get_static_pad("src") if self._encoder is not None else None
        if pad is not None:
            pad.send_event(
                GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
            )
