"""Per-library SQLite databases, stored in a hidden folder in the Music folder.

Everything lives in ~/Music/.musichive/. Each library gets its own subfolder
there, ~/Music/.musichive/<library name>/, holding its database (musichive.db)
and any other files MusicHive keeps for that library.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.track_metadata import TrackInfo

SCHEMA_VERSION = 8
DB_NAME = "musichive.db"
DATA_DIR_NAME = ".musichive"

# Every text tag of every track, one row per tag (see track_metadata.read_all_tags).
_TRACK_TAGS_TABLE = """
CREATE TABLE IF NOT EXISTS track_tags (
    rel_path TEXT NOT NULL,
    name     TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (rel_path, name)
) WITHOUT ROWID;
"""

# When each song first joined the library. Rescans rebuild the tracks table, so
# this lives apart from it, as the play counts do.
_TRACK_ADDED_TABLE = """
CREATE TABLE IF NOT EXISTS track_added (
    rel_path TEXT PRIMARY KEY,
    added_at REAL NOT NULL
);
"""

# Star ratings, kept apart from the tracks table so that rescans leave them alone.
_RATINGS_TABLE = """
CREATE TABLE IF NOT EXISTS ratings (
    rel_path TEXT PRIMARY KEY,
    rating   INTEGER NOT NULL
);
"""

_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE covers (
    cover_key TEXT PRIMARY KEY,
    thumbnail BLOB NOT NULL
);
CREATE TABLE tracks (
    position     INTEGER PRIMARY KEY,
    rel_path     TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    artist       TEXT NOT NULL,
    album_artist TEXT NOT NULL DEFAULT '',
    album        TEXT NOT NULL,
    year         TEXT NOT NULL,
    genre        TEXT NOT NULL DEFAULT '',
    track_number INTEGER,
    disc_number  INTEGER,
    duration_ms  INTEGER,
    file_size    INTEGER,
    modified     REAL,
    cover_key    TEXT REFERENCES covers(cover_key)
);

CREATE TABLE plays (
    rel_path    TEXT PRIMARY KEY,
    count       INTEGER NOT NULL,
    last_played REAL
);
""" + _TRACK_TAGS_TABLE + _TRACK_ADDED_TABLE + _RATINGS_TABLE


@dataclass(frozen=True)
class LibraryInfo:
    id: str
    name: str
    root: Path  # the folder that holds the music
    db_path: Path  # <Music folder>/<library name>/musichive.db
    scanned_at: str | None  # ISO timestamp of the last scan; None if never scanned
    track_count: int


@dataclass
class ScannedTrack:
    rel_path: str  # path relative to the library root, with "/" separators
    info: TrackInfo
    file_size: int | None
    modified: float | None


@dataclass
class LoadedLibrary:
    paths: list[Path]  # full paths, in list order
    infos: list[TrackInfo]  # same order as paths
    covers: dict[str, bytes]  # cover_key -> JPEG thumbnail bytes


@dataclass(frozen=True)
class LibraryStats:
    file_count: int
    total_bytes: int
    total_ms: int


@dataclass
class SavedSession:
    rel_path: str  # the playing track, relative to the library root
    position_ms: int  # how far into the track
    order: list[int]  # library rows in play order (a row can appear more than once)
    order_position: int  # where the playing track is within `order`
    queued: list[bool] = field(default_factory=list)  # which entries the user queued
    stop_after_index: int = -1  # entry playback ends after, or -1
    playlist: str = ""  # the playlist the order comes from ("" means the library)


def track_key(artist: str, title: str) -> str:
    """A normalised "artist + title", for matching tracks with Last.fm's.

    Ignores case, accents, extra spaces and typographic apostrophes.
    """

    def clean(text: str) -> str:
        text = text.replace("\u2019", "'").replace("\u2018", "'")
        decomposed = unicodedata.normalize("NFKD", text.casefold())
        plain = "".join(char for char in decomposed if not unicodedata.combining(char))
        return " ".join(plain.split())

    return f"{clean(artist)}\x00{clean(title)}"


# ----- where things live -----


def music_dir() -> Path:
    """The user's Music folder: the XDG setting, or ~/Music."""
    home = Path.home()
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    try:
        text = (Path(config_home) / "user-dirs.dirs").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("XDG_MUSIC_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
                path = Path(value.replace("$HOME", str(home)))
                if path != home:  # "$HOME/" means the Music folder is disabled
                    return path
    except OSError:
        pass
    return home / "Music"

def data_dir() -> Path:
    """The hidden folder where MusicHive keeps its per-library data."""
    return music_dir() / DATA_DIR_NAME


def old_libraries_dir() -> Path:
    """Where earlier versions kept the databases (see migrate_old_libraries)."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "musichive" / "libraries"


def _id_for(root_text: str) -> str:
    """Stable id for a library, derived from its music folder's path."""
    return hashlib.sha1(root_text.encode("utf-8")).hexdigest()[:16]


def _free_folder(name: str) -> Path:
    """A folder inside the data folder for a new library called `name`.

    "Name" if no other library uses it, otherwise "Name (2)", "Name (3)", ...
    """
    base = data_dir()
    name = name.strip() or "Library"
    folder = base / name
    number = 2
    while (folder / DB_NAME).exists():
        folder = base / f"{name} ({number})"
        number += 1
    return folder


@contextmanager
def connect(db_path: Path):
    """Open a short-lived connection. Each thread opens its own."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        _upgrade_schema(conn)
        yield conn
    finally:
        conn.close()


def _upgrade_schema(conn: sqlite3.Connection) -> None:
    """Bring a database made by an older version up to the current schema."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version in (0, SCHEMA_VERSION):
        return  # a brand-new database, or one that is already current
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
        for name in ("album_artist", "genre"):
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE tracks ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS plays "
            "(rel_path TEXT PRIMARY KEY, count INTEGER NOT NULL)"
        )
        play_columns = {row[1] for row in conn.execute("PRAGMA table_info(plays)")}
        if "last_played" not in play_columns:
            conn.execute("ALTER TABLE plays ADD COLUMN last_played REAL")
        conn.execute(_TRACK_TAGS_TABLE)
        conn.execute(_TRACK_ADDED_TABLE)
        conn.execute(_RATINGS_TABLE)
        # Nobody noted when existing songs arrived: their file date is the best guess.
        conn.execute(
            "INSERT OR IGNORE INTO track_added (rel_path, added_at) "
            "SELECT rel_path, COALESCE(modified, ?) FROM tracks",
            (datetime.now().timestamp(),),
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # another connection upgraded it at the same moment


# ----- libraries -----


def add_library(root: Path) -> tuple[LibraryInfo, bool]:
    """Create a library for the music folder `root`.

    Returns (library, created). `created` is False if that folder already is
    a library. Raises OSError or sqlite3.Error on failure.
    """
    root = root.resolve()
    for existing in list_libraries():
        if existing.root == root:
            return existing, False

    name = root.name or "Library"
    folder = _free_folder(name)
    db_path = folder / DB_NAME
    folder_is_new = not folder.exists()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with connect(db_path) as conn:
            conn.executescript(_SCHEMA)
            with conn:
                conn.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    [("name", name), ("root", str(root))],
                )
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except Exception:
        db_path.unlink(missing_ok=True)  # never leave a half-made database
        if folder_is_new:
            try:
                folder.rmdir()
            except OSError:
                pass
        raise

    info = _read_info(db_path)
    if info is None:
        raise sqlite3.DatabaseError(f"Library database is unreadable: {db_path}")
    return info, True


def list_libraries() -> list[LibraryInfo]:
    """All readable libraries, sorted by name.

    A library is any folder directly inside the data folder that contains a
    musichive.db file.
    """
    try:
        folders = [entry for entry in data_dir().iterdir() if entry.is_dir()]
    except OSError:
        return []

    libraries = []
    for folder in folders:
        db_path = folder / DB_NAME
        if db_path.is_file():
            info = _read_info(db_path)
            if info is not None:
                libraries.append(info)
    return sorted(libraries, key=lambda library: library.name.lower())


def remove_library(info: LibraryInfo) -> None:
    """Delete a library's database and its folder. Music files are never touched."""
    info.db_path.unlink(missing_ok=True)
    try:
        info.db_path.parent.rmdir()  # fails, harmlessly, if anything else is in it
    except OSError:
        pass


def _old_database_paths() -> list[Path]:
    """Databases left over from earlier versions' storage locations."""
    paths: list[Path] = []

    # First layout: ~/.local/share/musichive/libraries/<id>.db
    try:
        paths += sorted(old_libraries_dir().glob("*.db"))
    except OSError:
        pass

    # Second layout: ~/Music/<library name>/musichive.db
    try:
        for folder in sorted(music_dir().iterdir()):
            if folder.is_dir() and not folder.name.startswith("."):
                candidate = folder / DB_NAME
                if candidate.is_file():
                    paths.append(candidate)
    except OSError:
        pass

    return paths


def migrate_old_libraries() -> int:
    """Move databases from earlier locations into the data folder.

    Returns how many were moved. Anything that cannot be moved is left where
    it is, untouched.
    """
    known_roots = {library.root for library in list_libraries()}
    moved = 0
    for old_path in _old_database_paths():
        info = _read_info(old_path)
        if info is None or info.root in known_roots:
            continue
        folder = _free_folder(info.name)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_path), str(folder / DB_NAME))
        except OSError:
            continue
        known_roots.add(info.root)
        moved += 1

        # Remove the old folder if the move left it empty. This fails,
        # harmlessly, for a folder that still holds music.
        try:
            old_path.parent.rmdir()
        except OSError:
            pass
    return moved


# ----- tracks -----


def all_tag_names(db_path: Path) -> list[str]:
    """The name of every tag found in a library's files."""
    with connect(db_path) as conn:
        return [name for (name,) in conn.execute("SELECT DISTINCT name FROM track_tags")]


def load_tag_values(db_path: Path, names: list[str]) -> dict[str, dict[str, str]]:
    """The values of these tags: {relative path: {tag name: value}}."""
    if not names:
        return {}
    values: dict[str, dict[str, str]] = {}
    marks = ", ".join("?" * len(names))
    with connect(db_path) as conn:
        for rel_path, name, value in conn.execute(
            f"SELECT rel_path, name, value FROM track_tags WHERE name IN ({marks})", names
        ):
            values.setdefault(rel_path, {})[name] = value
    return values


def load_library(info: LibraryInfo, tag_names: list[str] | None = None) -> LoadedLibrary:
    """Read every saved track and cover thumbnail of a library, with these tags."""
    tag_values = load_tag_values(info.db_path, tag_names or [])
    paths: list[Path] = []
    infos: list[TrackInfo] = []
    with connect(info.db_path) as conn:
        rows = conn.execute(
            "SELECT t.rel_path, t.title, t.artist, t.album_artist, t.album, "
            "t.year, t.genre, t.track_number, t.disc_number, t.duration_ms, "
            "t.cover_key, COALESCE(p.count, 0), t.file_size, a.added_at, p.last_played, r.rating "
            "FROM tracks t LEFT JOIN plays p ON p.rel_path = t.rel_path "
            "LEFT JOIN track_added a ON a.rel_path = t.rel_path "
            "LEFT JOIN ratings r ON r.rel_path = t.rel_path "
            "ORDER BY t.position"
        )
        for (
            rel_path,
            title,
            artist,
            album_artist,
            album,
            year,
            genre,
            track,
            disc,
            duration,
            cover_key,
            plays,
            file_size,
            added_at,
            last_played,
            rating,
        ) in rows:
            paths.append(info.root / rel_path)
            infos.append(
                TrackInfo(
                    title=title,
                    artist=artist,
                    album_artist=album_artist,
                    album=album,
                    year=year,
                    genre=genre,
                    track_number=track,
                    disc_number=disc,
                    duration_ms=duration,
                    cover_key=cover_key,
                    play_count=plays,
                    file_size=file_size,
                    added_at=added_at,
                    last_played=last_played,
                    rating=rating or 0,
                    tags=tag_values.get(rel_path, {}),
                )
            )
        covers = {
            key: bytes(blob)
            for key, blob in conn.execute("SELECT cover_key, thumbnail FROM covers")
        }
    return LoadedLibrary(paths, infos, covers)


def library_stats(db_path: Path) -> LibraryStats:
    """The number of tracks in a library, their total size and total length."""
    with connect(db_path) as conn:
        count, size, duration = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(file_size), 0), "
            "COALESCE(SUM(duration_ms), 0) FROM tracks"
        ).fetchone()
    return LibraryStats(count, size, duration)

def increment_play_count(db_path: Path, rel_path: str, played_at: float) -> int:
    """Add one play to a track, note when, and return its new play count."""
    with connect(db_path) as conn:
        with conn:
            conn.execute(
                "INSERT INTO plays (rel_path, count, last_played) VALUES (?, 1, ?) "
                "ON CONFLICT(rel_path) DO UPDATE SET "
                "count = count + 1, last_played = excluded.last_played",
                (rel_path, played_at),
            )
            return conn.execute(
                "SELECT count FROM plays WHERE rel_path = ?", (rel_path,)
            ).fetchone()[0]


def set_rating(db_path: Path, rel_path: str, rating: int) -> None:
    """Save a track's star rating (0 removes it)."""
    with connect(db_path) as conn:
        with conn:
            if rating:
                conn.execute(
                    "INSERT OR REPLACE INTO ratings (rel_path, rating) VALUES (?, ?)",
                    (rel_path, rating),
                )
            else:
                conn.execute("DELETE FROM ratings WHERE rel_path = ?", (rel_path,))


def load_play_counts(db_path: Path) -> dict[str, int]:
    """Every stored play count of a library, by relative path."""
    with connect(db_path) as conn:
        return dict(conn.execute("SELECT rel_path, count FROM plays"))


def apply_lastfm_counts(db_path: Path, counts: dict[str, int]) -> int:
    """Replace play counts with Last.fm's. Returns how many tracks changed.

    `counts` maps track_key(artist, title) to Last.fm's play count. A track
    that Last.fm has no plays for keeps its local count.
    """
    with connect(db_path) as conn:
        rows = conn.execute("SELECT rel_path, artist, title FROM tracks").fetchall()
        changes = []
        for rel_path, artist, title in rows:
            count = counts.get(track_key(artist, title), 0)
            if count > 0:
                changes.append((rel_path, count))
        with conn:
            conn.executemany(
                "INSERT INTO plays (rel_path, count) VALUES (?, ?) "
                "ON CONFLICT(rel_path) DO UPDATE SET count = excluded.count",
                changes,
            )
    return len(changes)

def save_scan(
    db_path: Path, tracks: list[ScannedTrack], covers: dict[str, bytes]
) -> None:
    """Replace a library's contents with a new scan, all in one transaction.

    If anything fails, the previous contents stay exactly as they were.
    """
    rows = (
        (
            position,
            track.rel_path,
            track.info.title,
            track.info.artist,
            track.info.album_artist,
            track.info.album,
            track.info.year,
            track.info.genre,
            track.info.track_number,
            track.info.disc_number,
            track.info.duration_ms,
            track.file_size,
            track.modified,
            track.info.cover_key,
        )
        for position, track in enumerate(tracks)
    )
    tag_rows = (
        (track.rel_path, name, value)
        for track in tracks
        for name, value in track.info.tags.items()
    )
    with connect(db_path) as conn:
        with conn:  # commits on success, rolls back on error
            conn.execute("DELETE FROM tracks")
            conn.execute("DELETE FROM covers")
            conn.execute("DELETE FROM track_tags")
            conn.executemany(
                "INSERT INTO covers (cover_key, thumbnail) VALUES (?, ?)",
                covers.items(),
            )
            conn.executemany(
                "INSERT INTO tracks (position, rel_path, title, artist, "
                "album_artist, album, year, genre, track_number, disc_number, "
                "duration_ms, file_size, modified, cover_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.executemany(
                "INSERT INTO track_tags (rel_path, name, value) VALUES (?, ?, ?)",
                tag_rows,
            )
            # A rating in a file wins; a song without one keeps the rating it has here.
            conn.executemany(
                "INSERT OR REPLACE INTO ratings (rel_path, rating) VALUES (?, ?)",
                ((track.rel_path, track.info.rating) for track in tracks if track.info.rating),
            )
            # On a library's first scan nothing is "new", so file dates tell songs apart.
            first_scan = conn.execute("SELECT COUNT(*) FROM track_added").fetchone()[0] == 0
            now = datetime.now().timestamp()
            conn.executemany(
                "INSERT OR IGNORE INTO track_added (rel_path, added_at) VALUES (?, ?)",
                (
                    (track.rel_path, track.modified if first_scan and track.modified else now)
                    for track in tracks
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('scanned_at', ?)",
                (datetime.now().isoformat(timespec="seconds"),),
            )


def remove_track(db_path: Path, rel_path: str) -> None:
    """Drop one track from a library (after its file was deleted)."""
    with connect(db_path) as conn:
        with conn:
            conn.execute("DELETE FROM tracks WHERE rel_path = ?", (rel_path,))
            conn.execute("DELETE FROM plays WHERE rel_path = ?", (rel_path,))
            conn.execute("DELETE FROM ratings WHERE rel_path = ?", (rel_path,))
            conn.execute("DELETE FROM track_tags WHERE rel_path = ?", (rel_path,))
            conn.execute("DELETE FROM track_added WHERE rel_path = ?", (rel_path,))
            conn.execute(
                "DELETE FROM covers WHERE cover_key NOT IN "
                "(SELECT cover_key FROM tracks WHERE cover_key IS NOT NULL)"
            )


# ----- saved session -----


def save_session(db_path: Path, session: SavedSession) -> None:
    """Remember what was playing, in the library's own database."""
    values = [
        ("session_track", session.rel_path),
        ("session_position_ms", str(session.position_ms)),
        ("session_order", json.dumps(session.order, separators=(",", ":"))),
        ("session_order_position", str(session.order_position)),
        (
            "session_queued",
            json.dumps([1 if flag else 0 for flag in session.queued], separators=(",", ":")),
        ),
        ("session_stop_after", str(session.stop_after_index)),
        ("session_playlist", session.playlist),
    ]
    with connect(db_path) as conn:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", values
            )


def load_session(db_path: Path) -> SavedSession | None:
    """The saved session, or None if there is none or it is unreadable."""
    with connect(db_path) as conn:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
    try:
        order = [int(row) for row in json.loads(meta["session_order"])]
        queued_text = meta.get("session_queued")
        queued = (
            [bool(int(flag)) for flag in json.loads(queued_text)]
            if queued_text
            else [False] * len(order)
        )
        return SavedSession(
            rel_path=meta["session_track"],
            position_ms=max(0, int(meta["session_position_ms"])),
            order=order,
            order_position=int(meta["session_order_position"]),
            queued=queued,
            stop_after_index=int(meta.get("session_stop_after", -1)),
            playlist=meta.get("session_playlist", ""),
        )
    except (KeyError, ValueError, TypeError):
        return None


def clear_session(db_path: Path) -> None:
    with connect(db_path) as conn:
        with conn:
            conn.execute(
                "DELETE FROM meta WHERE key IN ('session_track', "
                "'session_position_ms', 'session_order', 'session_order_position', "
                "'session_queued', 'session_stop_after', 'session_playlist')"
            )


# ----- internal -----


def _read_info(db_path: Path) -> LibraryInfo | None:
    try:
        with connect(db_path) as conn:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
            count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    except sqlite3.Error:
        return None
    if "root" not in meta:
        return None
    root = Path(meta["root"])
    return LibraryInfo(
        id=_id_for(meta["root"]),
        name=meta.get("name") or root.name or str(root),
        root=root,
        db_path=db_path,
        scanned_at=meta.get("scanned_at"),
        track_count=count,
    )