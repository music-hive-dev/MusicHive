"""Party Mode: guests on the same network add songs to the queue from their phones.

    phone ──scans the QR code──> http://<this computer>:<port>/<code>/
    phone ──search, add, vote to skip──> PartyServer ──> PartyHost (main thread)
    phone <──what plays and what comes next (server-sent events)── PartyHost

The page (assets/party/) asks for a name once and sends it with everything, so
the Upcoming list can say who asked for a song.

Where songs come from is a PartySource, named by the start of each result's id
("library:…"). The library is the only one for now; a source that fetches songs
from elsewhere (such as octo-fiesta) can be added next to it. Its request() may
take a while, and it can report later with PartyHost.notify().

Guests' requests arrive on the server's threads. Everything they read or change
runs on the main thread (PartyHost.on_main), like the rest of MusicHive.
"""

import json
import queue
import secrets
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QBuffer, QIODevice, QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath

from src.cast import SERVER_PORTS, lan_address
from src.cover_view import decode_cover
from src.i18n import tr

try:
    import segno
except ImportError:  # the address is still shown, just without the code
    segno = None

PAGE_FOLDER = Path(__file__).parent.parent / "assets" / "party"
LOGO_FILE = Path(__file__).parent.parent / "musichive_colour.svg"
PAGE_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "party.js": ("party.js", "text/javascript; charset=utf-8"),
    "party.css": ("party.css", "text/css; charset=utf-8"),
}
MAX_POST_BYTES = 4096
MAX_NAME = 15  # characters of a guest's name
MAX_GUEST_ID = 40
MAX_QUERY = 100
SEARCH_RESULTS = 25  # from each source
UPCOMING_SHOWN = 15  # songs of the Upcoming list the page shows
MAIN_THREAD_WAIT = 5.0  # seconds a guest's request waits for the main thread
EVENTS_KEPT = 64  # events waiting for a page that does not read them: it is let go
NOW_COVER_SIDE = 480  # pixels of the playing song's cover on the page
STATE_DELAY_MS = 150  # changes this close together go out to the phones as one


class PartyError(Exception):
    """Something a guest asked for that cannot be done; the message is for them."""


class PartySource(Protocol):
    """Somewhere songs come from. Ids of its results start with its name and ":".

    search() and cover() run on the main thread, or, for a `threaded` source (one
    that asks over the network), on the server's thread that answers the phone.
    request() always runs on the main thread, and must not wait for the network.
    """

    name: str
    threaded: bool

    def search(self, text: str, limit: int) -> list[dict]:
        """Results: {"id", "title", "artist", "album", "cover"} ("cover" is a key for
        cover(), or ""), and optionally "note", a word about the result such as that
        it has to be fetched first."""

    def cover(self, key: str) -> bytes | None:
        """A small JPEG or PNG for a result's "cover" key."""

    def request(self, item: str, guest: "Guest") -> str:
        """Queue the result with this id (without the source's name). Returns what to
        tell the guest. Raises PartyError when it cannot be done."""


@dataclass
class Guest:
    id: str  # made up by the phone and kept there, so a reload is the same guest
    name: str
    connections: int = 0  # pages of this guest open right now


@dataclass
class PartyState:
    """What the phones show; main.py fills it in when PartyHost asks."""

    title: str = ""
    artist: str = ""
    guest: str = ""  # who asked for the playing song
    cover: bytes | None = None  # the playing song's cover, as stored in the file
    playing: bool = False
    upcoming: list[dict] = field(default_factory=list)  # {title, artist, guest, cover}


class _Job:
    def __init__(self, function: Callable, args: tuple) -> None:
        self.function = function
        self.args = args
        self.done = threading.Event()
        self.result = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self.function(*self.args)
        except BaseException as error:  # handed to the waiting thread
            self.error = error
        finally:
            self.done.set()


class _Bridge(QObject):
    """Runs jobs from the server's threads on the main thread."""

    job = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.job.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @staticmethod
    def _run(job: _Job) -> None:
        job.run()


class PartyHost(QObject):
    """The party: its guests, the skip votes and the server. Lives on the main thread.

    main.py gives it `state` (what plays and what comes next), `skip` (go to the
    next song) and the sources, and calls changed() when any of that changed.
    """

    guests_changed = Signal()  # who is here, or the skip votes
    stopped = Signal()

    def __init__(
        self,
        state: Callable[[], PartyState],
        skip: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._skip = skip
        self.sources: dict[str, PartySource] = {}
        self.vote_skip = True
        self.guest_limit = 0  # songs each guest may have waiting (0: no limit)
        # How many of a guest's songs wait: queued and not played yet, or still being
        # fetched. main.py sets it.
        self.waiting_for: Callable[[Guest], int] | None = None
        self._guests: dict[str, Guest] = {}
        self._votes: set[str] = set()  # guest ids that want the playing song skipped
        self._song_key: object = None  # the playing song the votes are for
        self._now_cover: bytes | None = None  # the playing cover, sized for phones
        self._now_cover_from: bytes | None = None  # ...made from these file bytes
        self._now_cover_version = 0
        self._last_state: dict | None = None
        self._pages: set[queue.Queue] = set()  # one per open page
        self._lock = threading.Lock()  # guards _pages
        self._bridge = _Bridge()
        self._server: _PartyServer | None = None
        self._code = ""
        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(STATE_DELAY_MS)
        self._state_timer.timeout.connect(self._send_state)

    # ----- starting and stopping -----

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> str:
        """Start the party, if it hasn't; returns the address for the guests.
        Raises OSError when no port can be had."""
        if self._server is None:
            address = lan_address()
            for port in (*SERVER_PORTS, 0):
                try:
                    server = _PartyServer((address, port), _PartyHandler)
                    break
                except OSError:
                    continue
            else:
                raise OSError(tr("MusicHive could not offer the party page on the network"))
            server.host = self
            self._code = secrets.token_urlsafe(9)
            self._server = server
            threading.Thread(target=server.serve_forever, name="party server", daemon=True).start()
            self.changed()
        return self.address()

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        self._code = ""
        with self._lock:
            pages, self._pages = self._pages, set()
        for page in pages:
            page.put(None)  # their connections end
        self._guests.clear()
        self._votes.clear()
        self._last_state = None

        def close() -> None:
            server.shutdown()
            server.server_close()

        threading.Thread(target=close, name="party server stop", daemon=True).start()
        self.guests_changed.emit()
        self.stopped.emit()

    def address(self) -> str:
        server = self._server
        if server is None:
            return ""
        host, port = server.server_address[:2]
        return f"http://{host}:{port}/{self._code}/"

    def code_matches(self, code: str) -> bool:
        current = self._code  # read once: the server's threads ask too
        return bool(current) and secrets.compare_digest(code, current)

    # ----- what the host sees -----

    def guest_names(self) -> list[str]:
        """The guests with the page open now, in the order they came."""
        return [guest.name for guest in self._guests.values() if guest.connections and guest.name]

    def votes(self) -> tuple[int, int]:
        """Skip votes for the playing song, and how many are needed."""
        return len(self._votes), self._votes_needed()

    def set_vote_skip(self, enabled: bool) -> None:
        self.vote_skip = enabled
        if not enabled:
            self._votes.clear()
        self.guests_changed.emit()
        self.changed()

    # ----- news for the phones -----

    def changed(self) -> None:
        """What plays or what comes next changed: tell the phones in a moment."""
        if self.running:
            self._state_timer.start()

    def notify(self, guest_id: str, message: str) -> None:
        """Tell one guest something (a source that finished fetching their song)."""
        self._send("notice", {"guest": guest_id, "message": message})

    def _send_state(self) -> None:
        if not self.running:
            return
        state = self._state()
        key = (state.title, state.artist, state.guest)
        if key != self._song_key:  # a new song: the votes were for the last one
            self._song_key = key
            if self._votes:
                self._votes.clear()
                self.guests_changed.emit()
        if state.cover is not self._now_cover_from:
            self._now_cover_from = state.cover
            self._now_cover = _jpeg(decode_cover(state.cover, NOW_COVER_SIDE))
            self._now_cover_version += 1
        votes, needed = self.votes()
        data = {
            "title": state.title,
            "artist": state.artist,
            "guest": state.guest,
            "playing": state.playing,
            "cover": f"cover/now-{self._now_cover_version}" if self._now_cover else "",
            "upcoming": state.upcoming[:UPCOMING_SHOWN],
            "voteSkip": self.vote_skip,
            "votes": votes,
            "votesNeeded": needed,
            "guests": len(self.guest_names()),
        }
        self._last_state = data
        self._send("state", data)

    def _send(self, name: str, data: dict) -> None:
        with self._lock:
            pages = list(self._pages)
        for page in pages:
            if page.qsize() < EVENTS_KEPT:
                page.put((name, data))

    # ----- called from the server's threads -----

    def on_main(self, function: Callable, *args):
        """Run `function` on the main thread and return what it returns."""
        job = _Job(function, args)
        self._bridge.job.emit(job)
        if not job.done.wait(MAIN_THREAD_WAIT):
            raise PartyError(tr("MusicHive is busy. Try again in a moment."))
        if job.error is not None:
            raise job.error
        return job.result

    def open_page(self, guest_id: str, page: queue.Queue) -> None:
        """A phone opened the page; its events go into `page`. Returns at once: the
        main thread sends it the texts and the state when it gets to it."""
        with self._lock:
            self._pages.add(page)
        self._bridge.job.emit(_Job(self._page_opened, (guest_id, page)))

    def close_page(self, guest_id: str, page: queue.Queue) -> None:
        with self._lock:
            self._pages.discard(page)
        # Queued after the opening, so they run in the same order.
        self._bridge.job.emit(_Job(self._page_closed, (guest_id,)))

    # ----- on the main thread, for the server -----

    def texts(self) -> dict:
        """The page's words, in the user's language."""
        return {
            "welcome": tr("Welcome to the party!"),
            "askName": tr("What's your name? It's shown next to the songs you add."),
            "namePlaceholder": tr("Your name"),
            "join": tr("Join"),
            "changeName": tr("Change name"),
            "nowPlaying": tr("Now playing"),
            "paused": tr("Paused"),
            "nothingPlaying": tr("Nothing is playing yet"),
            "upNext": tr("Up next"),
            "nothingNext": tr("Nothing queued. Add a song!"),
            "addedBy": tr("added by {name}"),
            "search": tr("Search for a song, artist or album"),
            "noResults": tr("Nothing found"),
            "add": tr("Add"),
            "voteSkip": tr("Vote to skip"),
            "voted": tr("You voted to skip"),
            "votes": tr("{votes} of {needed} votes to skip"),
            "lost": tr("Lost the connection to MusicHive. Trying again…"),
            "ended": tr("The party has ended. Thanks for coming!"),
        }

    def search(self, text: str) -> list[dict]:
        """On a server thread: each source is asked where it may be (see PartySource)."""
        text = text.strip()[:MAX_QUERY]
        if not text:
            return []
        results: list[dict] = []
        for name, source in list(self.sources.items()):
            try:
                if source.threaded:
                    found = source.search(text, SEARCH_RESULTS)
                else:
                    found = self.on_main(source.search, text, SEARCH_RESULTS)
            except PartyError:
                raise
            except Exception as error:  # one source failing leaves the others
                print(f"MusicHive: party search in {name} failed: {error}")
                continue
            for result in found:
                result = dict(result)
                result["id"] = f"{name}:{result['id']}"
                if result.get("cover"):
                    result["cover"] = f"cover/{name}-{result['cover']}"
                results.append(result)
        return results

    def request(self, guest_id: str, name: str, item: str) -> str:
        guest = self._meet(guest_id, name)
        source_name, _colon, rest = item.partition(":")
        source = self.sources.get(source_name)
        if source is None or not rest:
            raise PartyError(tr("That song is no longer there. Search again."))
        limit = self.guest_limit
        if limit and self.waiting_for is not None and self.waiting_for(guest) >= limit:
            raise PartyError(tr(
                "You have %n song(s) waiting already. Add another once one has played.",
                "", limit,
            ))
        message = source.request(rest, guest)
        self.changed()
        return message

    def vote(self, guest_id: str, name: str) -> str:
        self._meet(guest_id, name)
        if not self.vote_skip:
            raise PartyError(tr("Skipping by vote is turned off."))
        state = self._state()
        if not state.title:
            raise PartyError(tr("Nothing is playing."))
        self._votes.add(guest_id)
        votes, needed = self.votes()
        if votes >= needed:
            self._votes.clear()
            self._skip()
            message = tr("Skipped!")
        else:
            message = tr("Voted. {votes} of {needed} votes to skip.").format(
                votes=votes, needed=needed
            )
        self.guests_changed.emit()
        self.changed()
        return message

    def cover(self, name: str) -> bytes | None:
        """On a server thread: a cover the page asked for, "now-<n>" or "<source>-<key>"."""
        if name.startswith("now-"):
            return self._now_cover
        source_name, _dash, key = name.partition("-")
        source = self.sources.get(source_name)
        if source is None or not key:
            return None
        return source.cover(key) if source.threaded else self.on_main(source.cover, key)

    def last_state(self) -> dict | None:
        return self._last_state

    def _meet(self, guest_id: str, name: str) -> Guest:
        name = " ".join(name.split())[:MAX_NAME]
        if not guest_id or not name:
            raise PartyError(tr("Tell us your name first."))
        guest = self._guests.get(guest_id)
        if guest is None:
            guest = self._guests[guest_id] = Guest(guest_id, name)
            self.guests_changed.emit()
        elif guest.name != name:
            guest.name = name
            self.guests_changed.emit()
        return guest

    def _page_opened(self, guest_id: str, page: queue.Queue) -> None:
        page.put(("texts", self.texts()))
        if self._last_state is not None:
            page.put(("state", self._last_state))
        guest = self._guests.get(guest_id)
        if guest is None:
            guest = self._guests[guest_id] = Guest(guest_id, "")
        guest.connections += 1
        self.guests_changed.emit()
        self.changed()  # the guest count on the phones

    def _page_closed(self, guest_id: str) -> None:
        guest = self._guests.get(guest_id)
        if guest is not None:
            guest.connections = max(0, guest.connections - 1)
        self.guests_changed.emit()
        self.changed()

    def _votes_needed(self) -> int:
        """More than half of the guests who are here."""
        here = sum(1 for guest in self._guests.values() if guest.connections)
        return max(here, len(self._votes), 1) // 2 + 1


class LibrarySource:
    """Songs from the library that is open. main.py gives it the songs to search
    and what queues one; `generation` goes up when the library's rows change, so
    an old search result can't queue the wrong song."""

    name = "library"
    threaded = False

    def __init__(
        self,
        songs: Callable[[], list[tuple[int, str, str, str, str | None]]],
        queue_song: Callable[[int, str], str],
        cover: Callable[[str], bytes | None],
    ) -> None:
        self._songs = songs  # (row, title, artist, album, cover key)
        self._queue_song = queue_song  # (row, guest name) -> message for the guest
        self._cover = cover  # cover key -> the library's JPEG thumbnail
        self.generation = 0

    def cover(self, key: str) -> bytes | None:
        return self._cover(key)

    def search(self, text: str, limit: int) -> list[dict]:
        words = text.casefold().split()
        results = []
        for row, title, artist, album, cover in self._songs():
            haystack = f"{title} {artist} {album}".casefold()
            if all(word in haystack for word in words):
                results.append({
                    "id": f"{self.generation}.{row}",
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "cover": cover or "",
                })
                if len(results) >= limit:
                    break
        return results

    def request(self, item: str, guest: Guest) -> str:
        generation, _dot, row = item.partition(".")
        if generation != str(self.generation) or not row.isdigit():
            raise PartyError(tr("The library changed. Search again."))
        return self._queue_song(int(row), guest.name)


def _jpeg(image: QImage | None) -> bytes | None:
    if image is None:
        return None
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.convertToFormat(QImage.Format.Format_RGB32).save(buffer, "JPEG", 88):
        return None
    return bytes(buffer.data())


# ----- the QR code -----

QR_DARK = QColor("#151416")
QR_LIGHT = QColor("#ffffff")
QR_BORDER = 2  # modules of light around the code; the card's rounded corners add more
LOGO_SHARE = 0.26  # of the code's width; "H" error correction makes up for what it hides
DOT_SHARE = 0.95  # a dot's width, of its module's: a gap keeps neighbouring dots apart
FINDER = 7  # modules across each of the three corner blocks
# How thick the corner blocks' rings are, in modules. A little under one, so they
# look as heavy as the dots instead of massive; scanners allow for that much.
RING_SHARE = 0.9


def qr_image(text: str, side: int) -> QImage | None:
    """A QR code for `text` with the MusicHive logo in the middle, about `side`
    pixels square, on a white card with rounded corners. None without segno.

    Each module is a round dot, and the three corner blocks are a ring around a
    dot. Every module is a whole number of pixels, and the logo sits in a hole
    of whole modules, so no dot is ever half covered.
    """
    if segno is None:
        return None
    code = segno.make_qr(text, error="h", boost_error=False)
    rows = [list(row) for row in code.matrix_iter(scale=1, border=QR_BORDER)]
    count = len(rows)
    module = max(1, side // count)
    side = module * count
    # The hole: as many modules as the logo needs, centred exactly (the same
    # odd or even count as the code, so the same number is left on each side).
    hole = max(3, round(count * LOGO_SHARE))
    if (count - hole) % 2:
        hole += 1
    first = (count - hole) // 2
    last = first + hole  # the first module after the hole

    image = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    card = QPainterPath()
    radius = module * QR_BORDER * 0.8
    card.addRoundedRect(QRectF(0, 0, side, side), radius, radius)
    painter.fillPath(card, QR_LIGHT)

    # The corner blocks (top left, top right, bottom left), drawn on their own.
    near, far = QR_BORDER, count - QR_BORDER - FINDER
    finders = [(near, near), (far, near), (near, far)]

    def in_finder(x: int, y: int) -> bool:
        return any(fx <= x < fx + FINDER and fy <= y < fy + FINDER for fx, fy in finders)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QR_DARK)
    dot = module * DOT_SHARE
    inset = (module - dot) / 2
    for y, row in enumerate(rows):
        in_hole_row = first <= y < last
        for x, dark in enumerate(row):
            if not dark or (in_hole_row and first <= x < last) or in_finder(x, y):
                continue
            painter.drawEllipse(QRectF(x * module + inset, y * module + inset, dot, dot))

    # Each corner block: a ring (RING_SHARE of a module thick) around a dot three
    # modules wide, with a light gap between them, like the square original.
    for fx, fy in finders:
        outer = QRectF(fx * module, fy * module, FINDER * module, FINDER * module)
        ring = QPainterPath()
        ring.addEllipse(outer)
        thickness = module * RING_SHARE
        ring.addEllipse(outer.adjusted(thickness, thickness, -thickness, -thickness))  # the hollow
        painter.fillPath(ring, QR_DARK)  # (odd-even fill leaves the middle light)
        painter.drawEllipse(outer.adjusted(2 * module, 2 * module, -2 * module, -2 * module))

    # The logo, with half a module of white around it inside the hole.
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    margin = module * 0.5
    logo_side = hole * module - 2 * margin
    logo = QIcon(str(LOGO_FILE)).pixmap(round(logo_side), round(logo_side))
    painter.drawPixmap(
        QRectF(first * module + margin, first * module + margin, logo_side, logo_side),
        logo,
        QRectF(logo.rect()),
    )
    painter.end()
    return image


def qr_png(text: str, side: int) -> bytes | None:
    """The same, as PNG bytes (for a TV's browser page)."""
    image = qr_image(text, side)
    if image is None:
        return None
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


# ----- the web server -----


class _PartyServer(ThreadingHTTPServer):
    daemon_threads = True
    host: PartyHost


class _PartyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MusicHive"

    def log_message(self, *_args) -> None:
        pass

    # Every address is /<code>/<what>; anything else gets "not found".

    def _route(self) -> tuple[str, dict[str, str]] | None:
        url = urllib.parse.urlparse(self.path)
        parts = url.path.split("/")
        if len(parts) < 2 or not self.server.host.code_matches(parts[1]):
            self.send_error(404)
            return None
        if len(parts) == 2:  # /<code>: on to /<code>/, so the page's own links work
            self.send_response(301)
            self.send_header("Location", f"/{parts[1]}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        query = {key: values[0] for key, values in urllib.parse.parse_qs(url.query).items()}
        return "/".join(parts[2:]), query

    def do_GET(self) -> None:
        route = self._route()
        if route is None:
            return
        what, query = route
        host = self.server.host
        try:
            if what in PAGE_FILES:
                name, kind = PAGE_FILES[what]
                self._send(kind, (PAGE_FOLDER / name).read_bytes(), cache=False)
            elif what == "logo.svg":
                self._send("image/svg+xml", LOGO_FILE.read_bytes())
            elif what == "events":
                self._send_events(query.get("guest", "")[:MAX_GUEST_ID])
            elif what == "search":
                results = host.search(query.get("q", ""))
                self._send_json({"results": results})
            elif what.startswith("cover/"):
                data = host.cover(what[len("cover/"):])
                if data:
                    self._send("image/jpeg", data)
                else:
                    self.send_error(404)
            else:
                self.send_error(404)
        except PartyError as error:
            self._send_json({"error": str(error)}, status=409)
        except OSError:
            self.close_connection = True

    def do_POST(self) -> None:
        route = self._route()
        if route is None:
            return
        what, _query = route
        host = self.server.host
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_POST_BYTES:
            self.send_error(400)
            return
        try:
            message = json.loads(self.rfile.read(length))
            guest = str(message.get("guest", ""))[:MAX_GUEST_ID]
            name = str(message.get("name", ""))
            if what == "add":
                text = host.on_main(host.request, guest, name, str(message.get("id", "")))
            elif what == "vote":
                text = host.on_main(host.vote, guest, name)
            else:
                self.send_error(404)
                return
            self._send_json({"message": text})
        except PartyError as error:
            self._send_json({"error": str(error)}, status=409)
        except (ValueError, TypeError, AttributeError):
            self.send_error(400)
        except OSError:
            self.close_connection = True

    def _send(self, kind: str, data: bytes, cache: bool = True, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_json(self, data: dict, status: int = 200) -> None:
        self._send("application/json", json.dumps(data).encode(), cache=False, status=status)

    def _send_events(self, guest_id: str) -> None:
        """What plays and what comes next, as it changes (server-sent events)."""
        host = self.server.host
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        page: queue.Queue = queue.Queue()
        host.open_page(guest_id, page)
        try:
            while True:
                try:
                    event = page.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": still here\n\n")  # keeps the connection open
                    continue
                if event is None:  # the party ended
                    self.wfile.write(b"event: ended\ndata: {}\n\n")
                    break
                name, data = event
                if name == "notice" and data.get("guest") != guest_id:
                    continue
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        except OSError:
            pass  # the phone went away or reloaded the page
        finally:
            host.close_page(guest_id, page)
