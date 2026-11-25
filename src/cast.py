"""Casting: playing on a Chromecast, or on a TV or speaker that speaks DLNA (UPnP).

    MusicHive ──commands──> the device ──fetches the sound──> MusicHive's own little web server

The device plays the sound itself, so MusicHive's equaliser, crossfade and
visualiser are not part of it. Songs and downloaded episodes are offered by a
small web server that runs while something is cast; radio is passed on from
MusicHive's own connection to the station, so titles and taping keep working.
Streamed episodes go to the device by their web address.

Everything that talks to the network runs on worker threads: the window only
ever reads the latest status that a worker left behind.

Chromecasts need the optional PyChromecast package; DLNA needs nothing extra.
"""

import html
import json
import mimetypes
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from src.i18n import tr

try:
    import pychromecast
    from pychromecast.discovery import CastBrowser, SimpleCastListener
except ImportError:  # Chromecasts are optional; DLNA devices still work
    pychromecast = None

CHROMECAST = "chromecast"
DLNA = "dlna"
BROWSER = "browser"  # a web page MusicHive serves, opened in a TV's (or any) browser

SEARCH_SECONDS = 4.0  # how long a search listens for devices
POLL_SECONDS = 1.0  # how often a device is asked what it is doing
SETTLE_SECONDS = 1.5  # a device's status is not trusted this soon after a command
LOAD_TIMEOUT_SECONDS = 20  # a Chromecast that has not started by then will not
LOST_AFTER_FAILURES = 5  # this many failed polls in a row: the device is gone
NETWORK_TIMEOUT = 5
SERVER_PORTS = range(8471, 8481)  # tried in turn, so a firewall rule can name them
KEEP_SHARED = 9  # files and pictures the server keeps offering (current, previous…)
QUEUE_SLOTS = 10  # stand-ins on each side of what plays, so a Chromecast offers skipping
SILENCE_SECONDS = 60  # the stand-in for songs: quiet, and long enough to be replaced
COPY_BYTES = 64 * 1024
PAGE_FOLDER = Path(__file__).parent.parent / "assets" / "tv"  # the browser page's files
PAGE_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "tv.js": ("tv.js", "text/javascript; charset=utf-8"),
    "tv.css": ("tv.css", "text/css; charset=utf-8"),
}
WRONG_GUESSES_ALLOWED = 20  # an address that tries this many wrong page codes is ignored
MAX_STATUS_BYTES = 64 * 1024

AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".aac": "audio/aac",
    ".m3u8": "application/x-mpegURL",
}

_SSDP_ADDRESS = ("239.255.255.250", 1900)
_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
_DEVICE_NS = "{urn:schemas-upnp-org:device-1-0}"
# DLNA TVs look for these on the sound they fetch; some refuse to play without them.
_DLNA_FEATURES = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
_DLNA_LIVE_FEATURES = (  # no seeking: it is made as it plays
    "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
)


def log(message: str) -> None:
    """A line in the terminal about casting, to see where it goes wrong."""
    print(f"MusicHive cast: {message}", file=sys.stderr, flush=True)


class CastError(Exception):
    """A device refused or did not answer; the message is for the user."""


def audio_type(name: str) -> str:
    """The MIME type for a file name or web address, by its ending."""
    suffix = Path(urllib.parse.urlparse(name).path).suffix.lower()
    return AUDIO_TYPES.get(suffix) or mimetypes.guess_type(name)[0] or "audio/mpeg"


def image_type(data: bytes) -> str:
    return "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"


# ----- devices -----


@dataclass
class CastDevice:
    """A TV, speaker or Chromecast found on the network."""

    kind: str  # CHROMECAST, DLNA or BROWSER
    id: str  # stays the same for the device between searches
    name: str
    host: str  # its network address
    # DLNA: where to send commands, and the exact service names it uses
    transport_url: str = ""
    transport_type: str = ""
    rendering_url: str = ""
    rendering_type: str = ""
    # Chromecast: what discovery found, and the discovery it belongs to
    cast_info: object = field(default=None, repr=False, compare=False)
    zeroconf: object = field(default=None, repr=False, compare=False)
    # Browser: the number in the page's address (kept, so a bookmark keeps working)
    code: str = ""


class CastFinder(QObject):
    """Looks for devices to cast to. Chromecast discovery keeps running once
    started, because a connected Chromecast relies on it to find the device again."""

    found = Signal(list)  # the CastDevices found, sorted by name

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._browser = None
        self._zeroconf = None
        self._lock = threading.Lock()
        self._searching = False

    @property
    def chromecast_support(self) -> bool:
        return pychromecast is not None

    def search(self) -> bool:
        """Start a search; `found` follows after a few seconds. False if one is running."""
        with self._lock:
            if self._searching:
                return False
            self._searching = True
        threading.Thread(target=self._search, name="cast search", daemon=True).start()
        return True

    def _search(self) -> None:
        devices: list[CastDevice] = []
        try:
            chromecasts = threading.Thread(target=self._start_chromecast_discovery, daemon=True)
            chromecasts.start()
            devices.extend(_find_dlna_devices(SEARCH_SECONDS))
            chromecasts.join(SEARCH_SECONDS)
            devices.extend(self._chromecasts())
        finally:
            with self._lock:
                self._searching = False
            devices.sort(key=lambda d: d.name.casefold())
            self.found.emit(devices)

    def _start_chromecast_discovery(self) -> None:
        if pychromecast is None:
            return
        with self._lock:
            if self._browser is not None:
                return
        try:
            zconf = pychromecast.discovery.create_zeroconf()
            browser = CastBrowser(SimpleCastListener(), zconf)
            browser.start_discovery()
        except Exception:  # no network, or mDNS blocked: Chromecasts are simply not found
            return
        with self._lock:
            self._zeroconf, self._browser = zconf, browser
        time.sleep(SEARCH_SECONDS - 0.5)  # the first answers take a moment

    def _chromecasts(self) -> list[CastDevice]:
        with self._lock:
            browser, zconf = self._browser, self._zeroconf
        if browser is None:
            return []
        found = []
        for info in list(browser.devices.values()):
            found.append(CastDevice(
                kind=CHROMECAST,
                id=str(info.uuid),
                name=info.friendly_name or info.model_name or info.host,
                host=info.host,
                cast_info=info,
                zeroconf=zconf,
            ))
        return found

    def shutdown(self) -> None:
        with self._lock:
            browser, self._browser = self._browser, None
        if browser is not None:
            try:
                browser.stop_discovery()  # also closes its zeroconf
            except Exception:
                pass


def _find_dlna_devices(seconds: float) -> list[CastDevice]:
    """Ask the network for media renderers (SSDP), then read each one's description."""
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {_SSDP_ADDRESS[0]}:{_SSDP_ADDRESS[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {_RENDERER}\r\n\r\n"
    ).encode()
    locations: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.settimeout(0.5)
            for _ in range(2):  # UDP may be lost; devices ignore the repeat
                sock.sendto(message, _SSDP_ADDRESS)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    data, _sender = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                for line in data.decode("utf-8", "replace").split("\r\n"):
                    name, _, value = line.partition(":")
                    if name.strip().lower() == "location" and value.strip():
                        locations.add(value.strip())
    except OSError:
        return []
    devices: dict[str, CastDevice] = {}
    for location in locations:
        device = _read_description(location)
        if device is not None:
            devices[device.id] = device
    return list(devices.values())


def _read_description(location: str) -> CastDevice | None:
    try:
        with urllib.request.urlopen(location, timeout=NETWORK_TIMEOUT) as response:
            root = ElementTree.fromstring(response.read())
    except (OSError, ValueError, ElementTree.ParseError):
        return None
    base = (root.findtext(f"{_DEVICE_NS}URLBase") or "").strip() or location
    for node in root.iter(f"{_DEVICE_NS}device"):
        services = {}
        for service in node.iter(f"{_DEVICE_NS}service"):
            kind = (service.findtext(f"{_DEVICE_NS}serviceType") or "").strip()
            control = (service.findtext(f"{_DEVICE_NS}controlURL") or "").strip()
            if ":AVTransport:" in kind:
                services["transport"] = (urllib.parse.urljoin(base, control), kind)
            elif ":RenderingControl:" in kind:
                services["rendering"] = (urllib.parse.urljoin(base, control), kind)
        if "transport" not in services:
            continue
        rendering = services.get("rendering", ("", ""))
        return CastDevice(
            kind=DLNA,
            id=(node.findtext(f"{_DEVICE_NS}UDN") or location).strip(),
            name=(node.findtext(f"{_DEVICE_NS}friendlyName") or "").strip()
            or urllib.parse.urlparse(location).hostname
            or location,
            host=urllib.parse.urlparse(location).hostname or "",
            transport_url=services["transport"][0],
            transport_type=services["transport"][1],
            rendering_url=rendering[0],
            rendering_type=rendering[1],
        )
    return None


# ----- the web server the devices fetch from -----


def lan_address() -> str:
    """This computer's address on its main network (no packet is sent)."""
    return _local_address("10.254.254.254")


def _local_address(device_host: str) -> str:
    """This computer's address as the device sees it (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect((device_host, 9))
            return sock.getsockname()[0]
        except OSError:
            return socket.gethostbyname(socket.gethostname())


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def verify_request(self, request, client_address) -> bool:
        """Anyone but the device cast to is hung up on before a word is read
        (while a browser page is offered, anyone who may know its code is heard)."""
        if self.media.hears(client_address[0]):
            return True
        log(f"refused a connection from {client_address[0]} (not the device cast to)")
        return False


class MediaServer:
    """Offers files, pictures and radio to the device cast to, each at a
    hard-to-guess address.

    It runs only while casting, listens only on the network the device is on,
    and answers only that device. While a browser page is offered, a browser
    that opens the page's address (with its code) is let in as well.
    """

    def __init__(self) -> None:
        self._server: _Server | None = None
        self._shared: OrderedDict[str, tuple[object, str]] = OrderedDict()
        self._lock = threading.Lock()
        self._allowed: set[str] = set()  # the addresses of the devices cast to
        self._browser = None  # the BrowserSession whose page is offered
        self._code = ""  # the number in that page's address
        self._wrong_guesses: dict[str, int] = {}

    def allows(self, address: str) -> bool:
        with self._lock:
            return address in self._allowed

    def hears(self, address: str) -> bool:
        with self._lock:
            if address in self._allowed:
                return True
            return bool(self._code) and self._wrong_guesses.get(address, 0) < WRONG_GUESSES_ALLOWED

    def browser_for(self, code: str, address: str):
        """The BrowserSession whose page has this code, letting `address` in; or None
        (and a wrong guess counted)."""
        with self._lock:
            if self._code and secrets.compare_digest(code, self._code):
                self._allowed.add(address)
                return self._browser
            if address not in self._allowed:
                self._wrong_guesses[address] = self._wrong_guesses.get(address, 0) + 1
            return None

    def open_browser(self, session, code: str) -> str:
        """Offer the browser page at /<code>/; returns its address."""
        local = lan_address()
        server = self._start(local)
        with self._lock:
            self._browser, self._code = session, code
        return f"http://{local}:{server.server_address[1]}/{code}/"

    def page_address(self) -> str:
        server = self._server
        if server is None or not self._code:
            return ""
        host, port = server.server_address[:2]
        return f"http://{host}:{port}/{self._code}/"

    def _start(self, local_address: str) -> _Server:
        server = self._server
        if server is not None:
            if server.server_address[0] == local_address:
                return server
            self._close(server)  # the device is on another network
        for port in (*SERVER_PORTS, 0):
            try:
                server = _Server((local_address, port), _MediaHandler)
                break
            except OSError:
                continue
        else:
            raise CastError(tr("MusicHive could not offer its sound on the network"))
        server.media = self  # type: ignore[attr-defined]
        self._server = server
        threading.Thread(target=server.serve_forever, name="cast server", daemon=True).start()
        return server

    def stop(self) -> None:
        """Stop answering and forget everything shared. Returns at once."""
        server, self._server = self._server, None
        with self._lock:
            self._shared.clear()
            self._allowed = set()
            self._browser, self._code = None, ""
            self._wrong_guesses.clear()
        if server is not None:
            self._close(server)

    @staticmethod
    def _close(server: _Server) -> None:
        def close() -> None:
            server.shutdown()  # waits for the server's loop, up to half a second
            server.server_close()

        threading.Thread(target=close, name="cast server stop", daemon=True).start()

    def share_file(self, path: Path) -> str:
        return self._share(path, audio_type(path.name), path.name)

    def share_silence(self) -> str:
        """The quiet stand-in a Chromecast's previous and next buttons lead to."""
        if not hasattr(self, "_silence"):
            self._silence = silence_wav()
        return self._share(self._silence, "audio/wav", "silence.wav")

    def share_bytes(self, data: bytes, kind: str, name: str) -> str:
        return self._share(data, kind, name)

    def share_image(self, data: bytes) -> str:
        kind = image_type(data)
        return self._share(data, kind, "cover.png" if kind == "image/png" else "cover.jpg")

    def share_stream(self, stream, kind: str) -> str:
        """`stream` has serve(write): it writes live sound or video until told to
        stop (a radio station, or a cast video)."""
        return self._share(stream, kind, "live")

    def _share(self, source: object, kind: str, name: str) -> str:
        token = secrets.token_urlsafe(12)
        with self._lock:
            self._shared[token] = (source, kind)
            while len(self._shared) > KEEP_SHARED:
                self._shared.popitem(last=False)
        return f"/{token}/{urllib.parse.quote(name)}"

    def lookup(self, token: str) -> tuple[object, str] | None:
        with self._lock:
            return self._shared.get(token)

    def url(self, path: str, device_host: str) -> str:
        """The full address of something shared, for `device_host` (the only
        device that will be answered from now on). An empty `device_host` is a
        browser page: the browsers that opened it are answered."""
        if not device_host:
            server = self._start(lan_address())
            return f"http://{server.server_address[0]}:{server.server_address[1]}{path}"
        device = socket.gethostbyname(device_host)
        local = _local_address(device)
        with self._lock:
            self._allowed = {device}
        server = self._start(local)
        return f"http://{local}:{server.server_address[1]}{path}"


class _MediaHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MusicHive"

    def log_message(self, *_args) -> None:
        pass  # the terminal stays quiet

    def do_HEAD(self) -> None:
        self._answer(with_body=False)

    def do_GET(self) -> None:
        self._answer(with_body=True)

    def do_POST(self) -> None:
        parts = urllib.parse.urlparse(self.path).path.split("/")
        session = self._browser(parts)
        if session is None or parts[2:] not in (["status"], ["control"]):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_STATUS_BYTES:
            self.send_error(400)
            return
        try:
            message = json.loads(self.rfile.read(length))
            if parts[2] == "status":
                session.receive(message)
            else:  # a button on the page: {action, value}
                session.request(str(message["action"]), int(message.get("value") or 0))
        except (ValueError, TypeError, KeyError):
            self.send_error(400)
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _browser(self, parts: list[str]):
        """The BrowserSession for a page address (/<code>/…), or None."""
        if len(parts) < 2 or not parts[1].isdigit():
            return None
        return self.server.media.browser_for(parts[1], self.client_address[0])

    def _answer(self, with_body: bool) -> None:
        media = self.server.media
        parts = urllib.parse.urlparse(self.path).path.split("/")
        session = self._browser(parts)
        if session is not None:
            self._answer_page(session, parts, with_body)
            return
        if not media.allows(self.client_address[0]):
            self.send_error(404)
            return
        entry = media.lookup(parts[1]) if len(parts) > 2 else None
        if entry is None:
            self.send_error(404)
            return
        source, kind = entry
        log(f"{self.client_address[0]} asked for {kind} ({self.command}, "
            f"range {self.headers.get('Range') or 'none'})")
        if hasattr(source, "serve"):
            self._send_live(source, kind, with_body)
        else:
            self._send_bytes(source, kind, with_body)

    def _answer_page(self, session, parts: list[str], with_body: bool) -> None:
        """The browser page: its files, its events and (POST) its status."""
        if len(parts) == 2:  # /<code>: on to /<code>/, so the page's own links work
            self.send_response(301)
            self.send_header("Location", f"/{parts[1]}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rest = "/".join(parts[2:])
        if rest == "events":
            self._send_events(session)
            return
        if rest not in PAGE_FILES:
            self.send_error(404)
            return
        name, kind = PAGE_FILES[rest]
        try:
            data = (PAGE_FOLDER / name).read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if with_body:
            self.wfile.write(data)

    def _send_events(self, session) -> None:
        """What MusicHive tells the page, as it happens (server-sent events)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        log(f"a browser at {self.client_address[0]} opened the page")
        events = session.attach()
        try:
            while True:
                try:
                    event = events.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": still here\n\n")  # keeps the connection open
                    continue
                if event is None:
                    break
                name, data = event
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        except OSError:
            pass  # the page was closed or reloaded
        finally:
            session.detach(events)
            log(f"the browser at {self.client_address[0]} left the page")

    def _common_headers(self, kind: str, live: bool = False) -> None:
        self.send_header("Content-Type", kind)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("transferMode.dlna.org", "Streaming")
        if kind.startswith(("audio/", "video/")):
            features = _DLNA_LIVE_FEATURES if live else _DLNA_FEATURES
            self.send_header("contentFeatures.dlna.org", features)

    def _send_live(self, stream, kind: str, with_body: bool) -> None:
        self.send_response(200)
        self._common_headers(kind, live=True)
        self.send_header("Connection", "close")  # no length: the end is the hang-up
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.close_connection = True
        if with_body:
            sent = 0

            def write(data: bytes) -> None:
                nonlocal sent
                self.wfile.write(data)
                if sent == 0:
                    log(f"started sending {kind}")
                sent += len(data)

            try:
                stream.serve(write)
            except OSError as error:
                log(f"the device hung up after {sent // 1024} KB ({error})")
            else:
                log(f"stopped sending {kind} after {sent // 1024} KB")

    def _send_bytes(self, source, kind: str, with_body: bool) -> None:
        try:
            size = len(source) if isinstance(source, bytes) else os.path.getsize(source)
        except OSError:
            self.send_error(404)
            return
        start, end = 0, size - 1
        wanted = self.headers.get("Range", "")
        match = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", wanted)
        partial = bool(match and (match.group(1) or match.group(2)))
        if partial:
            first, last = match.group(1), match.group(2)
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:  # the last N bytes
                start = max(0, size - int(last))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(206 if partial else 200)
        self._common_headers(kind)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not with_body:
            return
        try:
            if isinstance(source, bytes):
                self.wfile.write(source[start : end + 1])
                return
            with open(source, "rb") as file:
                file.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = file.read(min(COPY_BYTES, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except OSError:
            self.close_connection = True  # the device hung up, often to seek


# ----- talking to a device -----


@dataclass
class CastMedia:
    """What to play on the device, and what it should show meanwhile."""

    url: str
    kind: str  # MIME type
    title: str = ""
    artist: str = ""
    album: str = ""
    cover_url: str = ""
    live: bool = False
    position_ms: int = 0
    autoplay: bool = True
    # Where a Chromecast's previous/next buttons lead: a silent clip, replaced as
    # soon as MusicHive hears of the skip. Empty: the media itself (a live video,
    # which simply goes on while MusicHive changes the song inside it).
    stand_in_url: str = ""
    lyrics: str | None = None  # for a browser page; None: not a song (no "No lyrics")
    gain_db: float = 0.0  # for a browser page: Level Volume's change for this song
    bars: dict | None = None  # for a browser page: the song's bar map (cast_bars.describe)
    bars_coming: bool = False  # …or it is being made, and follows when it is ready


def silence_wav(seconds: int = SILENCE_SECONDS) -> bytes:
    """A quiet WAV file: 8 kHz, 8 bits, mono."""
    rate = 8000
    size = rate * seconds
    header = (
        b"RIFF" + (36 + size).to_bytes(4, "little") + b"WAVEfmt "
        + (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (1).to_bytes(2, "little")
        + rate.to_bytes(4, "little") + rate.to_bytes(4, "little")
        + (1).to_bytes(2, "little") + (8).to_bytes(2, "little")
        + b"data" + size.to_bytes(4, "little")
    )
    return header + b"\x80" * size  # 128 is silence for 8-bit sound


class CastSession:
    """A connection to one device. Commands queue up for a worker thread, which
    also asks the device what it is doing; the window reads that through status()."""

    def __init__(self, device: CastDevice) -> None:
        self.device = device
        self._commands: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._sent = 0  # commands queued
        self._done = 0  # commands carried out
        self._last_command_at = 0.0
        # The device's latest status: "idle", "playing", "paused" or "buffering".
        self._state = "idle"
        self._position_ms = 0
        self._duration_ms = 0
        self._status_at = time.monotonic()
        self._seen_active = False  # the media loaded last has been seen playing
        self._stopping = False  # an idle device was asked to be
        self._finished = 0  # times the loaded media played to its end
        self._interrupted = 0  # times something else stopped it
        self._jumps = 0  # previous (-1) and next (+1) pressed on the device itself
        self._requests: list[tuple[str, int]] = []  # buttons pressed on a browser page
        self._errors: list[str] = []
        self._lost = ""  # the reason, once the device cannot be reached any more
        self._volume: int | None = None  # the device's volume, once read
        self._wanted_volume: int | None = None
        self._sent_volume: int | None = None
        self._failures = 0
        self._running = True
        self._thread = threading.Thread(target=self._run, name="cast session", daemon=True)
        self._thread.start()

    # ----- called from the window -----

    def load(self, media: CastMedia) -> None:
        self._queue(self._do_load, media)

    def play(self) -> None:
        self._queue(self._do_play)

    def pause(self) -> None:
        self._queue(self._do_pause)

    def seek(self, position_ms: int) -> None:
        self._queue(self._do_seek, max(0, position_ms))

    def stop(self) -> None:
        self._queue(self._do_stop)

    def set_volume(self, percent: int) -> None:
        """Only the last of several quick changes is sent (a sleep timer fade makes many)."""
        with self._lock:
            self._wanted_volume = max(0, min(100, percent))
        self._queue(self._apply_volume)

    def set_muted(self, muted: bool) -> None:
        self._queue(self._do_mute, muted)

    def close(self, wait: float = 0.0) -> None:
        """End the session; the device stops playing and the worker finishes.
        `wait`: seconds to wait for that (when quitting, so the device does hear it)."""
        self._queue(None)
        if wait > 0:
            self._thread.join(wait)

    def status(self) -> tuple[str, int, int, bool]:
        """(state, position in ms, duration in ms, settled). Not settled while a
        command may not show in the status yet."""
        with self._lock:
            state, position, duration = self._state, self._position_ms, self._duration_ms
            if state == "playing":
                position += int((time.monotonic() - self._status_at) * 1000)
                if duration > 0:
                    position = min(position, duration)
            settled = (
                self._done >= self._sent
                and time.monotonic() - self._last_command_at > SETTLE_SECONDS
            )
        return state, position, duration, settled

    def take_finished(self) -> bool:
        with self._lock:
            finished, self._finished = self._finished, 0
        return finished > 0

    def take_requests(self) -> list[tuple[str, int]]:
        """Buttons pressed on the device: ("playpause" | "next" | "previous" | "seek", ms)."""
        with self._lock:
            requests, self._requests = self._requests, []
        return requests

    def take_jumps(self) -> int:
        """How many songs the device's own controls asked to skip (negative: back)."""
        with self._lock:
            jumps, self._jumps = self._jumps, 0
        return jumps

    def take_interrupted(self) -> bool:
        with self._lock:
            interrupted, self._interrupted = self._interrupted, 0
        return interrupted > 0

    def take_errors(self) -> list[str]:
        with self._lock:
            errors, self._errors = self._errors, []
        return errors

    def take_volume(self) -> int | None:
        """The device's own volume, once, after connecting."""
        with self._lock:
            volume, self._volume = self._volume, None
        return volume

    @property
    def lost(self) -> str:
        return self._lost

    # ----- the worker -----

    def _queue(self, action: Callable | None, *args) -> None:
        with self._lock:
            self._sent += 1
            self._last_command_at = time.monotonic()
        self._commands.put((action, args))

    def _run(self) -> None:
        try:
            self._connect()
        except Exception as error:
            self._lose(tr("Cannot connect to {name}: {describe}").format(
                name=self.device.name,
                describe=_describe(error),
            ))
            return
        while self._running:
            try:
                action, args = self._commands.get(timeout=POLL_SECONDS)
            except queue.Empty:
                action, args = False, ()
            if action is None:
                self._finish()
                return
            try:
                if action:
                    action(*args)
            except Exception as error:
                with self._lock:
                    self._errors.append(f"{self.device.name}: {_describe(error)}")
            finally:
                if action:
                    with self._lock:
                        self._done += 1
                        self._last_command_at = time.monotonic()
            self._poll_safely()

    def _poll_safely(self) -> None:
        try:
            self._poll()
            self._failures = 0
        except Exception as error:
            self._failures += 1
            if self._failures >= LOST_AFTER_FAILURES:
                self._lose(tr("Lost the connection to {name}: {describe}").format(
                    name=self.device.name,
                    describe=_describe(error),
                ))

    def _lose(self, reason: str) -> None:
        self._running = False
        with self._lock:
            self._lost = reason
        try:
            self._disconnect()
        except Exception:
            pass

    def _finish(self) -> None:
        self._running = False
        try:
            self._quit()
        except Exception:
            pass
        try:
            self._disconnect()
        except Exception:
            pass

    def _update(self, state: str, position_ms: int, duration_ms: int, ended: str = "") -> None:
        """Store what the device said. `ended` is "finished" or "interrupted"
        when it went idle, if that is known."""
        with self._lock:
            if state in ("playing", "paused", "buffering"):
                self._seen_active = True
                self._stopping = False
            elif state == "idle" and self._seen_active and not self._stopping:
                self._seen_active = False
                if ended == "interrupted":
                    self._interrupted += 1
                elif ended == "error":
                    self._errors.append(tr("{name} could not play this").format(
                        name=self.device.name,
                    ))
                    self._interrupted += 1
                else:
                    self._finished += 1
            self._state = state
            self._position_ms = max(0, position_ms)
            if duration_ms > 0:
                self._duration_ms = duration_ms
            self._status_at = time.monotonic()

    def _starting_new_media(self) -> None:
        with self._lock:
            self._seen_active = False
            self._stopping = False
            self._state = "buffering"
            self._duration_ms = 0
            self._finished = self._interrupted = 0  # those were about the media before

    def _do_stop(self) -> None:
        with self._lock:
            self._stopping = True
            self._seen_active = False
        self._stop()

    def _apply_volume(self) -> None:
        with self._lock:
            volume = self._wanted_volume
        if volume is not None and volume != self._sent_volume:
            self._set_volume(volume)
            self._sent_volume = volume

    def _report_volume(self, percent: int) -> None:
        with self._lock:
            self._volume = percent
        self._sent_volume = percent

    # ----- what each kind of device does -----

    def _connect(self) -> None:
        raise NotImplementedError

    def _disconnect(self) -> None:
        pass

    def _quit(self) -> None:
        self._stop()

    def _do_load(self, media: CastMedia) -> None:
        raise NotImplementedError

    def _do_play(self) -> None:
        raise NotImplementedError

    def _do_pause(self) -> None:
        raise NotImplementedError

    def _do_seek(self, position_ms: int) -> None:
        raise NotImplementedError

    def _stop(self) -> None:
        raise NotImplementedError

    def _set_volume(self, percent: int) -> None:
        raise NotImplementedError

    def _do_mute(self, muted: bool) -> None:
        raise NotImplementedError

    def _poll(self) -> None:
        raise NotImplementedError


def _describe(error: Exception) -> str:
    if isinstance(error, urllib.error.URLError) and error.reason:
        return str(error.reason)
    return str(error) or type(error).__name__


def open_session(device: CastDevice, server: MediaServer | None = None) -> CastSession:
    """`server` is needed for a browser: it offers the page."""
    if device.kind == CHROMECAST:
        return ChromecastSession(device)
    if device.kind == BROWSER:
        from src.cast_browser import BrowserSession  # it needs this module first

        return BrowserSession(device, server)
    return DlnaSession(device)


class ChromecastSession(CastSession):
    def _connect(self) -> None:
        if pychromecast is None:
            raise CastError(tr("PyChromecast is not installed"))
        self._cast = pychromecast.get_chromecast_from_cast_info(
            self.device.cast_info, self.device.zeroconf, tries=2, timeout=NETWORK_TIMEOUT
        )
        self._cast.wait(timeout=10)
        self._media = self._cast.media_controller
        status = self._cast.status
        if status is not None and status.volume_level is not None:
            self._report_volume(round(status.volume_level * 100))
        self._disconnected_since: float | None = None
        self._loaded_url = ""
        self._media_session: int | None = None  # the device's number for what we loaded
        self._slots: dict[str, int] = {}  # each queue item's address, and its place
        self._slot = QUEUE_SLOTS  # the place playing now
        self._loaded: CastMedia | None = None

    def _disconnect(self) -> None:
        self._cast.disconnect(timeout=2)

    def _quit(self) -> None:
        self._cast.quit_app()

    def _do_load(self, media: CastMedia) -> None:
        """Load a queue: the media in the middle, stand-ins on both sides. The
        receiver only offers previous and next when there is something to go to."""
        self._starting_new_media()
        self._loaded = media
        self._media_session = None
        self._loaded_at = time.monotonic()
        video = media.kind.startswith("video/")
        # A music track gets the receiver's music layout, which hides any video.
        metadata = {"metadataType": 0 if video else 3, "title": media.title or "MusicHive"}
        if media.artist:
            metadata["artist"] = media.artist
        if media.album:
            metadata["albumName"] = media.album
        if media.cover_url:
            metadata["images"] = [{"url": media.cover_url}]
        stream_type = "LIVE" if media.live else "BUFFERED"
        items = []
        self._slots = {}
        for slot in range(2 * QUEUE_SLOTS + 1):
            if slot == QUEUE_SLOTS:
                url, kind = media.url, media.kind
            elif media.stand_in_url:
                url, kind = media.stand_in_url, "audio/wav"
            else:
                url, kind = media.url, media.kind
            # Each item needs its own address to be told apart (the server ignores this).
            url = f"{url}{'&' if '?' in url else '?'}slot={slot}"
            self._slots[url] = slot
            items.append({
                "media": {
                    "contentId": url,
                    "contentType": kind,
                    "streamType": stream_type if kind == media.kind else "BUFFERED",
                    "metadata": metadata,
                },
                "autoplay": True,
            })
        self._slot = QUEUE_SLOTS
        self._loaded_url = items[QUEUE_SLOTS]["media"]["contentId"]
        message = {
            "type": "QUEUE_LOAD",
            "items": items,
            "startIndex": QUEUE_SLOTS,
            "repeatMode": "REPEAT_OFF",
        }
        if media.position_ms > 0:
            message["currentTime"] = media.position_ms / 1000
        self._media.send_message(message, inc_session_id=True)
        self._media.block_until_active(timeout=15)
        if not media.autoplay:
            self._media.pause()

    def _jumped(self, slot: int) -> None:
        """The receiver moved to another queue item: its own previous or next, or
        (for a song) the end of the song, after which it goes on to the next item."""
        steps = slot - self._slot
        self._slot = slot
        media = self._loaded
        with self._lock:
            near_end = 0 < self._duration_ms - self._position_ms < 4000
            if steps == 1 and media is not None and media.stand_in_url and near_end:
                self._finished += 1  # it played to its end
            else:
                self._jumps += steps
                log(f"{self.device.name} asked to skip {steps:+d}")
        if media is not None and not media.stand_in_url and not 1 <= slot < 2 * QUEUE_SLOTS:
            self._do_load(media)  # out of stand-ins on that side: a fresh queue

    def _do_play(self) -> None:
        self._media.play()

    def _do_pause(self) -> None:
        self._media.pause()

    def _do_seek(self, position_ms: int) -> None:
        self._media.seek(position_ms / 1000)

    def _stop(self) -> None:
        if self._media.status.media_session_id is not None:
            self._media.stop()

    def _set_volume(self, percent: int) -> None:
        self._cast.set_volume(percent / 100)

    def _do_mute(self, muted: bool) -> None:
        self._cast.set_volume_muted(muted)

    def _poll(self) -> None:
        if not self._cast.socket_client.is_connected:
            # PyChromecast reconnects by itself; give it some time first.
            now = time.monotonic()
            if self._disconnected_since is None:
                self._disconnected_since = now
            elif now - self._disconnected_since > LOST_AFTER_FAILURES * 3:
                raise CastError(tr("the device stopped answering"))
            return
        self._disconnected_since = None
        status = self._media.status
        # Statuses about the media played before (it is told it was interrupted
        # when the next one loads) must not count for the new one.
        if status.player_state in ("PLAYING", "PAUSED", "BUFFERING"):
            slot = self._slots.get(status.content_id or "")
            if slot is None:
                self._check_load_failed()
                return
            self._media_session = status.media_session_id
            if slot != self._slot:
                self._jumped(slot)
                return
            media = self._loaded
            if media is not None and media.stand_in_url and slot != QUEUE_SLOTS:
                return  # a stand-in plays until MusicHive loads what comes next
        elif self._media_session is None or status.media_session_id != self._media_session:
            self._check_load_failed()
            return
        state = {
            "PLAYING": "playing",
            "PAUSED": "paused",
            "BUFFERING": "buffering",
        }.get(status.player_state, "idle")
        ended = {
            "FINISHED": "finished",
            "ERROR": "error",
            "CANCELLED": "interrupted",
            "INTERRUPTED": "interrupted",
        }.get(status.idle_reason or "", "")
        position = status.adjusted_current_time or 0.0
        seen = (status.player_state, status.idle_reason)
        if seen != getattr(self, "_last_seen", None):
            self._last_seen = seen
            log(f"{self.device.name} says {status.player_state}"
                + (f" ({status.idle_reason})" if status.idle_reason else ""))
        self._update(state, int(position * 1000), int((status.duration or 0) * 1000), ended)


    def _check_load_failed(self) -> None:
        """A device that refuses media says so only in its log: give up after a while."""
        if (
            self._loaded_url
            and self._media_session is None
            and time.monotonic() - self._loaded_at > LOAD_TIMEOUT_SECONDS
        ):
            self._loaded_url = ""
            with self._lock:
                self._errors.append(tr("{name} could not play this").format(name=self.device.name))
                self._interrupted += 1


class DlnaSession(CastSession):
    def _connect(self) -> None:
        self._pending_seek: int | None = None
        self._ended_near = 0  # how close to its end the media was when last seen
        if self.device.rendering_url:
            try:
                answer = self._render("GetVolume", [("Channel", "Master")])
                self._report_volume(int(answer.get("CurrentVolume", "")))
            except (CastError, OSError, ValueError):
                pass  # some devices have no volume control over the network
        self._transport("GetTransportInfo")  # does it answer at all?

    def _soap(self, url: str, service: str, action: str, args: list) -> dict[str, str]:
        body = "".join(
            f"<{name}>{html.escape(str(value), quote=False)}</{name}>" for name, value in args
        )
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{service}">{body}</u:{action}>'
            "</s:Body></s:Envelope>"
        )
        request = urllib.request.Request(
            url,
            data=envelope.encode(),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{service}#{action}"',
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
                data = response.read()
        except urllib.error.HTTPError as error:
            raise CastError(_soap_fault(error.read()) or tr("{action} was refused").format(
                action=action,
            )) from None
        answer = {}
        for element in ElementTree.fromstring(data).iter():
            if len(element) == 0:
                answer[element.tag.rpartition("}")[2]] = element.text or ""
        return answer

    def _transport(self, action: str, args: list | None = None) -> dict[str, str]:
        device = self.device
        return self._soap(
            device.transport_url, device.transport_type, action, [("InstanceID", 0), *(args or [])]
        )

    def _render(self, action: str, args: list) -> dict[str, str]:
        device = self.device
        if not device.rendering_url:
            raise CastError(tr("no volume control"))
        return self._soap(
            device.rendering_url, device.rendering_type, action, [("InstanceID", 0), *args]
        )

    def _do_load(self, media: CastMedia) -> None:
        self._starting_new_media()
        try:
            self._transport("Stop")  # some TVs take no new address while playing
        except CastError:
            pass
        self._transport(
            "SetAVTransportURI",
            [("CurrentURI", media.url), ("CurrentURIMetaData", _didl(media))],
        )
        self._pending_seek = media.position_ms if media.position_ms > 0 else None
        self._ended_near = 0
        if media.autoplay:
            self._transport("Play", [("Speed", 1)])

    def _do_play(self) -> None:
        self._transport("Play", [("Speed", 1)])

    def _do_pause(self) -> None:
        self._transport("Pause")

    def _do_seek(self, position_ms: int) -> None:
        self._pending_seek = None
        self._transport("Seek", [("Unit", "REL_TIME"), ("Target", _clock(position_ms))])

    def _stop(self) -> None:
        self._transport("Stop")

    def _set_volume(self, percent: int) -> None:
        self._render("SetVolume", [("Channel", "Master"), ("DesiredVolume", percent)])

    def _do_mute(self, muted: bool) -> None:
        self._render("SetMute", [("Channel", "Master"), ("DesiredMute", 1 if muted else 0)])

    def _poll(self) -> None:
        info = self._transport("GetTransportInfo")
        state = {
            "PLAYING": "playing",
            "PAUSED_PLAYBACK": "paused",
            "PAUSED_RECORDING": "paused",
            "TRANSITIONING": "buffering",
        }.get(info.get("CurrentTransportState", ""), "idle")
        position = duration = 0
        if state != "idle":
            where = self._transport("GetPositionInfo")
            position = _milliseconds(where.get("RelTime", ""))
            duration = _milliseconds(where.get("TrackDuration", ""))
            self._ended_near = duration - position if duration > 0 else 0
        if self._pending_seek is not None and state in ("playing", "paused"):
            target, self._pending_seek = self._pending_seek, None
            self._transport("Seek", [("Unit", "REL_TIME"), ("Target", _clock(target))])
            position = target
        # DLNA does not say why it stopped: near the end it will have played out.
        ended = "finished" if self._ended_near < 5000 else "interrupted"
        self._update(state, position, duration, ended)


def _soap_fault(data: bytes) -> str:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return ""
    for element in root.iter():
        if element.tag.rpartition("}")[2] == "errorDescription" and element.text:
            return element.text
    return ""


def _didl(media: CastMedia) -> str:
    """The DIDL-Lite description DLNA devices show while playing."""
    escape = html.escape
    parts = [
        f"<dc:title>{escape(media.title or 'MusicHive')}</dc:title>",
        "<upnp:class>object.item.videoItem</upnp:class>"
        if media.kind.startswith("video/")
        else "<upnp:class>object.item.audioItem.musicTrack</upnp:class>",
    ]
    if media.artist:
        parts.append(f"<upnp:artist>{escape(media.artist)}</upnp:artist>")
        parts.append(f"<dc:creator>{escape(media.artist)}</dc:creator>")
    if media.album:
        parts.append(f"<upnp:album>{escape(media.album)}</upnp:album>")
    if media.cover_url:
        parts.append(f"<upnp:albumArtURI>{escape(media.cover_url)}</upnp:albumArtURI>")
    features = "*" if media.live else _DLNA_FEATURES
    parts.append(
        f'<res protocolInfo="http-get:*:{escape(media.kind)}:{features}">'
        f"{escape(media.url)}</res>"
    )
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        f'<item id="0" parentID="-1" restricted="1">{"".join(parts)}</item></DIDL-Lite>'
    )


def _clock(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _milliseconds(text: str) -> int:
    """'1:02:03' or '0:03:21.500' in milliseconds; 0 for 'NOT_IMPLEMENTED' and the like."""
    match = re.fullmatch(r"\s*(\d+):(\d{1,2}):(\d{1,2})(?:\.(\d+))?\s*", text or "")
    if not match:
        return 0
    hours, minutes, seconds = (int(match.group(i)) for i in (1, 2, 3))
    fraction = int((match.group(4) or "0")[:3].ljust(3, "0"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + fraction
