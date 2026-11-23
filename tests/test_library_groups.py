from pathlib import Path

import pytest

from src.library_groups import (
    GROUPERS,
    VARIOUS_ARTISTS,
    group_by_album,
    group_key,
    songs_text,
)
from src.track_metadata import TrackInfo


@pytest.fixture
def library():
    infos = [
        TrackInfo("S1", artist="Beta", album="Alb", track_number=2),
        TrackInfo("S2", artist="alpha", album="Alb", track_number=1),
        TrackInfo("S3", artist="Alpha", album="Other", year="1999", genre="Rock; Pop"),
        TrackInfo("S4", artist="Gamma", genre="rock, Jazz"),
    ]
    folders = [Path("/m/a"), Path("/m/a"), Path("/m/b"), Path("/m/c")]
    return infos, folders


def test_songs_text():
    assert songs_text(1) == "1 song"
    assert songs_text(0) == "0 songs"
    assert songs_text(2) == "2 songs"


def test_group_by_artist_ignores_case(library):
    groups = GROUPERS["artist"](*library)
    assert [group.name for group in groups] == ["alpha", "Beta", "Gamma"]
    assert groups[0].key == "alpha"
    assert groups[0].rows == [1, 2]
    assert groups[0].subtitle == "2 songs"


def test_group_by_album(library):
    groups = group_by_album(*library)
    assert [group.name for group in groups] == ["Alb", "Other"]
    alb, other = groups
    assert alb.rows == [1, 0]  # by track number
    assert alb.artist == VARIOUS_ARTISTS
    assert alb.subtitle == VARIOUS_ARTISTS
    assert other.artist == "Alpha"
    assert other.year == "1999"
    assert other.subtitle == "Alpha · 1999"


def test_albums_with_the_same_name_in_different_folders_stay_apart():
    infos = [TrackInfo("a", album="Greatest Hits"), TrackInfo("b", album="Greatest Hits")]
    groups = group_by_album(infos, [Path("/m/x"), Path("/m/y")])
    assert len(groups) == 2


def test_album_artist_keeps_an_album_whole_across_folders():
    infos = [
        TrackInfo("a", album="Live", album_artist="Band", artist="Band"),
        TrackInfo("b", album="Live", album_artist="Band", artist="Guest"),
    ]
    groups = group_by_album(infos, [Path("/m/cd1"), Path("/m/cd2")])
    assert len(groups) == 1
    assert groups[0].artist == "Band"


def test_group_by_genre_splits_several_genres(library):
    groups = GROUPERS["genre"](*library)
    assert [group.name for group in groups] == ["Jazz", "Pop", "Rock"]
    assert groups[2].rows == [2, 3]


def test_group_by_decade(library):
    groups = GROUPERS["decade"](*library)
    assert [(group.name, group.rows) for group in groups] == [("1990s", [2])]


def test_group_key(library):
    infos, folders = library
    assert group_key("genre", infos[3], folders[3]) == "rock"
    assert group_key("album", infos[3], folders[3]) == ""
    assert group_key("album", infos[0], folders[0]) == "alb\n/m/a"
    assert group_key("decade", infos[0], folders[0]) == ""
    assert group_key("artist", infos[0], folders[0]) == "beta"
