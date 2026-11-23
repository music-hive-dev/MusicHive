import sqlite3

import pytest

from src import library_db
from src.library_db import (
    apply_lastfm_counts,
    increment_play_count,
    library_stats,
    load_play_counts,
    load_track_settings,
    save_track_settings,
    set_rating,
    track_key,
)


def test_track_key_ignores_case_accents_spaces_and_apostrophes():
    assert track_key("Björk", "It’s  Oh So Quiet ") == track_key("bjork", "it's oh so quiet")
    assert track_key("A", "B") != track_key("A", "C")
    assert track_key("AB", "C") != track_key("A", "BC")


@pytest.fixture
def db(tmp_path):
    """An empty library database, as add_library makes it."""
    path = tmp_path / library_db.DB_NAME
    conn = sqlite3.connect(path)
    conn.executescript(library_db._SCHEMA)
    conn.execute(f"PRAGMA user_version = {library_db.SCHEMA_VERSION}")
    conn.commit()
    conn.close()
    return path


def add_track(db, position, rel_path, artist, title, size=None, duration=None):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tracks (position, rel_path, title, artist, album, year, "
            "file_size, duration_ms) VALUES (?, ?, ?, ?, '', '', ?, ?)",
            (position, rel_path, title, artist, size, duration),
        )
    conn.close()


def test_play_counts(db):
    assert increment_play_count(db, "a.mp3", 100.0) == 1
    assert increment_play_count(db, "a.mp3", 200.0) == 2
    assert increment_play_count(db, "b.mp3", 300.0) == 1
    assert load_play_counts(db) == {"a.mp3": 2, "b.mp3": 1}


def test_ratings(db):
    def rating_of(rel_path):
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT rating FROM ratings WHERE rel_path = ?", (rel_path,)
            ).fetchone()
        conn.close()
        return row[0] if row else 0

    set_rating(db, "a.mp3", 4)
    assert rating_of("a.mp3") == 4
    set_rating(db, "a.mp3", 0)
    assert rating_of("a.mp3") == 0


def test_track_settings(db):
    save_track_settings(db, {"a.mp3": ("Bass", "", True), "b.mp3": ("", "Bars", False)})
    assert load_track_settings(db, ["a.mp3", "b.mp3", "c.mp3"]) == {
        "a.mp3": ("Bass", "", True),
        "b.mp3": ("", "Bars", False),
    }
    save_track_settings(db, {"a.mp3": ("", "", False)})
    assert load_track_settings(db, ["a.mp3"]) == {}


def test_apply_lastfm_counts(db):
    add_track(db, 0, "a.mp3", "Björk", "It’s Oh So Quiet")
    add_track(db, 1, "b.mp3", "Other", "Song")
    increment_play_count(db, "b.mp3", 1.0)
    changed = apply_lastfm_counts(db, {track_key("bjork", "it's oh so quiet"): 12})
    assert changed == 1
    assert load_play_counts(db) == {"a.mp3": 12, "b.mp3": 1}


def test_library_stats(db):
    assert library_stats(db) == library_db.LibraryStats(0, 0, 0)
    add_track(db, 0, "a.mp3", "A", "T", size=1000, duration=60_000)
    add_track(db, 1, "b.mp3", "A", "U", size=500)
    assert library_stats(db) == library_db.LibraryStats(2, 1500, 60_000)
