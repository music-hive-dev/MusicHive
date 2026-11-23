import pytest

from src.scrobble_backlog import (
    DAILY_SCROBBLE_BUDGET,
    RE_DATE_START_SECONDS,
    SECONDS_PER_DAY,
    ImportResult,
    ScrobbleBacklog,
    parse_scrobbler_log,
)

HEADER = "#AUDIOSCROBBLER/1.1\n#TZ/UTC\n#CLIENT/Rockbox\n"
NOW = 1_800_000_000


def write_log(folder, lines, name=".scrobbler.log"):
    path = folder / name
    path.write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_parse_counts_plays_skips_and_bad_lines(tmp_path):
    log = write_log(
        tmp_path,
        [
            "Artist\tAlbum\tTitle\t1\t200\tL\t1700000000",
            "Artist\t\tSkipped\t2\t100\tS\t1700000100",
            "bad line",
            "Artist\t\tNo album\t3\t\tL\t1700000200",
        ],
    )
    plays, skipped, unreadable = parse_scrobbler_log(log)
    assert (skipped, unreadable) == (1, 1)
    assert [entry for _identity, entry in plays] == [
        {
            "artist": "Artist",
            "title": "Title",
            "timestamp": 1_700_000_000,
            "album": "Album",
            "duration": 200,
        },
        {"artist": "Artist", "title": "No album", "timestamp": 1_700_000_200},
    ]


def test_identities_are_stable_and_tell_repeats_apart(tmp_path):
    line = "Artist\tAlbum\tTitle\t1\t200\tL\t1700000000"
    log = write_log(tmp_path, [line, line])
    first = [identity for identity, _entry in parse_scrobbler_log(log)[0]]
    second = [identity for identity, _entry in parse_scrobbler_log(log)[0]]
    assert first == second
    assert len(set(first)) == 2


def test_unknown_time_is_zero(tmp_path):
    log = write_log(tmp_path, ["Artist\t\tTitle\t1\t200\tL\t0"])
    assert parse_scrobbler_log(log)[0][0][1]["timestamp"] == 0


@pytest.fixture
def backlog(tmp_path):
    return ScrobbleBacklog(tmp_path / "data" / "backlog.json")


def test_importing_twice_adds_nothing_new(tmp_path, backlog):
    log = write_log(tmp_path, ["A\t\tT\t1\t200\tL\t1700000000"])
    assert backlog.add(log) == ImportResult(1, 0, 0, 0)
    assert backlog.add(log) == ImportResult(0, 1, 0, 0)
    assert backlog.count == 1


def test_backlog_is_saved(tmp_path, backlog):
    backlog.add(write_log(tmp_path, ["A\t\tT\t1\t200\tL\t1700000000"]))
    again = ScrobbleBacklog(tmp_path / "data" / "backlog.json")
    assert again.count == 1
    assert again.add(write_log(tmp_path, ["A\t\tT\t1\t200\tL\t1700000000"])).added == 0


def test_clear(tmp_path, backlog):
    log = write_log(tmp_path, ["A\t\tT\t1\t200\tL\t1700000000"])
    backlog.add(log)
    backlog.clear()
    assert backlog.count == 0
    assert backlog.add(log).added == 1  # forgotten, so it can be imported again


def test_daily_room(backlog):
    assert backlog.room(NOW, per_day=100) == 100
    backlog.note_sent(30, NOW, from_backlog=True)
    assert backlog.room(NOW, per_day=100) == 70
    backlog.note_sent(DAILY_SCROBBLE_BUDGET - 50, NOW, from_backlog=False)
    assert backlog.room(NOW, per_day=100) == 20
    assert backlog.room(NOW + SECONDS_PER_DAY, per_day=100) == 100


def test_mark_full(backlog):
    backlog.mark_full(NOW)
    assert backlog.room(NOW + 10, per_day=100) == 0
    assert backlog.room(NOW + SECONDS_PER_DAY, per_day=100) == 100


def test_prepare_re_dates_old_plays_and_finish_removes_sent_ones(tmp_path, backlog):
    recent = NOW - 3600
    backlog.add(
        write_log(
            tmp_path,
            [
                "A\tAl\tNew\t3\t100\tL\t%d" % recent,
                "A\tAl\tOld1\t1\t200\tL\t1000000000",
                "A\t\tOld2\t2\t\tL\t1000000100",
            ],
        )
    )
    batch, cursor = backlog.prepare(3, NOW, per_day=100)
    start = NOW - RE_DATE_START_SECONDS
    assert [entry["title"] for entry in batch] == ["Old1", "Old2", "New"]
    assert [entry["timestamp"] for entry in batch] == [start, start + 200, recent]
    assert cursor == start + 200 + 210

    backlog.finish(keep=[1], cursor=cursor)
    assert backlog.count == 1
    batch, _cursor = backlog.prepare(1, NOW, per_day=100)
    assert batch[0]["title"] == "Old2"
    assert batch[0]["timestamp"] == cursor  # re-dated plays never go back in time
