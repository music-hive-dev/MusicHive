"""Internet radio: finding stations (Radio Browser), favourites, what was heard on
them, and the songs the listener chose to remember.

Everything is kept in radio.db in the data folder. Stations come from Radio
Browser (radio-browser.info), a free directory that needs no account or key.
"""

import json
import random
import sqlite3
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, QThreadPool, Signal

from src import library_db

USER_AGENT = "MusicHive/1.0 (radio)"
SERVERS_URL = "https://all.api.radio-browser.info/json/servers"
FALLBACK_SERVERS = ("de1.api.radio-browser.info", "nl1.api.radio-browser.info", "at1.api.radio-browser.info")
TIMEOUT_SECONDS = 20
RESULT_LIMIT = 150
HISTORY_LIMIT = 300  # titles kept per station

# ----- settings -----

KEEP_MINUTES_KEY = "radio/timeShiftMinutes"
SCROBBLE_KEY = "radio/scrobble"
SCROBBLE_AFTER_KEY = "radio/scrobbleAfterSeconds"
TAPE_FOLDER_KEY = "radio/tapeFolder"
TAPE_PER_SONG_KEY = "radio/tapePerSong"
TAPE_FROM_START_KEY = "radio/tapeFromSongStart"
TAPE_TAGS_KEY = "radio/tapeTags"


@dataclass(frozen=True)
class RadioSettings:
    keep_minutes: int = 30  # how far back pausing can go (the time-shift buffer)
    scrobble: bool = False  # songs heard on the radio go to Last.fm
    scrobble_after_seconds: int = 60  # a radio song counts after this long
    tape_folder: str = ""  # "" means the default
    tape_per_song: bool = True  # one file per song (at each title change)
    tape_from_song_start: bool = True  # taping starts at the beginning of the song heard
    tape_tags: bool = True  # artist, title and station written into MP3 tapes

    def tapes_folder(self) -> Path:
        return Path(self.tape_folder) if self.tape_folder else default_tapes_folder()


def default_tapes_folder() -> Path:
    return library_db.music_dir() / "MusicHive Tapes"


def load_radio_settings(settings: QSettings) -> RadioSettings:
    d = RadioSettings()
    return RadioSettings(
        keep_minutes=max(1, min(240, settings.value(KEEP_MINUTES_KEY, d.keep_minutes, type=int))),
        scrobble=settings.value(SCROBBLE_KEY, d.scrobble, type=bool),
        scrobble_after_seconds=max(
            10, settings.value(SCROBBLE_AFTER_KEY, d.scrobble_after_seconds, type=int)
        ),
        tape_folder=settings.value(TAPE_FOLDER_KEY, d.tape_folder, type=str),
        tape_per_song=settings.value(TAPE_PER_SONG_KEY, d.tape_per_song, type=bool),
        tape_from_song_start=settings.value(TAPE_FROM_START_KEY, d.tape_from_song_start, type=bool),
        tape_tags=settings.value(TAPE_TAGS_KEY, d.tape_tags, type=bool),
    )


def save_radio_settings(settings: QSettings, style: RadioSettings) -> None:
    for key, value in (
        (KEEP_MINUTES_KEY, style.keep_minutes),
        (SCROBBLE_KEY, style.scrobble),
        (SCROBBLE_AFTER_KEY, style.scrobble_after_seconds),
        (TAPE_FOLDER_KEY, style.tape_folder),
        (TAPE_PER_SONG_KEY, style.tape_per_song),
        (TAPE_FROM_START_KEY, style.tape_from_song_start),
        (TAPE_TAGS_KEY, style.tape_tags),
    ):
        settings.setValue(key, value)


# ----- what is stored -----


@dataclass
class Station:
    id: int  # 0 for a search result that is not stored yet
    uuid: str  # Radio Browser's id; "" for a station added by its address
    name: str
    url: str
    homepage: str
    favicon: str
    country: str
    language: str
    tags: str  # comma-separated genres
    codec: str
    bitrate: int
    hls: bool
    favourite: bool = False
    last_played: float = 0.0

    def details(self) -> str:
        """A short line: country · genres · MP3 128 kbps."""
        quality = " ".join(
            part for part in (self.codec, f"{self.bitrate} kbps" if self.bitrate else "") if part
        )
        genres = ", ".join(tag.strip() for tag in self.tags.split(",")[:3] if tag.strip())
        return " · ".join(part for part in (self.country, genres, quality) if part)


@dataclass
class HeardSong:
    id: int
    station_id: int
    station: str
    title: str
    heard_at: float


_STATION_COLUMNS = (
    "id, uuid, name, url, homepage, favicon, country, language, tags, codec, bitrate, hls, "
    "favourite, last_played"
)


def _station(row) -> Station:
    return Station(*row[:11], bool(row[11]), bool(row[12]), row[13])


class RadioStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS stations (
                id INTEGER PRIMARY KEY,
                uuid TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL UNIQUE,
                homepage TEXT NOT NULL DEFAULT '',
                favicon TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                codec TEXT NOT NULL DEFAULT '',
                bitrate INTEGER NOT NULL DEFAULT 0,
                hls INTEGER NOT NULL DEFAULT 0,
                favourite INTEGER NOT NULL DEFAULT 0,
                last_played REAL NOT NULL DEFAULT 0,
                favourite_order INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY,
                station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                heard_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS history_by_station ON history (station_id, heard_at);
            CREATE TABLE IF NOT EXISTS remembered (
                id INTEGER PRIMARY KEY,
                station_id INTEGER,
                station TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                heard_at REAL NOT NULL
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ----- stations -----

    def save(self, station: Station) -> int:
        """Store a station (found, or added by address) and return its id. A station
        already stored keeps its favourite mark and history."""
        row = self._db.execute("SELECT id FROM stations WHERE url = ?", (station.url,)).fetchone()
        values = (station.uuid, station.name, station.homepage, station.favicon, station.country,
                  station.language, station.tags, station.codec, station.bitrate, int(station.hls))
        if row:
            self._db.execute(
                "UPDATE stations SET uuid = ?, name = ?, homepage = ?, favicon = ?, country = ?, "
                "language = ?, tags = ?, codec = ?, bitrate = ?, hls = ? WHERE id = ?",
                (*values, row[0]),
            )
            station_id = row[0]
        else:
            station_id = self._db.execute(
                "INSERT INTO stations (uuid, name, homepage, favicon, country, language, tags, "
                "codec, bitrate, hls, url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*values, station.url),
            ).lastrowid
        self._db.commit()
        return station_id

    def station(self, station_id: int) -> Station | None:
        row = self._db.execute(
            f"SELECT {_STATION_COLUMNS} FROM stations WHERE id = ?", (station_id,)
        ).fetchone()
        return _station(row) if row else None

    def favourites(self) -> list[Station]:
        rows = self._db.execute(
            f"SELECT {_STATION_COLUMNS} FROM stations WHERE favourite = 1 "
            "ORDER BY favourite_order, name COLLATE NOCASE"
        )
        return [_station(row) for row in rows]

    def recent(self, limit: int = 30) -> list[Station]:
        rows = self._db.execute(
            f"SELECT {_STATION_COLUMNS} FROM stations WHERE last_played > 0 "
            "ORDER BY last_played DESC LIMIT ?",
            (limit,),
        )
        return [_station(row) for row in rows]

    def set_favourite(self, station_id: int, favourite: bool) -> None:
        order = self._db.execute("SELECT COALESCE(MAX(favourite_order), 0) + 1 FROM stations").fetchone()[0]
        self._db.execute(
            "UPDATE stations SET favourite = ?, favourite_order = ? WHERE id = ?",
            (int(favourite), order if favourite else 0, station_id),
        )
        self._db.commit()

    def rename(self, station_id: int, name: str) -> None:
        self._db.execute("UPDATE stations SET name = ? WHERE id = ?", (name, station_id))
        self._db.commit()

    def set_played(self, station_id: int) -> None:
        self._db.execute("UPDATE stations SET last_played = ? WHERE id = ?", (time.time(), station_id))
        self._db.commit()

    def forget_recent(self, station_id: int) -> None:
        self._db.execute("UPDATE stations SET last_played = 0 WHERE id = ?", (station_id,))
        self._db.commit()

    # ----- what was heard -----

    def add_history(self, station_id: int, title: str) -> bool:
        """Note a title heard on a station. False if it is the same as the last one."""
        last = self._db.execute(
            "SELECT title FROM history WHERE station_id = ? ORDER BY heard_at DESC LIMIT 1",
            (station_id,),
        ).fetchone()
        if last and last[0] == title:
            return False
        self._db.execute(
            "INSERT INTO history (station_id, title, heard_at) VALUES (?, ?, ?)",
            (station_id, title, time.time()),
        )
        self._db.execute(
            "DELETE FROM history WHERE station_id = ? AND id NOT IN ("
            " SELECT id FROM history WHERE station_id = ? ORDER BY heard_at DESC LIMIT ?)",
            (station_id, station_id, HISTORY_LIMIT),
        )
        self._db.commit()
        return True

    def history(self, station_id: int) -> list[HeardSong]:
        rows = self._db.execute(
            "SELECT h.id, h.station_id, s.name, h.title, h.heard_at FROM history h "
            "JOIN stations s ON s.id = h.station_id WHERE h.station_id = ? "
            "ORDER BY h.heard_at DESC",
            (station_id,),
        )
        return [HeardSong(*row) for row in rows]

    def clear_history(self, station_id: int) -> None:
        self._db.execute("DELETE FROM history WHERE station_id = ?", (station_id,))
        self._db.commit()

    def remember(self, station_id: int, station: str, title: str) -> None:
        self._db.execute(
            "INSERT INTO remembered (station_id, station, title, heard_at) VALUES (?, ?, ?, ?)",
            (station_id, station, title, time.time()),
        )
        self._db.commit()

    def remembered(self) -> list[HeardSong]:
        rows = self._db.execute(
            "SELECT id, COALESCE(station_id, 0), station, title, heard_at FROM remembered "
            "ORDER BY heard_at DESC"
        )
        return [HeardSong(*row) for row in rows]

    def forget(self, remembered_ids: list[int]) -> None:
        self._db.executemany("DELETE FROM remembered WHERE id = ?", [(i,) for i in remembered_ids])
        self._db.commit()


# ----- Radio Browser (worker threads) -----

_server: str | None = None


def _get_json(path: str, params: dict | None = None):
    """Ask Radio Browser; its servers take turns, so another one is tried on failure."""
    global _server
    servers = [_server] if _server else []
    if not servers:
        try:
            request = urllib.request.Request(SERVERS_URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                names = [entry["name"] for entry in json.loads(response.read())]
            random.shuffle(names)
            servers = names
        except (OSError, ValueError, KeyError, TypeError):
            servers = []
    servers += [s for s in FALLBACK_SERVERS if s not in servers]
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    last_error: Exception | None = None
    for server in servers:
        try:
            request = urllib.request.Request(
                f"https://{server}{path}{query}", headers={"User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                result = json.loads(response.read())
            _server = server
            return result
        except (OSError, ValueError) as error:
            last_error = error
            if _server == server:
                _server = None
    raise last_error or OSError("Radio Browser cannot be reached")


def _station_from(entry: dict) -> Station:
    return Station(
        id=0,
        uuid=entry.get("stationuuid") or "",
        name=(entry.get("name") or "").strip() or "Unnamed station",
        url=entry.get("url_resolved") or entry.get("url") or "",
        homepage=entry.get("homepage") or "",
        favicon=entry.get("favicon") or "",
        country=entry.get("country") or "",
        language=entry.get("language") or "",
        tags=entry.get("tags") or "",
        codec=entry.get("codec") or "",
        bitrate=int(entry.get("bitrate") or 0),
        hls=bool(entry.get("hls")),
    )


def search_stations(name: str = "", country_code: str = "", language: str = "", tag: str = "") -> list[Station]:
    params = {
        "limit": RESULT_LIMIT, "hidebroken": "true", "order": "clickcount", "reverse": "true",
    }
    if name:
        params["name"] = name
    if country_code:
        params["countrycode"] = country_code
    if language:
        params["language"] = language
        params["languageExact"] = "true"
    if tag:
        params["tag"] = tag
        params["tagExact"] = "true"
    entries = _get_json("/json/stations/search", params)
    return [s for s in (_station_from(e) for e in entries) if s.url]


def popular_stations() -> list[Station]:
    entries = _get_json("/json/stations/topclick", {"limit": RESULT_LIMIT, "hidebroken": "true"})
    return [s for s in (_station_from(e) for e in entries) if s.url]


def browse_lists() -> dict:
    """The countries, languages and genres to choose from, the biggest first."""
    countries = _get_json("/json/countries", {"order": "stationcount", "reverse": "true", "hidebroken": "true"})
    languages = _get_json("/json/languages", {"order": "stationcount", "reverse": "true", "hidebroken": "true", "limit": 150})
    tags = _get_json("/json/tags", {"order": "stationcount", "reverse": "true", "hidebroken": "true", "limit": 250})
    return {
        "countries": sorted(
            ((c.get("name", ""), c.get("iso_3166_1", "")) for c in countries
             if c.get("name") and c.get("iso_3166_1") and int(c.get("stationcount") or 0) >= 5),
            key=lambda pair: pair[0].casefold(),
        ),
        "languages": [l["name"] for l in languages if l.get("name") and int(l.get("stationcount") or 0) >= 5],
        "tags": [t["name"] for t in tags if t.get("name")],
    }


def count_click(uuid: str) -> None:
    """Tell Radio Browser the station was played (it asks apps to)."""
    if uuid:
        _get_json(f"/json/url/{urllib.parse.quote(uuid)}")


def fetch_bytes(url: str, limit: int = 5 * 1024 * 1024) -> bytes:
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("not a web address")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("too large")
    return data


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, work: Callable, signals: _Signals) -> None:
        super().__init__()
        self._work = work
        self.signals = signals

    def run(self) -> None:
        try:
            result = self._work()
        except Exception as error:  # network and parse errors of every kind
            self.signals.failed.emit(str(getattr(error, "reason", None) or error))
        else:
            self.signals.done.emit(result)


class RadioManager(QObject):
    """Finding stations, favourites, history and remembered songs."""

    stations_found = Signal(str, list)  # a heading, [Station]
    lists_ready = Signal(dict)  # countries, languages, tags
    favourites_changed = Signal()
    station_changed = Signal(int)
    history_changed = Signal(int)  # station id
    remembered_changed = Signal()
    logo_ready = Signal(int)  # station id
    failed = Signal(str)
    status = Signal(str)

    def __init__(self, settings: QSettings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings_store = settings
        self.settings = load_radio_settings(settings)
        base = library_db.data_dir() / "radio"
        self.store = RadioStore(base / "radio.db")
        self._logos = base / "logos"
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._tasks: set[_Signals] = set()
        self._logo_requests: set[int] = set()

    def _run(self, work: Callable, done: Callable, failed: Callable | None = None) -> None:
        signals = _Signals()
        self._tasks.add(signals)

        def finish(result) -> None:
            self._tasks.discard(signals)
            done(result)

        def fail(message: str) -> None:
            self._tasks.discard(signals)
            (failed or self.failed.emit)(message)

        signals.done.connect(finish)
        signals.failed.connect(fail)
        self._pool.start(_Task(work, signals))

    def shutdown(self) -> None:
        self._pool.clear()
        self._pool.waitForDone(3000)
        self.store.close()

    def apply_settings(self, settings: RadioSettings) -> None:
        self.settings = settings
        save_radio_settings(self._settings_store, settings)

    # ----- finding stations -----

    def search(self, name: str, country_code: str, language: str, tag: str, heading: str) -> None:
        self.status.emit("Looking for stations…")
        self._run(
            lambda: search_stations(name, country_code, language, tag),
            lambda found: self._found(heading, found),
            lambda message: self.failed.emit(f"Radio Browser: {message}"),
        )

    def popular(self) -> None:
        self._run(
            popular_stations,
            lambda found: self._found("Popular stations", found),
            lambda message: self.failed.emit(f"Radio Browser: {message}"),
        )

    def _found(self, heading: str, stations: list[Station]) -> None:
        self.status.emit("")
        self.stations_found.emit(heading, stations)

    def load_lists(self) -> None:
        self._run(browse_lists, self.lists_ready.emit, lambda _m: None)

    def add_by_address(self, url: str, name: str = "") -> int:
        parsed = urllib.parse.urlparse(url)
        station = Station(
            id=0, uuid="", name=name or parsed.netloc or url, url=url, homepage="", favicon="",
            country="", language="", tags="", codec="", bitrate=0,
            hls=parsed.path.lower().endswith(".m3u8"),
        )
        return self.store.save(station)

    def keep(self, station: Station) -> int:
        """A found station, stored so it can be played, made a favourite or have a history."""
        station_id = station.id or self.store.save(station)
        self.fetch_logo(station_id)
        return station_id

    # ----- favourites and playing -----

    def set_favourite(self, station_id: int, favourite: bool) -> None:
        self.store.set_favourite(station_id, favourite)
        self.favourites_changed.emit()
        self.station_changed.emit(station_id)

    def rename(self, station_id: int, name: str) -> None:
        self.store.rename(station_id, name)
        self.favourites_changed.emit()
        self.station_changed.emit(station_id)

    def played(self, station_id: int) -> None:
        self.store.set_played(station_id)
        station = self.store.station(station_id)
        if station is not None and station.uuid:
            self._run(lambda: count_click(station.uuid), lambda _r: None, lambda _m: None)
        self.station_changed.emit(station_id)

    # ----- logos -----

    def logo_file(self, station_id: int) -> Path:
        return self._logos / f"{station_id}.img"

    def fetch_logo(self, station_id: int) -> None:
        station = self.store.station(station_id)
        target = self.logo_file(station_id)
        if station is None or not station.favicon or target.is_file():
            return
        if station_id in self._logo_requests:
            return
        self._logo_requests.add(station_id)
        url = station.favicon

        def done(data: bytes) -> None:
            self._logo_requests.discard(station_id)
            self._logos.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self.logo_ready.emit(station_id)

        def fail(_message: str) -> None:
            self._logo_requests.discard(station_id)

        self._run(lambda: fetch_bytes(url), done, fail)

    def fetch_image(self, url: str, done: Callable[[bytes], None]) -> None:
        self._run(lambda: fetch_bytes(url), done, lambda _m: None)

    # ----- what was heard -----

    def heard(self, station_id: int, title: str) -> None:
        if title and self.store.add_history(station_id, title):
            self.history_changed.emit(station_id)

    def remember(self, station_id: int, title: str) -> None:
        station = self.store.station(station_id)
        self.store.remember(station_id, station.name if station else "", title)
        self.remembered_changed.emit()

    def forget(self, remembered_ids: list[int]) -> None:
        self.store.forget(remembered_ids)
        self.remembered_changed.emit()

    def clear_history(self, station_id: int) -> None:
        self.store.clear_history(station_id)
        self.history_changed.emit(station_id)
