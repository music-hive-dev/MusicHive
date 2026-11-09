"""Plays from a portable player's log, waiting to be sent to Last.fm a day's worth at a time.

Rockbox writes every play to a .scrobbler.log file. Last.fm caps how many
scrobbles it takes per day and refuses plays dated more than about two weeks
ago, so a big log is sent slowly: a play still inside that window keeps its real
time, and an older one is given a recent time, in the original order.
"""

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

# Last.fm's cap is about 2800 a day; the rest is left for what is played here.
DAILY_SCROBBLE_BUDGET = 2700
DEFAULT_BACKLOG_PER_DAY = 100  # how many imported plays go out a day, unless changed
SECONDS_PER_DAY = 86400
KEEP_REAL_DATE_SECONDS = 13 * SECONDS_PER_DAY  # a safe margin inside the two weeks
RE_DATE_START_SECONDS = 12 * SECONDS_PER_DAY  # how far back re-dated plays are placed
# Re-dated plays are placed one song's length apart, as if listened to in a row.
MIN_PLAY_SECONDS = 30
UNKNOWN_PLAY_SECONDS = 210  # for a play whose length is not in the log
# A day's plays may not cover more than this part of a day, so that re-dated
# plays stay behind the present however long the backlog is.
MOST_OF_A_DAY = 0.9


@dataclass(frozen=True)
class ImportResult:
    added: int
    already_imported: int
    skipped_on_player: int  # marked in the log as skipped, so not really listened to
    unreadable: int


def _as_real_time(stamp: int, is_utc: bool) -> int:
    """A log's timestamp as real time. 0 means the player did not know the time.

    Unless the log says it is in UTC, Rockbox has written the clock on the
    player as it stood, in local time.
    """
    if stamp <= 0:
        return 0
    if is_utc:
        return stamp
    try:
        return int(time.mktime((*time.gmtime(stamp)[:8], -1)))
    except (OverflowError, ValueError, OSError):
        return 0


def parse_scrobbler_log(path: Path) -> tuple[list[tuple[str, dict]], int, int]:
    """Read a .scrobbler.log. Raises OSError if the file cannot be read.

    Returns ([(identity, entry)], plays marked as skipped, unreadable lines).
    Two imports of one log give each play the same identity.
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    plays: list[tuple[str, dict]] = []
    skipped = unreadable = 0
    is_utc = False
    occurrences: dict[str, int] = {}
    for line in text.splitlines():
        if line.startswith("#TZ/"):
            is_utc = line[4:].strip().upper() == "UTC"
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 7 or not fields[0].strip() or not fields[2].strip():
            unreadable += 1
            continue
        artist, album, title, _track, length, rating, stamp = fields[:7]
        if rating.strip().upper() != "L":
            skipped += 1
            continue
        entry = {
            "artist": artist.strip(),
            "title": title.strip(),
            "timestamp": _as_real_time(int(stamp) if stamp.strip().isdigit() else 0, is_utc),
        }
        if album.strip():
            entry["album"] = album.strip()
        if length.strip().isdigit():
            entry["duration"] = int(length)
        occurrences[line] = occurrences.get(line, 0) + 1
        identity = hashlib.sha1(f"{line}\n{occurrences[line]}".encode()).hexdigest()[:16]
        plays.append((identity, entry))
    return plays, skipped, unreadable


class ScrobbleBacklog:
    """The waiting plays, and how many scrobbles have gone out lately.

    The count covers 24 hours from the first scrobble of a period rather than
    a calendar day, so two days' worth can never land within 24 hours.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: list[dict] = []
        self._seen: set[str] = set()  # every play ever imported, so none is sent twice
        self._period_start = 0.0
        self._sent_in_period = 0  # every scrobble of this period
        self._backlog_sent_in_period = 0  # those that came from an imported log
        self._re_date_cursor = 0
        self._in_flight: list[dict] = []  # the plays of the batch being sent
        self._load()

    @property
    def count(self) -> int:
        return len(self._entries)

    def add(self, path: Path) -> ImportResult:
        """Import a log. Raises OSError if the file cannot be read."""
        plays, skipped, unreadable = parse_scrobbler_log(path)
        new = [(identity, entry) for identity, entry in plays if identity not in self._seen]
        self._seen.update(identity for identity, _entry in new)
        self._entries.extend(entry for _identity, entry in new)
        self._entries.sort(key=lambda entry: entry["timestamp"])  # oldest go first
        self._save()
        return ImportResult(len(new), len(plays) - len(new), skipped, unreadable)

    def clear(self) -> None:
        self._entries.clear()
        self._seen.clear()
        self._save()

    # ----- the daily allowance -----

    def _start_new_period_if_due(self, now: float) -> None:
        if self._period_start and now >= self._period_start + SECONDS_PER_DAY:
            self._period_start = 0.0
            self._sent_in_period = 0
            self._backlog_sent_in_period = 0

    def room(self, now: float, per_day: int) -> int:
        """How many more imported plays may go out in the current 24 hours."""
        self._start_new_period_if_due(now)
        return max(
            0,
            min(
                DAILY_SCROBBLE_BUDGET - self._sent_in_period,
                per_day - self._backlog_sent_in_period,
            ),
        )

    def note_sent(self, count: int, now: float, from_backlog: bool) -> None:
        """Last.fm accepted this many scrobbles, from the backlog or from playing here."""
        self._start_new_period_if_due(now)
        if count > 0:
            self._period_start = self._period_start or now
            self._sent_in_period += count
            if from_backlog:
                self._backlog_sent_in_period += count
            self._save()

    def mark_full(self, now: float) -> None:
        """Last.fm says today's cap is reached: send nothing more this period."""
        self._period_start = self._period_start or now
        self._sent_in_period = DAILY_SCROBBLE_BUDGET
        self._save()

    # ----- sending -----

    def prepare(self, count: int, now: float, per_day: int) -> tuple[list[dict], int]:
        """The next plays to send, with their dates settled, and the new re-date position."""
        oldest_real_date = now - KEEP_REAL_DATE_SECONDS
        cursor = max(self._re_date_cursor, int(now - RE_DATE_START_SECONDS))
        longest_gap = int(SECONDS_PER_DAY * MOST_OF_A_DAY / per_day)
        self._in_flight = self._entries[:count]
        batch = []
        for entry in self._in_flight:
            entry = dict(entry)
            if not oldest_real_date <= entry["timestamp"] <= now:
                entry["timestamp"] = cursor
                length = entry.get("duration") or UNKNOWN_PLAY_SECONDS
                cursor += min(max(length, MIN_PLAY_SECONDS), longest_gap)
            batch.append(entry)
        return batch, cursor

    def finish(self, keep: list[int], cursor: int) -> None:
        """A prepared batch was answered. The plays at positions `keep` must wait.

        Plays are removed by which ones they are, not by where they are in the
        list, because importing another log meanwhile may have reordered it.
        """
        waiting = {id(self._in_flight[position]) for position in keep}
        sent = {id(entry) for entry in self._in_flight} - waiting
        self._entries = [entry for entry in self._entries if id(entry) not in sent]
        self._in_flight = []
        self._re_date_cursor = cursor
        self._save()

    # ----- saving -----

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._entries = data.get("entries", [])
            self._seen = set(data.get("seen", []))
            self._period_start = float(data.get("period_start", 0.0))
            self._sent_in_period = int(data.get("sent_in_period", 0))
            self._backlog_sent_in_period = int(data.get("backlog_sent_in_period", 0))
            self._re_date_cursor = int(data.get("re_date_cursor", 0))

    def _save(self) -> None:
        data = {
            "entries": self._entries,
            "seen": sorted(self._seen),
            "period_start": self._period_start,
            "sent_in_period": self._sent_in_period,
            "backlog_sent_in_period": self._backlog_sent_in_period,
            "re_date_cursor": self._re_date_cursor,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(".tmp")
            temp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(temp, self._path)
        except OSError:
            pass
