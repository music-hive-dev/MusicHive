"""Per-library SQLite databases: one database file per music library."""

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.track_metadata import TrackInfo

SCHEMA_VERSION = 1

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
    root: Path
    db_path: Path
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


def libraries_dir() -> Path:
    """Folder holding one database file per library."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "musichive" / "libraries"


def library_id_for(root: Path) -> str:
    """Stable id for a library folder (also the database's file name)."""
    return hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()[:16]


@contextmanager
def connect(db_path: Path):
    """Open a short-lived connection. Each thread opens its own."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        yield conn
    finally:
        conn.close()


def add_library(root: Path) -> tuple[LibraryInfo, bool]:
    """Create the database for `root` if needed.

    Returns (library, created). `created` is False if it already existed.
    Raises OSError or sqlite3.Error on failure.
    """
    root = root.resolve()
    db_path = libraries_dir() / f"{library_id_for(root)}.db"
    created = not db_path.exists()

    if created:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with connect(db_path) as conn:
                conn.executescript(_SCHEMA)
                with conn:
                    conn.executemany(
                        "INSERT INTO meta (key, value) VALUES (?, ?)",
                        [("name", root.name or str(root)), ("root", str(root))],
                    )
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except Exception:
            db_path.unlink(missing_ok=True)  # never leave a half-made database
            raise

    info = _read_info(db_path)
    if info is None:
        raise sqlite3.DatabaseError(f"Library database is unreadable: {db_path}")
    return info, created


def list_libraries() -> list[LibraryInfo]:
    """All readable libraries, sorted by name."""
    directory = libraries_dir()
    if not directory.is_dir():
        return []
    libraries = []
    for path in directory.glob("*.db"):
        info = _read_info(path)
        if info is not None:
            libraries.append(info)
    return sorted(libraries, key=lambda library: library.name.lower())


def remove_library(library_id: str) -> None:
    """Delete a library's database. The music files are never touched."""
    (libraries_dir() / f"{library_id}.db").unlink(missing_ok=True)


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
        id=db_path.stem,
        name=meta.get("name") or root.name or str(root),
        root=root,
        db_path=db_path,
        scanned_at=meta.get("scanned_at"),
        track_count=count,
    )