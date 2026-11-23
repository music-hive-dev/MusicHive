from types import SimpleNamespace

import pytest

from src.auto_playlist import apply_limit, rule_matches, song_matches


def rule(comparison, value, tag="genre"):
    return {"tag": tag, "comparison": comparison, "value": value}


@pytest.mark.parametrize(
    "comparison, value, tag_text, expected",
    [
        ("contains", "rock", "Hard Rock", True),
        ("contains", "jazz", "Hard Rock", False),
        ("does not contain", "jazz", "Hard Rock", True),
        ("is", "pop", "Rock; Pop", True),
        ("is", "Ro", "Rock; Pop", False),
        ("is not", "pop", "Rock; Pop", False),
        ("is not", "jazz", "Rock; Pop", True),
        ("starts with", "hard", "Hard Rock", True),
        ("ends with", "ROCK", "Hard Rock", True),
        ("ends with", "hard", "Hard Rock", False),
    ],
)
def test_text_comparisons(comparison, value, tag_text, expected):
    assert rule_matches(rule(comparison, value), tag_text) is expected


def test_number_comparisons_use_the_leading_number():
    assert rule_matches(rule("is greater than", "1998", "year"), "1999-05-01")
    assert not rule_matches(rule("is less than", "1998", "year"), "1999-05-01")


def test_number_comparison_without_numbers_is_false():
    assert not rule_matches(rule("is greater than", "1998", "year"), "")
    assert not rule_matches(rule("is greater than", "abc", "year"), "2000")


def test_time_values_can_be_typed_as_minutes_and_seconds():
    assert rule_matches(rule("is greater than", "1:30", "time"), "100")
    assert not rule_matches(rule("is greater than", "1:30", "time"), "80")
    assert rule_matches(rule("is less than", "1:02:30", "time"), "3000")


def test_song_matches_all_or_any():
    rules = [rule("contains", "rock"), rule("is", "2000", "year")]
    tags = {"genre": "Rock", "year": "1999"}
    assert not song_matches(rules, tags)
    assert song_matches(rules, tags, match_any=True)


def test_song_matches_without_rules():
    assert song_matches([], {})


def test_missing_tag_counts_as_empty():
    assert song_matches([rule("does not contain", "rock")], {})
    assert not song_matches([rule("contains", "rock")], {})


# ----- limits -----


def make_infos(**columns):
    """SimpleNamespace TrackInfo stand-ins, one per row, from per-column lists."""
    count = len(next(iter(columns.values())))
    defaults = dict(play_count=0, added_at=None, last_played=None, duration_ms=None, file_size=None)
    infos = []
    for row in range(count):
        values = dict(defaults)
        values.update({name: column[row] for name, column in columns.items()})
        infos.append(SimpleNamespace(**values))
    return infos.__getitem__


def no_album(_row):
    return ""


def test_limit_songs_most_played_keeps_library_order_for_ties():
    info_of = make_infos(play_count=[5, 9, 5, 1])
    limit = {"amount": 3, "unit": "songs", "order": "most_played"}
    assert apply_limit([0, 1, 2, 3], info_of, no_album, limit) == [1, 0, 2]


def test_limit_recently_played_puts_never_played_last():
    info_of = make_infos(last_played=[None, 100, 50])
    limit = {"amount": 3, "unit": "songs", "order": "recently_played"}
    assert apply_limit([0, 1, 2], info_of, no_album, limit) == [1, 2, 0]


def test_limit_albums_keeps_whole_albums():
    info_of = make_infos(added_at=[0, 1, 2, 3])
    albums = {0: "A", 1: "B", 2: "A", 3: ""}
    limit = {"amount": 2, "unit": "albums", "order": "oldest"}
    assert apply_limit([0, 1, 2, 3], info_of, albums.get, limit) == [0, 2, 1]


def test_limit_minutes_stops_at_the_first_song_that_does_not_fit():
    info_of = make_infos(added_at=[0, 1, 2], duration_ms=[60_000, 120_000, 30_000])
    limit = {"amount": 2, "unit": "minutes", "order": "oldest"}
    assert apply_limit([0, 1, 2], info_of, no_album, limit) == [0]


def test_limit_megabytes():
    info_of = make_infos(added_at=[0, 1, 2], file_size=[400_000] * 3)
    limit = {"amount": 1, "unit": "MB", "order": "oldest"}
    assert apply_limit([0, 1, 2], info_of, no_album, limit) == [0, 1]


def test_limit_random_picks_from_the_rows():
    info_of = make_infos(play_count=[0] * 10)
    limit = {"amount": 4, "unit": "songs", "order": "random"}
    chosen = apply_limit(list(range(10)), info_of, no_album, limit)
    assert len(chosen) == 4
    assert len(set(chosen)) == 4
    assert set(chosen) <= set(range(10))
