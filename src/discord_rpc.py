"""Discord Rich Presence, over Discord's local IPC socket.

The Discord desktop app listens on a Unix socket called discord-ipc-0 (up to
discord-ipc-9). We connect, introduce ourselves with our application id, and
then send SET_ACTIVITY commands. Every message is an 8-byte header (an opcode
and a length, both little-endian 32-bit integers) followed by JSON.

Everything runs on the GUI thread using Qt's QLocalSocket, so nothing blocks
and no extra Python package is needed.
"""

import json
import os
import struct
import sys
import time
import uuid
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalSocket

# The key of an image you upload under "Rich Presence > Art Assets" in the
# Discord Developer Portal. Leave it empty to show the application's icon.
LARGE_IMAGE_KEY = "mh"

# What Discord shows in the member list: 0 = "Listening to MusicHive",
# 1 = the artist, 2 = the title.
STATUS_DISPLAY_TYPE = 0

ACTIVITY_LISTENING = 2  # "Listening to ..."

OP_HANDSHAKE, OP_FRAME, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 3, 4

MIN_UPDATE_INTERVAL = 15.0  # seconds between presence updates (Discord's limit)
SETTLE_DELAY = 0.7  # wait a moment so title, position and length are all known
RETRY_INTERVAL = 30.0  # how often to look for Discord while it is not running
MAX_FIELD_BYTES = 128  # longest text Discord accepts on one presence line


def make_activity(
    title: str,
    artist: str,
    album: str,
    position_ms: int,
    duration_ms: int,
    paused: bool,
) -> dict:
    """The presence for a track: title, artist and, while playing, a progress bar."""
    activity: dict = {"type": ACTIVITY_LISTENING, "details": _field(title)}

    if paused:
        activity["state"] = _field(f"{artist} (paused)" if artist else "Paused")
    else:
        if artist:
            activity["state"] = _field(artist)
        # A start and an end time (in seconds) make Discord draw a progress bar.
        start = int(time.time()) - position_ms // 1000
        timestamps = {"start": start}
        if duration_ms > 0:
            timestamps["end"] = start + duration_ms // 1000
        activity["timestamps"] = timestamps

    if LARGE_IMAGE_KEY:
        assets = {"large_image": LARGE_IMAGE_KEY}
        if album:
            assets["large_text"] = _field(album)  # shown when hovering the image
        activity["assets"] = assets

    if STATUS_DISPLAY_TYPE:
        activity["status_display_type"] = STATUS_DISPLAY_TYPE
    return activity


def _field(text: str) -> str:
    """Make `text` fit a presence line: a single line of 2 to 128 bytes."""
    text = " ".join(text.split())
    if len(text) < 2:
        text = text.ljust(2, "\u00a0")  # Discord wants at least two characters
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_FIELD_BYTES:
        return text
    return encoded[: MAX_FIELD_BYTES - 3].decode("utf-8", errors="ignore") + "…"


def _socket_paths() -> list[str]:
    """Where Discord's socket might be: native, Flatpak and Snap installs."""
    override = os.environ.get("DISCORD_IPC_PATH")  # lets you point at one socket
    if override:
        return [override]

    bases: list[str] = []
    for variable in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
        value = os.environ.get(variable)
        if value and value not in bases:
            bases.append(value)
    if "/tmp" not in bases:
        bases.append("/tmp")

    paths = []
    for subfolder in ("", "app/com.discordapp.Discord", "snap.discord"):
        for base in bases:
            for number in range(10):
                paths.append(os.path.join(base, subfolder, f"discord-ipc-{number}"))
    return paths


class DiscordPresence(QObject):
    """Keeps Discord's Rich Presence in step with what MusicHive is playing.

    `provider` is called whenever an update is about to be sent. It returns
    the activity to show (see make_activity), or None to show nothing.
    """

    status_message = Signal(str)  # something worth telling the user

    def __init__(
        self,
        client_id: str,
        provider: Callable[[], dict | None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._client_id = client_id
        self._provider = provider
        self._enabled = False
        self._ready = False  # True once Discord has answered our handshake
        self._blocked = False  # Discord refused us (for example a wrong application id)
        self._buffer = bytearray()
        self._last_send = 0.0
        self._reported_missing = False

        self._socket = QLocalSocket(self)
        self._socket.readyRead.connect(self._on_ready_read)
        self._socket.disconnected.connect(self._on_disconnected)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._send_now)

    # ----- public API -----

    def set_enabled(self, enabled: bool) -> None:
        """Turn the presence on or off. Off removes it from Discord."""
        self._enabled = enabled
        if enabled:
            self._blocked = False
            self._reported_missing = False
            self.request_update()
        else:
            self._timer.stop()
            self._disconnect()

    def request_update(self) -> None:
        """Something changed: send the current state soon, but never more
        often than Discord allows."""
        if not self._enabled or self._blocked:
            return
        since_last = time.monotonic() - self._last_send
        wait = max(SETTLE_DELAY, MIN_UPDATE_INTERVAL - since_last)
        wait_ms = int(wait * 1000)
        if self._timer.isActive() and self._timer.remainingTime() <= wait_ms:
            return  # an update is already coming up at least as soon
        self._timer.start(wait_ms)

    def shutdown(self) -> None:
        self.set_enabled(False)

    # ----- sending -----

    def _send_now(self) -> None:
        if not self._enabled or self._blocked:
            return
        activity = self._provider()
        connected = self._socket.state() == QLocalSocket.LocalSocketState.ConnectedState
        if activity is None and not connected:
            return  # nothing to show and nothing to remove

        if not connected and not self._connect():
            if not self._reported_missing:
                self._reported_missing = True
                #self.status_message.emit(
                #    "Discord was not found. Rich Presence will appear once Discord is running."
                #)
            self._timer.start(int(RETRY_INTERVAL * 1000))
            return
        if not self._ready:
            return  # Discord's READY answer is on its way; it calls us again

        self._write_frame(OP_FRAME, self._command(activity))
        self._last_send = time.monotonic()

    @staticmethod
    def _command(activity: dict | None) -> dict:
        return {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(uuid.uuid4()),
        }

    def _connect(self) -> bool:
        """Open the connection to Discord and send our handshake."""
        socket = self._socket
        socket.abort()
        for path in _socket_paths():
            if not os.path.exists(path):
                continue
            socket.connectToServer(path)
            if socket.waitForConnected(300):
                self._ready = False
                self._buffer.clear()
                self._write_frame(OP_HANDSHAKE, {"v": 1, "client_id": self._client_id})
                return True
            socket.abort()
        return False

    def _disconnect(self) -> None:
        """Remove our presence and close the connection."""
        socket = self._socket
        if socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            if self._ready:
                self._write_frame(OP_FRAME, self._command(None))
            socket.flush()
            socket.disconnectFromServer()
            socket.waitForDisconnected(300)
        self._ready = False
        self._buffer.clear()

    def _write_frame(self, opcode: int, payload) -> None:
        if isinstance(payload, bytes):
            body = payload
        else:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._socket.write(struct.pack("<II", opcode, len(body)) + body)

    # ----- receiving -----

    def _on_ready_read(self) -> None:
        self._buffer += bytes(self._socket.readAll())
        while len(self._buffer) >= 8:
            opcode, length = struct.unpack_from("<II", self._buffer)
            if len(self._buffer) < 8 + length:
                break  # the rest of this message has not arrived yet
            body = bytes(self._buffer[8 : 8 + length])
            del self._buffer[: 8 + length]
            self._handle_frame(opcode, body)

    def _handle_frame(self, opcode: int, body: bytes) -> None:
        try:
            message = json.loads(body) if body else {}
        except ValueError:
            message = {}
        if not isinstance(message, dict):
            message = {}

        if opcode == OP_PING:
            self._write_frame(OP_PONG, body)
        elif opcode == OP_CLOSE:
            text = message.get("message", "no reason given")
            print(f"MusicHive: Discord closed the connection: {text}", file=sys.stderr)
            self.status_message.emit(f"Discord closed the connection: {text}")
            self._blocked = True  # do not keep retrying (for example a wrong id)
            self._socket.abort()
        elif opcode == OP_FRAME:
            event = message.get("evt")
            if event == "READY":
                self._ready = True
                self.status_message.emit("Discord Rich Presence connected")
                self._send_now()
            elif event == "ERROR":
                data = message.get("data") or {}
                text = data.get("message", "unknown error")
                print(f"MusicHive: Discord rejected the presence: {text}", file=sys.stderr)
                self.status_message.emit(f"Discord rejected the presence: {text}")

    def _on_disconnected(self) -> None:
        self._ready = False
        if self._enabled and not self._blocked:
            self._timer.start(int(RETRY_INTERVAL * 1000))  # Discord may come back