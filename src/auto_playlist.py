"""The rules of an auto-playlist, such as "Genre contains Rock".

A rule is a dictionary: {"tag": "genre", "comparison": "contains", "value": "Rock"}.
Tag names are the ones track_metadata.read_all_tags gives. Case does not matter.
"""

import random
import re
from collections.abc import Callable

# The comparison each entry of ruleComparisonCombo stands for, in the order
# auto_playlist_rule.ui lists them.
COMPARISONS = (
    "contains",
    "does not contain",
    "is",
    "is not",
    "starts with",
    "ends with",
    "is greater than",
    "is less than",
)


TIME_TAG = "time"  # a song's length, in seconds


def _in_seconds(text: str) -> str:
    """A time typed as "1:30", "1:02:30" or "90", as a number of seconds."""
    try:
        seconds = 0.0
        for part in text.split(":"):
            seconds = seconds * 60 + float(part)
    except ValueError:
        return text
    return f"{seconds:g}"


def _number(text: str) -> float | None:
    """The number `text` starts with, so that "1999-05-01" counts as 1999."""
    match = re.match(r"\s*-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def rule_matches(rule: dict, tag_text: str) -> bool:
    """Whether a song whose tag holds `tag_text` ("" if it has none) passes `rule`."""
    comparison = rule["comparison"]
    value = _in_seconds(rule["value"]) if rule["tag"] == TIME_TAG else rule["value"]
    text, wanted = tag_text.casefold(), value.casefold()
    # A tag can hold several values ("Rock; Pop"): "is Rock" is true for that song.
    values = text.split("; ")
    if comparison == "contains":
        return wanted in text
    if comparison == "does not contain":
        return wanted not in text
    if comparison == "is":
        return wanted in values
    if comparison == "is not":
        return wanted not in values
    if comparison == "starts with":
        return text.startswith(wanted)
    if comparison == "ends with":
        return text.endswith(wanted)
    first, second = _number(tag_text), _number(value)
    if first is None or second is None:
        return False
    return first > second if comparison == "is greater than" else first < second


def song_matches(rules: list[dict], tags: dict[str, str], match_any: bool = False) -> bool:
    """Whether a song passes every rule, or any rule. With no rules, no song does."""
    results = (rule_matches(rule, tags.get(rule["tag"], "")) for rule in rules)
    return bool(rules) and (any(results) if match_any else all(results))


# ----- limiting a playlist -----

# What each entry of the limit combo boxes stands for, in the order auto_playlist_edit.ui
# lists them. A limit is {"amount": 25, "unit": "songs", "order": "newest"}.
LIMIT_UNITS = ("minutes", "hours", "GB", "MB", "songs", "albums")
LIMIT_ORDERS = (
    "random",
    "most_played",
    "least_played",
    "newest",
    "oldest",
    "recently_played",
    "longest_unplayed",
)

_MS_PER_UNIT = {"minutes": 60_000, "hours": 3_600_000}
_BYTES_PER_UNIT = {"MB": 1000**2, "GB": 1000**3}  # decimal, like the status bar


def _in_limit_order(rows: list[int], info_of: Callable, order: str) -> list[int]:
    """The rows, best candidates first. Equal songs stay in library order."""
    if order == "random":
        return random.sample(rows, len(rows))
    keys = {
        "most_played": lambda row: -info_of(row).play_count,
        "least_played": lambda row: info_of(row).play_count,
        "newest": lambda row: -(info_of(row).added_at or 0),
        "oldest": lambda row: info_of(row).added_at or 0,
        # A song that was never played counts as played longest ago.
        "recently_played": lambda row: -(info_of(row).last_played or 0),
        "longest_unplayed": lambda row: info_of(row).last_played or 0,
    }
    return sorted(rows, key=keys[order])


def apply_limit(rows: list[int], info_of: Callable, album_of: Callable, limit: dict) -> list[int]:
    """The rows a limit keeps, in the order they were chosen.

    `info_of(row)` gives a song's TrackInfo, `album_of(row)` its album's key ("" if none).
    Time and size limits stop at the first song that no longer fits.
    """
    ordered = _in_limit_order(rows, info_of, limit["order"])
    unit, amount = limit["unit"], limit["amount"]
    if unit == "songs":
        return ordered[:amount]
    if unit == "albums":
        albums: dict[str, list[int]] = {}
        for row in ordered:  # a song without an album counts as one of its own
            albums.setdefault(album_of(row) or f"row {row}", []).append(row)
        chosen = list(albums.values())[:amount]
        return [row for album in chosen for row in sorted(album)]

    if unit in _MS_PER_UNIT:
        budget = amount * _MS_PER_UNIT[unit]
        weight = lambda row: info_of(row).duration_ms or 0
    else:
        budget = amount * _BYTES_PER_UNIT[unit]
        weight = lambda row: info_of(row).file_size or 0
    kept = []
    used = 0
    for row in ordered:
        used += weight(row)
        if used > budget:
            break
        kept.append(row)
    return kept
