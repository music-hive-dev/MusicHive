"""Podcasts: finding them, subscribing, downloading episodes and tidying up.

Everything is kept in podcasts.db in the data folder; downloaded episodes go to
the download folder chosen in the settings (by default inside the data folder,
where the library scanner does not look).

Network work (searching, reading feeds, downloading) runs in worker threads. The
workers only fetch and parse; the database is only touched on the main thread.
"""

import email.utils
import json
import re
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, QThreadPool, QTimer, Signal

from src import library_db

USER_AGENT = "MusicHive/1.0 (podcasts)"
SEARCH_URL = "https://itunes.apple.com/search"
SEARCH_LIMIT = 30
FEED_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 60
MAX_FEED_BYTES = 30 * 1024 * 1024  # some feeds list thousands of episodes
CHUNK_BYTES = 256 * 1024
PROGRESS_EVERY_SECONDS = 0.25
MAX_DOWNLOADS_AT_ONCE = 2
FIRST_CHECK_DELAY_MS = 15_000  # after starting, before the feeds are first looked at
ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
AUDIO_SUFFIXES = {".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav", ".mp4"}
SUFFIX_FOR_TYPE = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/flac": ".flac",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
}

# ----- settings -----

FOLDER_KEY = "podcasts/folder"
CHECK_MINUTES_KEY = "podcasts/checkMinutes"
KEEP_COUNT_KEY = "podcasts/keepCount"
CONTINUE_KEY = "podcasts/continueWithNext"
DELETE_PLAYED_KEY = "podcasts/deletePlayed"
DELETE_OLD_KEY = "podcasts/deleteOld"
DELETE_OLD_DAYS_KEY = "podcasts/deleteOldDays"
SIZE_LIMIT_KEY = "podcasts/sizeLimit"
SIZE_LIMIT_GB_KEY = "podcasts/sizeLimitGb"


@dataclass(frozen=True)
class PodcastSettings:
    folder: str = ""  # "" means the default, inside the data folder
    check_minutes: int = 60  # how often subscribed podcasts are checked for new episodes
    keep_count: int = 3  # the next episodes to listen to kept downloaded, per podcast
    continue_with_next: bool = True  # after an episode ends, the next one starts
    delete_played: bool = True  # downloads are deleted once listened to
    delete_old: bool = False
    delete_old_days: int = 30  # downloads older than this are deleted
    size_limit: bool = False
    size_limit_gb: float = 5.0  # when the downloads take more, the oldest go

    def download_folder(self) -> Path:
        return Path(self.folder) if self.folder else default_download_folder()


def default_download_folder() -> Path:
    return library_db.data_dir() / "podcasts" / "episodes"


def load_podcast_settings(settings: QSettings) -> PodcastSettings:
    d = PodcastSettings()
    return PodcastSettings(
        folder=settings.value(FOLDER_KEY, d.folder, type=str),
        check_minutes=max(5, settings.value(CHECK_MINUTES_KEY, d.check_minutes, type=int)),
        keep_count=max(0, settings.value(KEEP_COUNT_KEY, d.keep_count, type=int)),
        continue_with_next=settings.value(CONTINUE_KEY, d.continue_with_next, type=bool),
        delete_played=settings.value(DELETE_PLAYED_KEY, d.delete_played, type=bool),
        delete_old=settings.value(DELETE_OLD_KEY, d.delete_old, type=bool),
        delete_old_days=max(1, settings.value(DELETE_OLD_DAYS_KEY, d.delete_old_days, type=int)),
        size_limit=settings.value(SIZE_LIMIT_KEY, d.size_limit, type=bool),
        size_limit_gb=max(0.1, settings.value(SIZE_LIMIT_GB_KEY, d.size_limit_gb, type=float)),
    )


def save_podcast_settings(settings: QSettings, style: PodcastSettings) -> None:
    for key, value in (
        (FOLDER_KEY, style.folder),
        (CHECK_MINUTES_KEY, style.check_minutes),
        (KEEP_COUNT_KEY, style.keep_count),
        (CONTINUE_KEY, style.continue_with_next),
        (DELETE_PLAYED_KEY, style.delete_played),
        (DELETE_OLD_KEY, style.delete_old),
        (DELETE_OLD_DAYS_KEY, style.delete_old_days),
        (SIZE_LIMIT_KEY, style.size_limit),
        (SIZE_LIMIT_GB_KEY, style.size_limit_gb),
    ):
        settings.setValue(key, value)


# ----- what is stored -----


@dataclass
class Podcast:
    id: int
    feed_url: str
    title: str
    author: str
    description: str
    image_url: str
    link: str
    subscribed: bool
    keep_count: int | None  # None: the setting's number
    last_checked: float


@dataclass
class Episode:
    id: int
    podcast_id: int
    guid: str
    title: str
    description: str
    published: float  # seconds since 1970, 0 if unknown
    url: str
    mime: str
    size: int  # bytes, as the feed says; 0 if unknown
    duration_ms: int  # 0 if unknown
    link: str
    file: str  # relative to the download folder; "" when not downloaded
    position_ms: int  # where listening stopped
    played: bool
    played_at: float
    keep: bool  # never deleted automatically
    auto_deleted: bool  # deleted by the tidy-up, so not downloaded again by itself
    downloaded_at: float

    @property
    def time_left_ms(self) -> int:
        if self.played:
            return 0
        return max(0, self.duration_ms - self.position_ms) if self.duration_ms else 0


@dataclass
class SeriesProgress:
    played: int
    total: int
    started: int  # episodes begun but not finished
    time_left_ms: int  # of the episodes not played yet (where their length is known)


@dataclass
class FeedItem:
    guid: str
    title: str
    description: str
    published: float
    url: str
    mime: str
    size: int
    duration_ms: int
    link: str


@dataclass
class FeedData:
    title: str
    author: str
    description: str
    image_url: str
    link: str
    items: list[FeedItem] = field(default_factory=list)


@dataclass
class SearchResult:
    title: str
    author: str
    feed_url: str
    image_url: str
    episode_count: int
    genre: str


_PODCAST_COLUMNS = "id, feed_url, title, author, description, image_url, link, subscribed, keep_count, last_checked"
_EPISODE_COLUMNS = (
    "id, podcast_id, guid, title, description, published, url, mime, size, duration_ms, link, "
    "file, position_ms, played, played_at, keep, auto_deleted, downloaded_at"
)


def _podcast(row) -> Podcast:
    return Podcast(
        row[0], row[1], row[2], row[3], row[4], row[5], row[6], bool(row[7]), row[8], row[9]
    )


def _episode(row) -> Episode:
    return Episode(
        row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9],
        row[10], row[11], row[12], bool(row[13]), row[14], bool(row[15]), bool(row[16]), row[17],
    )


class PodcastStore:
    """podcasts.db: the podcasts, their episodes and how far each was listened to."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS podcasts (
                id INTEGER PRIMARY KEY,
                feed_url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                image_url TEXT NOT NULL DEFAULT '',
                link TEXT NOT NULL DEFAULT '',
                subscribed INTEGER NOT NULL DEFAULT 0,
                keep_count INTEGER,
                last_checked REAL NOT NULL DEFAULT 0,
                cover_url TEXT NOT NULL DEFAULT '',
                subscribed_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY,
                podcast_id INTEGER NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
                guid TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                published REAL NOT NULL DEFAULT 0,
                url TEXT NOT NULL DEFAULT '',
                mime TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                link TEXT NOT NULL DEFAULT '',
                file TEXT NOT NULL DEFAULT '',
                position_ms INTEGER NOT NULL DEFAULT 0,
                played INTEGER NOT NULL DEFAULT 0,
                played_at REAL NOT NULL DEFAULT 0,
                keep INTEGER NOT NULL DEFAULT 0,
                auto_deleted INTEGER NOT NULL DEFAULT 0,
                downloaded_at REAL NOT NULL DEFAULT 0,
                first_seen REAL NOT NULL DEFAULT 0,
                last_listened REAL NOT NULL DEFAULT 0,
                UNIQUE (podcast_id, guid)
            );
            CREATE INDEX IF NOT EXISTS episodes_by_podcast ON episodes (podcast_id, published);
            """
        )
        # Added later: when each episode was last listened to.
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(episodes)")}
        if "last_listened" not in columns:
            self._db.execute(
                "ALTER TABLE episodes ADD COLUMN last_listened REAL NOT NULL DEFAULT 0"
            )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ----- podcasts -----

    def podcasts(self, subscribed_only: bool = True) -> list[Podcast]:
        where = "WHERE subscribed = 1" if subscribed_only else ""
        rows = self._db.execute(
            f"SELECT {_PODCAST_COLUMNS} FROM podcasts {where} ORDER BY title COLLATE NOCASE"
        )
        return [_podcast(row) for row in rows]

    def podcast(self, podcast_id: int) -> Podcast | None:
        row = self._db.execute(
            f"SELECT {_PODCAST_COLUMNS} FROM podcasts WHERE id = ?", (podcast_id,)
        ).fetchone()
        return _podcast(row) if row else None

    def podcast_id_for(self, feed_url: str) -> int | None:
        row = self._db.execute("SELECT id FROM podcasts WHERE feed_url = ?", (feed_url,)).fetchone()
        return row[0] if row else None

    def save_feed(self, feed_url: str, feed: FeedData) -> tuple[int, list[int]]:
        """Store a feed as read. Returns the podcast's id and the ids of new episodes."""
        podcast_id = self.podcast_id_for(feed_url)
        if podcast_id is None:
            podcast_id = self._db.execute(
                "INSERT INTO podcasts (feed_url) VALUES (?)", (feed_url,)
            ).lastrowid
        self._db.execute(
            "UPDATE podcasts SET title = ?, author = ?, description = ?, image_url = ?, "
            "link = ?, last_checked = ? WHERE id = ?",
            (feed.title, feed.author, feed.description, feed.image_url, feed.link,
             time.time(), podcast_id),
        )
        new = []
        now = time.time()
        for item in feed.items:
            found = self._db.execute(
                "SELECT id FROM episodes WHERE podcast_id = ? AND guid = ?", (podcast_id, item.guid)
            ).fetchone()
            values = (item.title, item.description, item.published, item.url, item.mime,
                      item.size, item.link)
            if found:
                self._db.execute(
                    "UPDATE episodes SET title = ?, description = ?, published = ?, url = ?, "
                    "mime = ?, size = ?, link = ?, "
                    "duration_ms = CASE WHEN ? > 0 THEN ? ELSE duration_ms END WHERE id = ?",
                    (*values, item.duration_ms, item.duration_ms, found[0]),
                )
            else:
                new.append(self._db.execute(
                    "INSERT INTO episodes (podcast_id, guid, title, description, published, url, "
                    "mime, size, link, duration_ms, first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (podcast_id, item.guid, *values, item.duration_ms, now),
                ).lastrowid)
        self._db.commit()
        return podcast_id, new

    def set_subscribed(self, podcast_id: int, subscribed: bool) -> None:
        self._db.execute(
            "UPDATE podcasts SET subscribed = ?, subscribed_at = ? WHERE id = ?",
            (int(subscribed), time.time() if subscribed else 0, podcast_id),
        )
        self._db.commit()

    def set_keep_count(self, podcast_id: int, count: int | None) -> None:
        self._db.execute("UPDATE podcasts SET keep_count = ? WHERE id = ?", (count, podcast_id))
        self._db.commit()

    def cover_url(self, podcast_id: int) -> str:
        row = self._db.execute("SELECT cover_url FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
        return row[0] if row else ""

    def set_cover_url(self, podcast_id: int, url: str) -> None:
        self._db.execute("UPDATE podcasts SET cover_url = ? WHERE id = ?", (url, podcast_id))
        self._db.commit()

    def delete_podcast(self, podcast_id: int) -> None:
        self._db.execute("DELETE FROM podcasts WHERE id = ?", (podcast_id,))
        self._db.commit()

    def forgotten_previews(self) -> list[int]:
        """Podcasts only looked at, never subscribed to, with nothing worth keeping."""
        rows = self._db.execute(
            "SELECT p.id FROM podcasts p WHERE p.subscribed = 0 AND NOT EXISTS ("
            " SELECT 1 FROM episodes e WHERE e.podcast_id = p.id"
            " AND (e.file != '' OR e.position_ms > 0 OR e.played = 1 OR e.keep = 1))"
        )
        return [row[0] for row in rows]

    # ----- episodes -----

    def episodes(self, podcast_id: int) -> list[Episode]:
        """A podcast's episodes, newest first."""
        rows = self._db.execute(
            f"SELECT {_EPISODE_COLUMNS} FROM episodes WHERE podcast_id = ? "
            "ORDER BY published DESC, id ASC",  # undated: as the feed lists them, newest first
            (podcast_id,),
        )
        return [_episode(row) for row in rows]

    def episode(self, episode_id: int) -> Episode | None:
        row = self._db.execute(
            f"SELECT {_EPISODE_COLUMNS} FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        return _episode(row) if row else None

    def downloaded(self) -> list[Episode]:
        rows = self._db.execute(f"SELECT {_EPISODE_COLUMNS} FROM episodes WHERE file != ''")
        return [_episode(row) for row in rows]

    def progress(self, podcast_id: int) -> SeriesProgress:
        episodes = self.episodes(podcast_id)
        return SeriesProgress(
            played=sum(e.played for e in episodes),
            total=len(episodes),
            started=sum(1 for e in episodes if not e.played and e.position_ms > 0),
            time_left_ms=sum(e.time_left_ms for e in episodes),
        )

    def released_since_subscribing(self, podcast_id: int) -> list[Episode]:
        """Episodes that came out while subscribed (not the ones there before)."""
        rows = self._db.execute(
            f"SELECT {', '.join('e.' + c.strip() for c in _EPISODE_COLUMNS.split(','))} "
            "FROM episodes e JOIN podcasts p ON p.id = e.podcast_id "
            "WHERE e.podcast_id = ? AND p.subscribed = 1 AND e.first_seen > p.subscribed_at "
            "ORDER BY e.published",
            (podcast_id,),
        )
        return [_episode(row) for row in rows]

    def new_count(self, podcast_id: int) -> int:
        """Episodes not listened to at all, released since subscribing."""
        row = self._db.execute(
            "SELECT COUNT(*) FROM episodes e JOIN podcasts p ON p.id = e.podcast_id "
            "WHERE e.podcast_id = ? AND e.played = 0 AND e.position_ms = 0 "
            "AND e.first_seen > p.subscribed_at",
            (podcast_id,),
        ).fetchone()
        return row[0] if row else 0

    def set_position(self, episode_id: int, position_ms: int, duration_ms: int = 0) -> None:
        """Where listening got to; it also counts as when the episode was last listened to."""
        self._db.execute(
            "UPDATE episodes SET position_ms = ?, last_listened = ?, "
            "duration_ms = CASE WHEN ? > 0 THEN ? ELSE duration_ms END WHERE id = ?",
            (position_ms, time.time(), duration_ms, duration_ms, episode_id),
        )
        self._db.commit()

    def last_listened(self, podcast_id: int) -> Episode | None:
        """The podcast's episode listened to most recently, if any."""
        row = self._db.execute(
            f"SELECT {_EPISODE_COLUMNS} FROM episodes WHERE podcast_id = ? AND last_listened > 0 "
            "ORDER BY last_listened DESC LIMIT 1",
            (podcast_id,),
        ).fetchone()
        return _episode(row) if row else None

    def set_played(self, episode_ids: list[int], played: bool) -> None:
        self._db.executemany(
            "UPDATE episodes SET played = ?, played_at = ?, position_ms = 0 WHERE id = ?",
            [(int(played), time.time() if played else 0, i) for i in episode_ids],
        )
        self._db.commit()

    def set_keep(self, episode_ids: list[int], keep: bool) -> None:
        self._db.executemany(
            "UPDATE episodes SET keep = ? WHERE id = ?", [(int(keep), i) for i in episode_ids]
        )
        self._db.commit()

    def set_file(self, episode_id: int, file: str, size: int) -> None:
        self._db.execute(
            "UPDATE episodes SET file = ?, size = CASE WHEN ? > 0 THEN ? ELSE size END, "
            "downloaded_at = ?, auto_deleted = 0 WHERE id = ?",
            (file, size, size, time.time(), episode_id),
        )
        self._db.commit()

    def clear_file(self, episode_id: int, automatically: bool) -> None:
        self._db.execute(
            "UPDATE episodes SET file = '', downloaded_at = 0, "
            "auto_deleted = CASE WHEN ? THEN 1 ELSE auto_deleted END WHERE id = ?",
            (int(automatically), episode_id),
        )
        self._db.commit()


# ----- reading feeds and searching (worker threads) -----


def _fetch(url: str, timeout: float = FEED_TIMEOUT_SECONDS, limit: int = MAX_FEED_BYTES) -> bytes:
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("Only http and https addresses can be used")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The feed is too large")
    return data


def _text(element, path: str) -> str:
    found = element.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_duration(text: str) -> int:
    """An itunes:duration ("1:02:03", "62:03" or "3723") in milliseconds."""
    text = text.strip()
    if not text:
        return 0
    try:
        seconds = 0.0
        for part in text.split(":"):
            seconds = seconds * 60 + float(part)
        return int(seconds * 1000)
    except ValueError:
        return 0


def parse_date(text: str) -> float:
    try:
        return email.utils.parsedate_to_datetime(text).timestamp() if text else 0.0
    except (TypeError, ValueError, IndexError, OverflowError):
        return 0.0


def parse_feed(data: bytes) -> FeedData:
    """An RSS podcast feed. Episodes without a sound file are left out."""
    root = ElementTree.fromstring(data)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("This is not a podcast feed")
    image = channel.find(f"{ITUNES_NS}image")
    image_url = image.get("href", "") if image is not None else ""
    feed = FeedData(
        title=_text(channel, "title") or "Untitled podcast",
        author=_text(channel, f"{ITUNES_NS}author") or _text(channel, "managingEditor"),
        description=_text(channel, "description") or _text(channel, f"{ITUNES_NS}summary"),
        image_url=image_url or _text(channel, "image/url"),
        link=_text(channel, "link"),
    )
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        url = enclosure.get("url", "").strip() if enclosure is not None else ""
        if not url:
            continue
        try:
            size = int(enclosure.get("length") or 0)
        except ValueError:
            size = 0
        feed.items.append(FeedItem(
            guid=_text(item, "guid") or url,
            title=_text(item, "title") or "Untitled episode",
            description=_text(item, "description") or _text(item, f"{ITUNES_NS}summary"),
            published=parse_date(_text(item, "pubDate")),
            url=url,
            mime=enclosure.get("type", ""),
            size=max(0, size),
            duration_ms=parse_duration(_text(item, f"{ITUNES_NS}duration")),
            link=_text(item, "link"),
        ))
    return feed


def read_feed(url: str) -> FeedData:
    return parse_feed(_fetch(url))


def search_podcasts(term: str) -> list[SearchResult]:
    """Apple Podcasts' directory (no account or key needed)."""
    query = urllib.parse.urlencode(
        {"media": "podcast", "entity": "podcast", "limit": SEARCH_LIMIT, "term": term}
    )
    data = json.loads(_fetch(f"{SEARCH_URL}?{query}").decode("utf-8"))
    results = []
    for entry in data.get("results", []):
        feed_url = entry.get("feedUrl") or ""
        if not feed_url:
            continue  # some entries cannot be subscribed to
        results.append(SearchResult(
            title=entry.get("collectionName") or entry.get("trackName") or "",
            author=entry.get("artistName") or "",
            feed_url=feed_url,
            image_url=entry.get("artworkUrl600") or entry.get("artworkUrl100") or "",
            episode_count=int(entry.get("trackCount") or 0),
            genre=entry.get("primaryGenreName") or "",
        ))
    return results


def download_file(url: str, target: Path, report: Callable[[int, int], None],
                  cancelled: threading.Event) -> int:
    """Download `url` to `target` (through a .part file). Returns its size in bytes."""
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("Only http and https addresses can be downloaded")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response, \
                open(partial, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            last_report = 0.0
            while chunk := response.read(CHUNK_BYTES):
                if cancelled.is_set():
                    raise InterruptedError
                out.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last_report >= PROGRESS_EVERY_SECONDS:
                    report(done, total)
                    last_report = now
        partial.replace(target)
        return done
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


# ----- running work in the background -----


class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)


class _Task(QRunnable):
    def __init__(self, work: Callable, signals: _TaskSignals) -> None:
        super().__init__()
        self._work = work
        self.signals = signals

    def run(self) -> None:
        try:
            result = self._work(self.signals)
        except InterruptedError:
            self.signals.failed.emit("")  # cancelled: nothing to report
        except Exception as error:  # network and parse errors of every kind
            self.signals.failed.emit(_describe(error))
        else:
            self.signals.done.emit(result)


def _describe(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return f"The server answered {error.code} ({error.reason})"
    if isinstance(error, urllib.error.URLError):
        return f"Could not connect: {error.reason}"
    if isinstance(error, ElementTree.ParseError):
        return "This is not a podcast feed"
    return str(error) or error.__class__.__name__


def safe_name(text: str, limit: int = 80) -> str:
    """Text that can be used as a file or folder name."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:limit].rstrip(" .") or "Untitled"


def suffix_for(episode: Episode) -> str:
    suffix = Path(urllib.parse.urlparse(episode.url).path).suffix.lower()
    if suffix in AUDIO_SUFFIXES:
        return suffix
    return SUFFIX_FOR_TYPE.get(episode.mime.split(";")[0].strip().lower(), ".mp3")


# ----- the manager -----


class PodcastManager(QObject):
    """Finds, subscribes, checks for new episodes, downloads and tidies up."""

    podcasts_changed = Signal()  # the list of subscriptions changed
    podcast_changed = Signal(int)  # a podcast's details or episodes changed
    episode_changed = Signal(int)  # an episode's download, progress or flags changed
    download_progress = Signal(int, float)  # episode id, 0..1 (or -1 when unknown)
    search_finished = Signal(list)  # [SearchResult]
    feed_opened = Signal(int)  # a podcast's feed was read: show it
    cover_ready = Signal(int)  # a podcast's cover image was saved
    failed = Signal(str)  # something went wrong; for the status bar
    status = Signal(str)

    def __init__(self, settings: QSettings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings_store = settings
        self.settings = load_podcast_settings(settings)
        base = library_db.data_dir() / "podcasts"
        self.store = PodcastStore(base / "podcasts.db")
        self._covers = base / "covers"
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._tasks: set[_TaskSignals] = set()  # kept alive until they report
        self._queue: list[int] = []  # episodes waiting to download
        self._downloads: dict[int, threading.Event] = {}  # downloading now: their cancel flag
        self.progress_of: dict[int, float] = {}  # episode id -> 0..1, -1 when unknown
        self._checking: set[int] = set()
        self.playing_episode: int | None = None  # never deleted while it plays
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.check_all)
        self._timer.start(self.settings.check_minutes * 60_000)
        QTimer.singleShot(FIRST_CHECK_DELAY_MS, self.check_all)
        for podcast_id in self.store.forgotten_previews():
            self.store.delete_podcast(podcast_id)
        self.tidy_up()

    # ----- background work -----

    def _run(self, work: Callable, done: Callable, failed: Callable | None = None,
             progress: Callable | None = None) -> None:
        signals = _TaskSignals()
        self._tasks.add(signals)

        def finish(result) -> None:
            self._tasks.discard(signals)
            done(result)

        def fail(message: str) -> None:
            self._tasks.discard(signals)
            (failed or self.failed.emit)(message)

        signals.done.connect(finish)
        signals.failed.connect(fail)
        if progress is not None:
            signals.progress.connect(progress)
        self._pool.start(_Task(work, signals))

    def shutdown(self) -> None:
        self._timer.stop()
        self._queue.clear()
        for cancel in self._downloads.values():
            cancel.set()
        self._pool.clear()
        self._pool.waitForDone(3000)
        self.store.close()

    # ----- settings -----

    def apply_settings(self, settings: PodcastSettings) -> None:
        self.settings = settings
        save_podcast_settings(self._settings_store, settings)
        self._timer.start(settings.check_minutes * 60_000)
        self.auto_download()
        self.tidy_up()

    def change_folder(self, folder: str) -> int:
        """Use another download folder, moving the downloads there.
        Returns how many could not be moved (those count as not downloaded)."""
        old_root = self.settings.download_folder()
        new_settings = replace(self.settings, folder=folder)
        new_root = new_settings.download_folder()
        failed = 0
        if new_root.resolve() != old_root.resolve():
            for episode in self.store.downloaded():
                source, target = old_root / episode.file, new_root / episode.file
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(target))
                except OSError:
                    failed += 1
                    self.store.clear_file(episode.id, automatically=False)
        self.apply_settings(new_settings)
        return failed

    def keep_count(self, podcast: Podcast) -> int:
        return self.settings.keep_count if podcast.keep_count is None else podcast.keep_count

    # ----- finding podcasts -----

    def search(self, term: str) -> None:
        self.status.emit(f"Searching for “{term}”…")
        self._run(
            lambda _signals: search_podcasts(term),
            self.search_finished.emit,
            lambda message: self.failed.emit(f"Search failed: {message}"),
        )

    def open_feed(self, feed_url: str) -> None:
        """Read a feed and show it, subscribed or not."""
        feed_url = feed_url.strip()
        self.status.emit("Reading the podcast's feed…")

        def done(feed: FeedData) -> None:
            podcast_id, _new = self.store.save_feed(feed_url, feed)
            self._fetch_cover(podcast_id)
            self.feed_opened.emit(podcast_id)
            self.status.emit("")

        self._run(
            lambda _signals: read_feed(feed_url),
            done,
            lambda message: self.failed.emit(f"Could not read the feed: {message}"),
        )

    def subscribe(self, podcast_id: int) -> None:
        self.store.set_subscribed(podcast_id, True)
        self.podcasts_changed.emit()
        self.podcast_changed.emit(podcast_id)
        self.auto_download(podcast_id)

    def unsubscribe(self, podcast_id: int, delete_downloads: bool) -> None:
        self.store.set_subscribed(podcast_id, False)
        for episode in self.store.episodes(podcast_id):
            self.cancel_download(episode.id)
            if delete_downloads and episode.file and episode.id != self.playing_episode:
                self.delete_download(episode.id)
        self.podcasts_changed.emit()
        self.podcast_changed.emit(podcast_id)

    def set_keep_count(self, podcast_id: int, count: int) -> None:
        self.store.set_keep_count(podcast_id, count)
        self.auto_download(podcast_id)

    # ----- checking for new episodes -----

    def check_all(self) -> None:
        podcasts = self.store.podcasts()
        if podcasts:
            self.status.emit("Checking podcasts for new episodes…")
        for podcast in podcasts:
            self.check(podcast.id)

    def check(self, podcast_id: int) -> None:
        podcast = self.store.podcast(podcast_id)
        if podcast is None or podcast_id in self._checking:
            return
        self._checking.add(podcast_id)
        feed_url = podcast.feed_url

        def done(feed: FeedData) -> None:
            self._checking.discard(podcast_id)
            if self.store.podcast(podcast_id) is None:
                return
            _id, new = self.store.save_feed(feed_url, feed)
            self._fetch_cover(podcast_id)
            self.podcast_changed.emit(podcast_id)
            if new and podcast.subscribed:
                count = len(new)
                self.status.emit(
                    f"{feed.title}: {count} new episode{'' if count == 1 else 's'}"
                )
            self.auto_download(podcast_id)
            if not self._checking:
                self.tidy_up()

        def fail(message: str) -> None:
            self._checking.discard(podcast_id)
            self.failed.emit(f"{podcast.title}: {message}")

        self._run(lambda _signals: read_feed(feed_url), done, fail)

    # ----- covers -----

    def cover_file(self, podcast_id: int) -> Path:
        return self._covers / f"{podcast_id}.img"

    def _fetch_cover(self, podcast_id: int) -> None:
        podcast = self.store.podcast(podcast_id)
        if podcast is None or not podcast.image_url:
            return
        target = self.cover_file(podcast_id)
        if target.is_file() and self.store.cover_url(podcast_id) == podcast.image_url:
            return
        url = podcast.image_url

        def done(data: bytes) -> None:
            self._covers.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self.store.set_cover_url(podcast_id, url)
            self.cover_ready.emit(podcast_id)

        self._run(lambda _signals: _fetch(url, limit=20 * 1024 * 1024), done, lambda _m: None)

    # ----- downloading -----

    def file_path(self, episode: Episode) -> Path | None:
        if not episode.file:
            return None
        path = self.settings.download_folder() / episode.file
        return path if path.is_file() else None

    def source_for(self, episode: Episode) -> Path | str:
        """What to play: the downloaded file, or else the web address to stream."""
        return self.file_path(episode) or episode.url

    def fetch_image(self, url: str, done: Callable[[bytes], None]) -> None:
        """Fetch a picture (a search result's cover) in the background."""
        self._run(lambda _signals: _fetch(url, limit=10 * 1024 * 1024), done, lambda _m: None)

    def is_active_download(self, episode_id: int) -> bool:
        """Being downloaded right now, not just waiting its turn."""
        return episode_id in self._downloads

    def is_downloading(self, episode_id: int) -> bool:
        return episode_id in self._downloads or episode_id in self._queue

    def download(self, episode_id: int, first: bool = False) -> None:
        episode = self.store.episode(episode_id)
        if episode is None or self.file_path(episode) is not None:
            return
        if episode_id in self._downloads:
            return
        if episode_id in self._queue:
            if not first:
                return
            self._queue.remove(episode_id)
        if first:
            self._queue.insert(0, episode_id)
        else:
            self._queue.append(episode_id)
        self.progress_of[episode_id] = 0.0
        self.episode_changed.emit(episode_id)
        self._start_downloads()

    def cancel_download(self, episode_id: int) -> None:
        if episode_id in self._queue:
            self._queue.remove(episode_id)
        if episode_id in self._downloads:
            self._downloads[episode_id].set()
        if self.progress_of.pop(episode_id, None) is not None:
            self.episode_changed.emit(episode_id)

    def _start_downloads(self) -> None:
        while self._queue and len(self._downloads) < MAX_DOWNLOADS_AT_ONCE:
            episode_id = self._queue.pop(0)
            episode = self.store.episode(episode_id)
            podcast = self.store.podcast(episode.podcast_id) if episode else None
            if episode is None or podcast is None:
                continue
            self._download(episode, podcast)

    def _download(self, episode: Episode, podcast: Podcast) -> None:
        folder = self.settings.download_folder()
        date = time.strftime("%Y-%m-%d ", time.localtime(episode.published)) if episode.published else ""
        relative = Path(safe_name(podcast.title)) / f"{date}{safe_name(episode.title)}{suffix_for(episode)}"
        target = folder / relative
        number = 2
        while target.exists():  # another episode with the same name and date
            relative = relative.with_name(f"{relative.stem} ({number}){relative.suffix}")
            target = folder / relative
            number += 1
        cancel = threading.Event()
        self._downloads[episode.id] = cancel
        episode_id, url = episode.id, episode.url

        def work(signals: _TaskSignals) -> int:
            return download_file(url, target, signals.progress.emit, cancel)

        def progress(done: int, total: int) -> None:
            self.progress_of[episode_id] = done / total if total else -1.0
            self.download_progress.emit(episode_id, self.progress_of[episode_id])

        def finished(size: int) -> None:
            self._downloads.pop(episode_id, None)
            self.progress_of.pop(episode_id, None)
            self.store.set_file(episode_id, relative.as_posix(), size)
            self.episode_changed.emit(episode_id)
            self._start_downloads()
            self.tidy_up()

        def fail(message: str) -> None:
            self._downloads.pop(episode_id, None)
            self.progress_of.pop(episode_id, None)
            self.episode_changed.emit(episode_id)
            if message:  # "" when it was cancelled
                self.failed.emit(f"Download of “{episode.title}” failed: {message}")
            self._start_downloads()

        self._run(work, finished, fail, progress)

    def delete_download(self, episode_id: int, automatically: bool = False) -> None:
        episode = self.store.episode(episode_id)
        if episode is None or not episode.file:
            return
        path = self.settings.download_folder() / episode.file
        try:
            path.unlink(missing_ok=True)
            if path.parent != self.settings.download_folder() and not any(path.parent.iterdir()):
                path.parent.rmdir()  # the podcast's folder, now empty
        except OSError:
            pass
        self.store.clear_file(episode_id, automatically)
        self.episode_changed.emit(episode_id)

    @staticmethod
    def up_next(episodes: list[Episode], count: int) -> list[Episode]:
        """The next `count` episodes to listen to, in order of release.

        They follow the latest released episode that was played or started (that
        one too, if it is not finished). With none of them, they are the first
        episodes of the podcast: 1, 2, 3 and so on.
        """
        in_order = sorted(episodes, key=lambda e: (e.published, -e.id))  # oldest first
        reached = [i for i, e in enumerate(in_order) if e.played or e.position_ms > 0]
        start = reached[-1] if reached else 0
        return [e for e in in_order[start:] if not e.played][:count]

    def episode_to_continue(self, podcast_id: int) -> int | None:
        """What "Play" on a podcast plays: the episode last listened to, where it was
        left off; if that one was finished, the next unplayed one after it. With none
        listened to yet, the first unplayed episode. None when everything is played."""
        episodes = self.store.episodes(podcast_id)
        last = self.store.last_listened(podcast_id)
        if last is None:
            first = self.up_next(episodes, 1)
            return first[0].id if first else None
        if not last.played:
            return last.id
        in_order = sorted(episodes, key=lambda e: (e.published, -e.id))  # oldest first
        ids = [e.id for e in in_order]
        after = in_order[ids.index(last.id) + 1:] if last.id in ids else []
        return next((e.id for e in after if not e.played), None)

    def auto_download(self, podcast_id: int | None = None) -> None:
        """Keep each subscribed podcast's next episodes downloaded, and every
        episode released since subscribing that has not been played."""
        podcasts = [self.store.podcast(podcast_id)] if podcast_id else self.store.podcasts()
        for podcast in podcasts:
            if podcast is None or not podcast.subscribed:
                continue
            episodes = self.store.episodes(podcast.id)
            wanted = self.up_next(episodes, self.keep_count(podcast))
            wanted += [e for e in self.store.released_since_subscribing(podcast.id)
                       if e not in wanted]
            for episode in wanted:
                if episode.played or episode.auto_deleted or episode.file:
                    continue
                if not self.is_downloading(episode.id):
                    self.download(episode.id)

    # ----- listening -----

    def set_position(self, episode_id: int, position_ms: int, duration_ms: int = 0) -> None:
        self.store.set_position(episode_id, position_ms, duration_ms)

    def set_played(self, episode_ids: list[int], played: bool) -> None:
        self.store.set_played(episode_ids, played)
        podcasts = set()
        for episode_id in episode_ids:
            self.episode_changed.emit(episode_id)
            episode = self.store.episode(episode_id)
            if episode is not None:
                podcasts.add(episode.podcast_id)
        for podcast_id in podcasts:
            self.podcast_changed.emit(podcast_id)
            self.auto_download(podcast_id)  # the next unplayed ones take their place
        self.tidy_up()

    def set_keep(self, episode_ids: list[int], keep: bool) -> None:
        self.store.set_keep(episode_ids, keep)
        for episode_id in episode_ids:
            self.episode_changed.emit(episode_id)

    # ----- tidying up -----

    def tidy_up(self) -> None:
        """Delete downloads as the settings say. Kept episodes and the one playing stay."""
        s = self.settings
        downloaded = [
            e for e in self.store.downloaded() if not e.keep and e.id != self.playing_episode
        ]
        gone: set[int] = set()
        now = time.time()
        for episode in downloaded:
            too_old = s.delete_old and now - episode.downloaded_at > s.delete_old_days * 86_400
            if (s.delete_played and episode.played) or too_old:
                self.delete_download(episode.id, automatically=True)
                gone.add(episode.id)
        if s.size_limit:
            folder = s.download_folder()
            limit = s.size_limit_gb * 1000**3
            sizes = {}
            for episode in self.store.downloaded():
                try:
                    sizes[episode.id] = (folder / episode.file).stat().st_size
                except OSError:
                    sizes[episode.id] = 0
            total = sum(sizes.values())
            # Played ones first, then the oldest releases.
            candidates = sorted(
                (e for e in downloaded if e.id not in gone),
                key=lambda e: (not e.played, e.played_at if e.played else e.published),
            )
            for episode in candidates:
                if total <= limit:
                    break
                self.delete_download(episode.id, automatically=True)
                total -= sizes.get(episode.id, 0)
                gone.add(episode.id)
        if gone:
            count = len(gone)
            self.status.emit(f"Deleted {count} podcast download{'' if count == 1 else 's'}")
