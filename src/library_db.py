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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.track_metadata import TrackInfo

SCHEMA_VERSION = 1
DB_NAME = "musichive.db"
DATA_DIR_NAME = ".musichive"

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
    album        TEXT NOT NULL,
    year         TEXT NOT NULL,
    track_number INTEGER,
    disc_number  INTEGER,
    duration_ms  INTEGER,
    file_size    INTEGER,
    modified     REAL,
    cover_key    TEXT REFERENCES covers(cover_key)
);
"""


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


@dataclass
class SavedSession:
    rel_path: str  # the playing track, relative to the library root
    position_ms: int  # how far into the track
    order: list[int]  # the whole play order, as library rows
    order_position: int  # where the playing track is within `order`


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
        yield conn
    finally:
        conn.close()


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


def load_library(info: LibraryInfo) -> LoadedLibrary:
    """Read every saved track and cover thumbnail of a library."""
    paths: list[Path] = []
    infos: list[TrackInfo] = []
    with connect(info.db_path) as conn:
        rows = conn.execute(
            "SELECT rel_path, title, artist, album, year, track_number, "
            "disc_number, duration_ms, cover_key FROM tracks ORDER BY position"
        )
        for rel_path, title, artist, album, year, track, disc, duration, cover_key in rows:
            paths.append(info.root / rel_path)
            infos.append(
                TrackInfo(
                    title=title,
                    artist=artist,
                    album=album,
                    year=year,
                    track_number=track,
                    disc_number=disc,
                    duration_ms=duration,
                    cover_key=cover_key,
                )
            )
        covers = {
            key: bytes(blob)
            for key, blob in conn.execute("SELECT cover_key, thumbnail FROM covers")
        }
    return LoadedLibrary(paths, infos, covers)


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
            track.info.album,
            track.info.year,
            track.info.track_number,
            track.info.disc_number,
            track.info.duration_ms,
            track.file_size,
            track.modified,
            track.info.cover_key,
        )
        for position, track in enumerate(tracks)
    )
    with connect(db_path) as conn:
        with conn:  # commits on success, rolls back on error
            conn.execute("DELETE FROM tracks")
            conn.execute("DELETE FROM covers")
            conn.executemany(
                "INSERT INTO covers (cover_key, thumbnail) VALUES (?, ?)",
                covers.items(),
            )
            conn.executemany(
                "INSERT INTO tracks (position, rel_path, title, artist, album, "
                "year, track_number, disc_number, duration_ms, file_size, "
                "modified, cover_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('scanned_at', ?)",
                (datetime.now().isoformat(timespec="seconds"),),
            )


# ----- saved session -----


def save_session(db_path: Path, session: SavedSession) -> None:
    """Remember what was playing, in the library's own database."""
    values = [
        ("session_track", session.rel_path),
        ("session_position_ms", str(session.position_ms)),
        ("session_order", json.dumps(session.order, separators=(",", ":"))),
        ("session_order_position", str(session.order_position)),
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
        return SavedSession(
            rel_path=meta["session_track"],
            position_ms=max(0, int(meta["session_position_ms"])),
            order=[int(row) for row in json.loads(meta["session_order"])],
            order_position=int(meta["session_order_position"]),
        )
    except (KeyError, ValueError, TypeError):
        return None


def clear_session(db_path: Path) -> None:
    with connect(db_path) as conn:
        with conn:
            conn.execute(
                "DELETE FROM meta WHERE key IN ('session_track', "
                "'session_position_ms', 'session_order', 'session_order_position')"
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