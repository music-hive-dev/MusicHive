"""MPRIS: lets desktop widgets show and control the player, over D-Bus.

Like discord_rpc.py this talks to a local socket on the GUI thread, so nothing
blocks and no extra Python package is needed. D-Bus messages are a small binary
format described by type signatures such as "s" (text) or "a{sv}" (a dictionary
of variants); _Writer and _Reader below turn Python values into it and back.

A variant is written as (signature, value), for example ("x", 1234).
"""

import hashlib
import os
import struct
import sys
import time
from pathlib import Path
from urllib.parse import unquote

from PySide6.QtCore import QObject, QStandardPaths, QUrl, Signal
from PySide6.QtNetwork import QLocalSocket

BUS_NAME = "org.mpris.MediaPlayer2.musichive"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
ROOT_INTERFACE = "org.mpris.MediaPlayer2"
PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"  # the spec's "nothing playing" id

# A position this far (in ms) from where playback should be counts as a seek.
SEEK_TOLERANCE_MS = 1500

_METHOD_CALL, _METHOD_RETURN, _ERROR, _SIGNAL = 1, 2, 3, 4
_FLAG_NO_REPLY = 1
_FIELD_PATH, _FIELD_INTERFACE, _FIELD_MEMBER, _FIELD_ERROR = 1, 2, 3, 4
_FIELD_REPLY_SERIAL, _FIELD_DESTINATION, _FIELD_SENDER, _FIELD_SIGNATURE = 5, 6, 7, 8
_BUS_DAEMON = "org.freedesktop.DBus"
_REQUEST_NAME_DO_NOT_QUEUE = 4

_FIXED_FORMATS = {
    "y": "B", "b": "I", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q", "t": "Q", "d": "d",
}
_ALIGNMENT = {
    "y": 1, "g": 1, "v": 1, "n": 2, "q": 2, "b": 4, "i": 4, "u": 4,
    "s": 4, "o": 4, "a": 4, "x": 8, "t": 8, "d": 8, "(": 8, "{": 8,
}

_ROOT_PROPERTIES = {
    "CanQuit": ("b", True),
    "CanRaise": ("b", True),
    "HasTrackList": ("b", False),
    "Identity": ("s", "MusicHive"),
    "DesktopEntry": ("s", "musichive"),
    "SupportedUriSchemes": ("as", []),
    "SupportedMimeTypes": ("as", []),
}
_PLAYER_SIGNATURES = {
    "PlaybackStatus": "s",
    "LoopStatus": "s",
    "Rate": "d",
    "Shuffle": "b",
    "Metadata": "a{sv}",
    "Volume": "d",
    "MinimumRate": "d",
    "MaximumRate": "d",
    "CanGoNext": "b",
    "CanGoPrevious": "b",
    "CanPlay": "b",
    "CanPause": "b",
    "CanSeek": "b",
    "CanControl": "b",
}

_INTROSPECTION = """<node>
 <interface name="org.freedesktop.DBus.Introspectable">
  <method name="Introspect"><arg name="xml" type="s" direction="out"/></method>
 </interface>
 <interface name="org.freedesktop.DBus.Peer">
  <method name="Ping"/>
 </interface>
 <interface name="org.freedesktop.DBus.Properties">
  <method name="Get">
   <arg name="interface" type="s" direction="in"/>
   <arg name="property" type="s" direction="in"/>
   <arg name="value" type="v" direction="out"/>
  </method>
  <method name="GetAll">
   <arg name="interface" type="s" direction="in"/>
   <arg name="properties" type="a{sv}" direction="out"/>
  </method>
  <method name="Set">
   <arg name="interface" type="s" direction="in"/>
   <arg name="property" type="s" direction="in"/>
   <arg name="value" type="v" direction="in"/>
  </method>
  <signal name="PropertiesChanged">
   <arg name="interface" type="s"/>
   <arg name="changed" type="a{sv}"/>
   <arg name="invalidated" type="as"/>
  </signal>
 </interface>
 <interface name="org.mpris.MediaPlayer2">
  <method name="Raise"/>
  <method name="Quit"/>
  <property name="CanQuit" type="b" access="read"/>
  <property name="CanRaise" type="b" access="read"/>
  <property name="HasTrackList" type="b" access="read"/>
  <property name="Identity" type="s" access="read"/>
  <property name="DesktopEntry" type="s" access="read"/>
  <property name="SupportedUriSchemes" type="as" access="read"/>
  <property name="SupportedMimeTypes" type="as" access="read"/>
 </interface>
 <interface name="org.mpris.MediaPlayer2.Player">
  <method name="Next"/>
  <method name="Previous"/>
  <method name="Pause"/>
  <method name="PlayPause"/>
  <method name="Stop"/>
  <method name="Play"/>
  <method name="Seek"><arg name="offset" type="x" direction="in"/></method>
  <method name="SetPosition">
   <arg name="track" type="o" direction="in"/>
   <arg name="position" type="x" direction="in"/>
  </method>
  <method name="OpenUri"><arg name="uri" type="s" direction="in"/></method>
  <signal name="Seeked"><arg name="position" type="x"/></signal>
  <property name="PlaybackStatus" type="s" access="read"/>
  <property name="LoopStatus" type="s" access="readwrite"/>
  <property name="Rate" type="d" access="readwrite"/>
  <property name="Shuffle" type="b" access="readwrite"/>
  <property name="Metadata" type="a{sv}" access="read"/>
  <property name="Volume" type="d" access="readwrite"/>
  <property name="Position" type="x" access="read"/>
  <property name="MinimumRate" type="d" access="read"/>
  <property name="MaximumRate" type="d" access="read"/>
  <property name="CanGoNext" type="b" access="read"/>
  <property name="CanGoPrevious" type="b" access="read"/>
  <property name="CanPlay" type="b" access="read"/>
  <property name="CanPause" type="b" access="read"/>
  <property name="CanSeek" type="b" access="read"/>
  <property name="CanControl" type="b" access="read"/>
 </interface>
</node>
"""


def file_url(path: str | Path) -> str:
    """A properly encoded file:// address."""
    return bytes(QUrl.fromLocalFile(str(path)).toEncoded()).decode("ascii")


def cover_file_url(data: bytes) -> str:
    """Save cover image bytes where widgets can read them, and return the address.

    MPRIS passes pictures as a file address, and most covers are inside the music
    files. Each picture gets its own name, because widgets cache by address.
    """
    cache = Path(QStandardPaths.writableLocation(QStandardPaths.CacheLocation)) / "mpris"
    suffix = ".png" if data.startswith(b"\x89PNG") else ".jpg"
    target = cache / f"{hashlib.sha1(data).hexdigest()}{suffix}"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        for old in cache.iterdir():
            if old != target:
                old.unlink(missing_ok=True)
        if not target.exists():
            target.write_bytes(data)
    except OSError:
        return ""
    return file_url(target)


# ----- the D-Bus wire format -----


def _type_end(signature: str, start: int) -> int:
    """Where the single complete type starting at `start` ends."""
    opening = signature[start]
    if opening == "a":
        return _type_end(signature, start + 1)
    if opening not in "({":
        return start + 1
    closing = ")" if opening == "(" else "}"
    depth = 0
    for index in range(start, len(signature)):
        if signature[index] == opening:
            depth += 1
        elif signature[index] == closing:
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError(f"unbalanced signature: {signature}")


def _split_types(signature: str) -> list[str]:
    """Split a signature into its complete types: "sa{sv}as" -> s, a{sv}, as."""
    types = []
    start = 0
    while start < len(signature):
        end = _type_end(signature, start)
        types.append(signature[start:end])
        start = end
    return types


class _Writer:
    def __init__(self) -> None:
        self.data = bytearray()

    def align(self, size: int) -> None:
        self.data.extend(bytes(-len(self.data) % size))

    def write(self, kind: str, value) -> None:
        code = kind[0]
        self.align(_ALIGNMENT[code])
        if code in _FIXED_FORMATS:
            self.data += struct.pack("<" + _FIXED_FORMATS[code], value)
        elif code in "so":
            text = value.encode("utf-8")
            self.data += struct.pack("<I", len(text)) + text + b"\0"
        elif code == "g":
            self.data += bytes([len(value)]) + value.encode("ascii") + b"\0"
        elif code == "v":
            signature, inner = value
            self.write("g", signature)
            self.write(signature, inner)
        elif code == "a":
            self._write_array(kind[1:], value)
        elif code in "({":
            for field_kind, field in zip(_split_types(kind[1:-1]), value):
                self.write(field_kind, field)
        else:
            raise ValueError(f"cannot write type {kind}")

    def _write_array(self, element: str, value) -> None:
        length_at = len(self.data)
        self.data += bytes(4)
        self.align(_ALIGNMENT[element[0]])  # this padding is not part of the length
        start = len(self.data)
        for item in value.items() if isinstance(value, dict) else value:
            self.write(element, item)
        struct.pack_into("<I", self.data, length_at, len(self.data) - start)


class _Reader:
    def __init__(self, data: bytes, order: str, position: int = 0) -> None:
        self.data = data
        self.order = order  # "<" or ">"
        self.position = position

    def align(self, size: int) -> None:
        self.position += -self.position % size

    def read(self, kind: str):
        code = kind[0]
        self.align(_ALIGNMENT[code])
        if code in _FIXED_FORMATS:
            value = struct.unpack_from(self.order + _FIXED_FORMATS[code], self.data, self.position)[0]
            self.position += struct.calcsize(_FIXED_FORMATS[code])
            return bool(value) if code == "b" else value
        if code in "so":
            return self._read_text(self.read("u"))
        if code == "g":
            self.position += 1
            return self._read_text(self.data[self.position - 1])
        if code == "v":
            return self.read(self.read("g"))
        if code == "a":
            return self._read_array(kind[1:])
        if code in "({":
            return tuple(self.read(field) for field in _split_types(kind[1:-1]))
        raise ValueError(f"cannot read type {kind}")

    def _read_text(self, length: int) -> str:
        text = self.data[self.position : self.position + length].decode("utf-8", errors="replace")
        self.position += length + 1  # the terminating zero
        return text

    def _read_array(self, element: str):
        length = self.read("u")
        self.align(_ALIGNMENT[element[0]])
        end = self.position + length
        items = []
        while self.position < end:
            items.append(self.read(element))
        return dict(items) if element[0] == "{" else items


def _session_bus_path() -> str | None:
    """Where the session bus socket is, or None if it cannot be found."""
    for entry in os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").split(";"):
        transport, _, options = entry.partition(":")
        if transport == "unix":
            for option in options.split(","):
                key, _, value = option.partition("=")
                if key == "path":
                    return unquote(value)
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and os.path.exists(os.path.join(runtime_dir, "bus")):
        return os.path.join(runtime_dir, "bus")
    return None


# ----- the service -----


class MprisService(QObject):
    """Publishes the player on the session bus.

    The signals are what the desktop asks for. set_properties and set_position
    tell the desktop what is going on. If there is no session bus, nothing
    happens and the rest of the app is not affected.
    """

    raise_requested = Signal()
    quit_requested = Signal()
    next_requested = Signal()
    previous_requested = Signal()
    play_requested = Signal()
    pause_requested = Signal()
    play_pause_requested = Signal()
    stop_requested = Signal()
    seek_requested = Signal(int)  # the position to go to, in milliseconds
    shuffle_requested = Signal(bool)
    loop_requested = Signal(str)  # "None", "Track" or "Playlist"
    volume_requested = Signal(float)  # from 0.0 to 1.0

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._socket = QLocalSocket(self)
        self._socket.readyRead.connect(self._on_ready_read)
        self._buffer = bytearray()
        self._authenticated = False
        self._ready = False
        self._serial = 0
        self._position_ms = 0
        self._position_time = time.monotonic()
        self._player = {
            "PlaybackStatus": "Stopped",
            "LoopStatus": "None",
            "Rate": 1.0,
            "Shuffle": False,
            "Metadata": {"mpris:trackid": ("o", NO_TRACK)},
            "Volume": 1.0,
            "MinimumRate": 1.0,
            "MaximumRate": 1.0,
            "CanGoNext": True,
            "CanGoPrevious": True,
            "CanPlay": True,
            "CanPause": True,
            "CanSeek": False,
            "CanControl": True,
        }

    # ----- public API -----

    def start(self) -> None:
        """Connect to the session bus and ask for our name."""
        path = _session_bus_path()
        if path is None:
            print("MusicHive: MPRIS is off (no session bus found)", file=sys.stderr)
            return
        self._socket.connectToServer(path)
        if not self._socket.waitForConnected(1000):
            print(f"MusicHive: MPRIS is off ({self._socket.errorString()})", file=sys.stderr)
            return
        uid = str(os.getuid()).encode("ascii").hex().encode("ascii")
        self._socket.write(b"\0AUTH EXTERNAL " + uid + b"\r\n")

    def shutdown(self) -> None:
        """Leave the bus; the desktop then knows the player has gone."""
        self._ready = False
        if self._socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            self._socket.flush()
            self._socket.disconnectFromServer()

    def set_properties(self, **values) -> None:
        """Update player properties, telling the desktop about those that changed."""
        changed = {
            name: value for name, value in values.items() if self._player.get(name) != value
        }
        if not changed:
            return
        self._player.update(changed)
        if "Metadata" in changed:  # a new track starting is not a seek
            self._position_ms = 0
            self._position_time = time.monotonic()
        if self._ready:
            variants = {name: self._variant(name) for name in changed}
            self._send_signal(
                PROPERTIES_INTERFACE,
                "PropertiesChanged",
                "sa{sv}as",
                (PLAYER_INTERFACE, variants, []),
            )

    def set_position(self, position_ms: int) -> None:
        """Note the playback position, and report it if it jumped.

        Widgets work out the position themselves while playing, so MPRIS only
        expects to be told about jumps.
        """
        now = time.monotonic()
        expected = self._position_ms
        if self._player["PlaybackStatus"] == "Playing":
            expected += (now - self._position_time) * 1000
        self._position_ms = position_ms
        self._position_time = now
        if self._ready and abs(position_ms - expected) > SEEK_TOLERANCE_MS:
            self._send_signal(PLAYER_INTERFACE, "Seeked", "x", (position_ms * 1000,))

    # ----- properties -----

    def _variant(self, name: str) -> tuple[str, object]:
        return (_PLAYER_SIGNATURES[name], self._player[name])

    def _properties(self, interface: str) -> dict[str, tuple[str, object]]:
        if interface == ROOT_INTERFACE:
            return dict(_ROOT_PROPERTIES)
        if interface == PLAYER_INTERFACE:
            properties = {name: self._variant(name) for name in _PLAYER_SIGNATURES}
            properties["Position"] = ("x", self._position_ms * 1000)
            return properties
        return {}

    def _set_property(self, name: str, value) -> None:
        if name == "LoopStatus" and value in ("None", "Track", "Playlist"):
            self.loop_requested.emit(value)
        elif name == "Shuffle":
            self.shuffle_requested.emit(bool(value))
        elif name == "Volume":
            self.volume_requested.emit(max(0.0, min(1.0, float(value))))

    # ----- sending -----

    def _send(self, message_type: int, fields: list, signature: str = "", values=()) -> None:
        body = _Writer()
        for kind, value in zip(_split_types(signature), values):
            body.write(kind, value)
        if signature:
            fields = [*fields, (_FIELD_SIGNATURE, ("g", signature))]
        self._serial += 1
        header = _Writer()
        header.data += struct.pack(
            "<BBBBII", ord("l"), message_type, 0, 1, len(body.data), self._serial
        )
        header.write("a(yv)", fields)
        header.align(8)
        self._socket.write(bytes(header.data) + bytes(body.data))

    def _call_bus(self, member: str, signature: str = "", values=()) -> None:
        self._send(
            _METHOD_CALL,
            [
                (_FIELD_PATH, ("o", "/org/freedesktop/DBus")),
                (_FIELD_INTERFACE, ("s", _BUS_DAEMON)),
                (_FIELD_MEMBER, ("s", member)),
                (_FIELD_DESTINATION, ("s", _BUS_DAEMON)),
            ],
            signature,
            values,
        )

    def _send_signal(self, interface: str, member: str, signature: str, values) -> None:
        self._send(
            _SIGNAL,
            [
                (_FIELD_PATH, ("o", OBJECT_PATH)),
                (_FIELD_INTERFACE, ("s", interface)),
                (_FIELD_MEMBER, ("s", member)),
            ],
            signature,
            values,
        )

    # ----- receiving -----

    def _on_ready_read(self) -> None:
        self._buffer += bytes(self._socket.readAll())
        if not self._authenticated and not self._finish_login():
            return
        while len(self._buffer) >= 16:
            order = "<" if self._buffer[0] == ord("l") else ">"
            body_length = struct.unpack_from(order + "I", self._buffer, 4)[0]
            fields_length = struct.unpack_from(order + "I", self._buffer, 12)[0]
            fields_end = 16 + fields_length
            total = fields_end + (-fields_end % 8) + body_length
            if len(self._buffer) < total:
                return  # the rest of this message has not arrived yet
            message = bytes(self._buffer[:total])
            del self._buffer[:total]
            try:
                self._handle_message(message, order)
            except (struct.error, ValueError, IndexError, KeyError, TypeError) as error:
                print(f"MusicHive: bad MPRIS message: {error}", file=sys.stderr)

    def _finish_login(self) -> bool:
        """Handle the bus's answer to our login. True once logged in."""
        end = self._buffer.find(b"\r\n")
        if end < 0:
            return False
        answer = bytes(self._buffer[:end])
        del self._buffer[: end + 2]
        if not answer.startswith(b"OK"):
            print(f"MusicHive: MPRIS is off (bus said {answer!r})", file=sys.stderr)
            self._socket.abort()
            return False
        self._authenticated = True
        self._socket.write(b"BEGIN\r\n")
        self._call_bus("Hello")
        self._call_bus("RequestName", "su", (BUS_NAME, _REQUEST_NAME_DO_NOT_QUEUE))
        self._ready = True
        return True

    def _handle_message(self, message: bytes, order: str) -> None:
        if message[1] != _METHOD_CALL:
            return  # replies to our calls and signals need no action
        reader = _Reader(message, order, 12)
        fields = dict(reader.read("a(yv)"))
        reader.align(8)
        arguments = [reader.read(kind) for kind in _split_types(fields.get(_FIELD_SIGNATURE, ""))]

        wants_reply = not message[2] & _FLAG_NO_REPLY
        serial = struct.unpack_from(order + "I", message, 8)[0]
        sender = fields.get(_FIELD_SENDER, "")
        reply_to = [(_FIELD_REPLY_SERIAL, ("u", serial)), (_FIELD_DESTINATION, ("s", sender))]

        def reply(signature: str = "", *values) -> None:
            if wants_reply:
                self._send(_METHOD_RETURN, reply_to, signature, values)

        def fail(name: str, text: str) -> None:
            if wants_reply:
                self._send(_ERROR, [*reply_to, (_FIELD_ERROR, ("s", name))], "s", (text,))

        member = fields.get(_FIELD_MEMBER, "")
        if fields.get(_FIELD_PATH) != OBJECT_PATH:
            fail("org.freedesktop.DBus.Error.UnknownObject", "No such object")
        elif not self._handle_call(member, arguments, reply):
            fail("org.freedesktop.DBus.Error.UnknownMethod", f"No method {member}")

    def _handle_call(self, member: str, arguments: list, reply) -> bool:
        """Carry out one method call. False if there is no such method."""
        commands = {
            "Raise": self.raise_requested,
            "Quit": self.quit_requested,
            "Next": self.next_requested,
            "Previous": self.previous_requested,
            "Pause": self.pause_requested,
            "PlayPause": self.play_pause_requested,
            "Stop": self.stop_requested,
            "Play": self.play_requested,
        }
        if member in commands:
            reply()  # before acting, as Quit ends the program
            commands[member].emit()
        elif member == "Seek":
            reply()
            self._seek_by(arguments[0])
        elif member == "SetPosition":
            reply()
            self._seek_to(arguments[0], arguments[1])
        elif member == "OpenUri":  # only songs in a library can be played
            reply()
        elif member == "Get":
            properties = self._properties(arguments[0])
            if arguments[1] not in properties:
                return False
            reply("v", properties[arguments[1]])
        elif member == "GetAll":
            reply("a{sv}", self._properties(arguments[0]))
        elif member == "Set":
            reply()
            self._set_property(arguments[1], arguments[2])
        elif member == "Introspect":
            reply("s", _INTROSPECTION)
        elif member == "Ping":
            reply()
        else:
            return False
        return True

    def _track_length_us(self) -> int:
        return self._player["Metadata"].get("mpris:length", ("x", 0))[1]

    def _seek_by(self, offset_us: int) -> None:
        """Seek relative to now. Past the end means the next track, says the spec."""
        target = self._position_ms * 1000 + offset_us
        length = self._track_length_us()
        if length and target >= length:
            self.next_requested.emit()
        else:
            self.seek_requested.emit(max(0, target) // 1000)

    def _seek_to(self, track_id: str, position_us: int) -> None:
        """Seek to a position, ignored if that track is no longer the current one."""
        current = self._player["Metadata"]["mpris:trackid"][1]
        length = self._track_length_us()
        if track_id == current and 0 <= position_us and (not length or position_us <= length):
            self.seek_requested.emit(position_us // 1000)
