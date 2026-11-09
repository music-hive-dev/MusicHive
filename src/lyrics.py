"""Reading lyrics, timed (LRC format) or plain.

A timed line looks like "[00:06.71]Gravity". There can be several times in front
of one line, words can have their own <00:07.10> times, and tags such as
"[ar:Artist]" describe the song rather than being lyrics.
"""

import re
from dataclasses import dataclass, field

_LINE_TIME = re.compile(r"\[(\d+):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_WORD_TIME = re.compile(r"<\d+:\d{1,2}(?:[.:]\d{1,3})?>")
_INFO_TAG = re.compile(r"^\[[a-zA-Z]+:.*\]$")
_OFFSET_TAG = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]$", re.IGNORECASE)


@dataclass
class Lyrics:
    texts: list[str] = field(default_factory=list)
    times_ms: list[int] = field(default_factory=list)  # one per line; empty if not timed


def _milliseconds(minutes: str, seconds: str) -> int:
    return int(minutes) * 60_000 + round(float(seconds.replace(":", ".")) * 1000)


def parse_lyrics(text: str) -> Lyrics:
    """Understand lyrics text. Timed lines come back in time order."""
    timed: list[tuple[int, str]] = []
    plain: list[str] = []
    offset = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        offset_tag = _OFFSET_TAG.match(line)
        if offset_tag:
            offset = int(offset_tag.group(1))
            continue
        times = []
        position = 0
        while match := _LINE_TIME.match(line, position):
            times.append(_milliseconds(match.group(1), match.group(2)))
            position = match.end()
        if times:
            words = _WORD_TIME.sub("", line[position:]).strip()
            timed.extend((time_ms, words) for time_ms in times)
        elif not _INFO_TAG.match(line):
            plain.append(line)
    if not timed:
        while plain and not plain[0]:
            del plain[0]
        return Lyrics(plain)
    timed.sort(key=lambda entry: entry[0])
    # A positive offset means the lyrics should come earlier.
    return Lyrics([words for _, words in timed], [max(0, ms - offset) for ms, _ in timed])
