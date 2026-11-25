"""Looking albums up on MusicBrainz, and working out what that would change.

MusicBrainz needs no account or key, but every request must name the app and a
way to reach its makers (the User-Agent), and there may be at most about one
request per second: _wait_turn() keeps to that for the whole app. When the
server is busy it answers 503 (or 429); the request is then tried again after a
pause. Its data is public domain (CC0), so it may be written into the files.

Everything here runs on a background thread, except building the proposals,
which is quick.
"""

import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from src.i18n import tr

API_URL = "https://musicbrainz.org/ws/2"
RELEASE_PAGE_URL = "https://musicbrainz.org/release/"
USER_AGENT = "MusicHive ( https://github.com/music-hive-dev/MusicHive )"
REQUEST_TIMEOUT = 20  # seconds
SECONDS_BETWEEN_REQUESTS = 1.1  # MusicBrainz allows one a second on average
BUSY_WAITS = (2, 5, 10, 20)  # seconds, when the server says it is busy; then give up
SEARCH_RESULTS = 10
LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')

# Tags a lookup can change, in the order they are shown.
TAG_ORDER = (
    "album", "albumartist", "date", "discnumber", "disctotal",
    "tracknumber", "tracktotal", "title", "artist",
)
NUMBER_TAGS = {"tracknumber", "tracktotal", "discnumber", "disctotal"}
# Changed without asking when they differ; titles and artists only when missing,
# as MusicBrainz's way of writing them (such as curly apostrophes) is a matter of taste.
TICKED_WHEN_DIFFERENT = {"album", "albumartist", "date", *NUMBER_TAGS}


class MusicBrainzError(Exception):
    pass


_turn_lock = threading.Lock()
_last_request = 0.0


def _wait_turn(cancel: threading.Event | None) -> bool:
    """Wait until another request may be sent. False if cancelled meanwhile."""
    global _last_request
    with _turn_lock:
        wait = _last_request + SECONDS_BETWEEN_REQUESTS - time.monotonic()
        if wait > 0:
            if cancel is not None and cancel.wait(wait):
                return False
            if cancel is None:
                time.sleep(wait)
        _last_request = time.monotonic()
    return True


def _get(path: str, params: dict[str, str], cancel: threading.Event | None = None) -> dict | None:
    """Ask the web service; None if cancelled. Raises MusicBrainzError."""
    query = urllib.parse.urlencode({**params, "fmt": "json"})
    request = urllib.request.Request(
        f"{API_URL}/{path}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    for attempt in range(len(BUSY_WAITS) + 1):
        if not _wait_turn(cancel):
            return None
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code in (429, 503) and attempt < len(BUSY_WAITS):
                retry = (error.headers.get("Retry-After") or "").strip() if error.headers else ""
                wait = int(retry) if retry.isdigit() else BUSY_WAITS[attempt]
                if cancel is not None and cancel.wait(wait):
                    return None
                if cancel is None:
                    time.sleep(wait)
                continue
            if error.code in (429, 503):
                raise MusicBrainzError(tr("MusicBrainz is busy; try again later")) from error
            if error.code == 404:
                raise MusicBrainzError(tr("MusicBrainz does not know that release")) from error
            raise MusicBrainzError(f"HTTP {error.code} {error.reason}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise MusicBrainzError(tr("Cannot reach MusicBrainz: {error}").format(
                error=error,
            )) from error
        except ValueError as error:
            raise MusicBrainzError(tr("MusicBrainz sent something unexpected")) from error
    return None


def _credit(credits: list | None) -> str:
    """An artist credit as one name, such as "Simon & Garfunkel"."""
    return "".join(
        (part.get("name") or part.get("artist", {}).get("name", "")) + (part.get("joinphrase") or "")
        for part in credits or []
    ).strip()


def plain(text: str) -> str:
    """Text with case, accents, "&" and punctuation taken out, for comparing."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"\s*[&+]\s*", " and ", text)
    return re.sub(r"[\W_]+", "", text)


def _year(text: str) -> str:
    return text[:4] if text[:4].isdigit() else ""


@dataclass
class Candidate:
    """A release a search found."""

    mbid: str
    title: str
    artist: str
    date: str
    country: str
    track_count: int
    formats: str  # such as "2×CD"
    status: str
    score: int  # MusicBrainz's own, 0 to 100
    rank: float = 0.0  # ours: how well it fits the songs

    @property
    def description(self) -> str:
        parts = [f"{self.title} – {self.artist}"]
        parts += [part for part in (self.date, self.country, self.formats) if part]
        parts.append(tr("%n track(s)", "", self.track_count))
        if self.status and self.status != "Official":
            parts.append(self.status.lower())
        return " · ".join(parts)


@dataclass
class Track:
    disc: int
    number: int
    title: str
    artist: str
    length_ms: int | None


@dataclass
class Release:
    mbid: str
    title: str
    artist: str
    date: str  # of this release, such as "2009-09-09"
    first_date: str  # when the album first came out
    discs: dict[int, int]  # disc number -> how many tracks it has
    tracks: list[Track] = field(default_factory=list)


def search_releases(
    artist: str, album: str, song_count: int, year: str, cancel: threading.Event | None = None
) -> list[Candidate] | None:
    """Releases that may be this album, the best fitting first. None if cancelled."""
    def escape(text: str) -> str:
        return LUCENE_SPECIAL.sub(r"\\\1", text)

    query = f'release:"{escape(album)}"'
    if artist and artist.casefold() != "various artists":
        query += f' AND artist:"{escape(artist)}"'
    elif artist:
        query += ' AND artist:"Various Artists"'
    answer = _get("release", {"query": query, "limit": str(SEARCH_RESULTS)}, cancel)
    if answer is None:
        return None
    candidates = []
    for entry in answer.get("releases", []):
        media = entry.get("media") or []
        formats: dict[str, int] = {}
        for medium in media:
            name = medium.get("format") or ""
            if name:
                formats[name] = formats.get(name, 0) + 1
        candidate = Candidate(
            mbid=entry.get("id", ""),
            title=entry.get("title", ""),
            artist=_credit(entry.get("artist-credit")),
            date=entry.get("date", "") or "",
            country=entry.get("country", "") or "",
            track_count=int(entry.get("track-count") or sum(m.get("track-count", 0) for m in media)),
            formats=" + ".join(f"{n}×{name}" if n > 1 else name for name, n in formats.items()),
            status=entry.get("status", "") or "",
            score=int(entry.get("score") or 0),
        )
        rank = float(candidate.score)
        if candidate.track_count == song_count:
            rank += 25
        if plain(candidate.title) == plain(album):
            rank += 10
        if year and _year(candidate.date) == year:
            rank += 5
        if candidate.status == "Official":
            rank += 5
        candidate.rank = rank
        candidates.append(candidate)
    candidates.sort(key=lambda c: -c.rank)
    return candidates


def fetch_release(mbid: str, cancel: threading.Event | None = None) -> Release | None:
    """A release with all its tracks. None if cancelled."""
    answer = _get(f"release/{mbid}", {"inc": "recordings artist-credits release-groups"}, cancel)
    if answer is None:
        return None
    group = answer.get("release-group") or {}
    release = Release(
        mbid=mbid,
        title=answer.get("title", ""),
        artist=_credit(answer.get("artist-credit")),
        date=answer.get("date", "") or "",
        first_date=group.get("first-release-date", "") or "",
        discs={},
    )
    for medium in answer.get("media") or []:
        disc = int(medium.get("position") or 1)
        tracks = medium.get("tracks") or []
        release.discs[disc] = len(tracks)
        for index, track in enumerate(tracks, start=1):
            number = track.get("position") or index
            release.tracks.append(
                Track(
                    disc=disc,
                    number=int(number),
                    title=track.get("title") or (track.get("recording") or {}).get("title", ""),
                    artist=_credit(track.get("artist-credit"))
                    or _credit((track.get("recording") or {}).get("artist-credit"))
                    or release.artist,
                    length_ms=track.get("length") or (track.get("recording") or {}).get("length"),
                )
            )
    return release


@dataclass
class Change:
    path: Path
    tag: str
    now: str
    new: str
    ticked: bool


def match_tracks(songs: list, release: Release) -> dict[Path, Track]:
    """Which track of the release each song is: by title, number and length.
    Songs that fit no track well enough are left out."""
    scores = []
    for song in songs:
        title = plain(song.values["title"] or song.path.stem)
        for index, track in enumerate(release.tracks):
            score = 0.0
            other = plain(track.title)
            if title and title == other:
                score += 4
            elif title and other and (other in title or title in other):
                score += 2
            if song.track == track.number:
                score += 3 if (song.disc or 1) == track.disc else 1
            if song.duration_ms and track.length_ms:
                gap = abs(song.duration_ms - track.length_ms)
                score += 2 if gap <= 3000 else 1 if gap <= 10000 else -2 if gap > 30000 else 0
            if score >= 3:
                scores.append((score, song.path, index))
    scores.sort(key=lambda entry: -entry[0])
    matched: dict[Path, Track] = {}
    taken: set[int] = set()
    for _score, path, index in scores:
        if path not in matched and index not in taken:
            matched[path] = release.tracks[index]
            taken.add(index)
    return matched


def propose(
    songs: list, release: Release, original_year: bool, year_only: bool
) -> tuple[list[Change], int]:
    """What the release would change in the songs, and how many songs fit no track.

    `songs` are check_tags.Song objects. Album tags go to every song; track tags
    only to the songs matched to a track. With `original_year`, the year is when
    the album first came out rather than when this release did. With `year_only`,
    dates are written as just the year, and full dates the songs have are
    shortened to it too."""
    matched = match_tracks(songs, release)
    date = (release.first_date if original_year else "") or release.date
    if year_only:
        date = _year(date) or date
    several_discs = len(release.discs) > 1
    changes: list[Change] = []
    for song in songs:
        new: dict[str, str] = {"album": release.title, "albumartist": release.artist}
        if date:
            new["date"] = date
        track = matched.get(song.path)
        if track is not None:
            new["title"] = track.title
            new["artist"] = track.artist
            new["tracknumber"] = str(track.number)
            new["tracktotal"] = str(release.discs.get(track.disc, 0) or "")
            if several_discs or song.disc is not None:
                new["discnumber"] = str(track.disc)
                new["disctotal"] = str(len(release.discs))
        now = {
            **{tag: song.values.get(tag, "") for tag in ("album", "albumartist", "title", "artist")},
            "date": getattr(song, "full_date", "") or song.values.get("date", ""),
            "tracknumber": "" if song.track is None else str(song.track),
            "tracktotal": "" if song.track_total is None else str(song.track_total),
            "discnumber": "" if song.disc is None else str(song.disc),
            "disctotal": "" if song.disc_total is None else str(song.disc_total),
        }
        for tag in TAG_ORDER:
            value = new.get(tag, "")
            if not value:
                continue
            current = now[tag]
            if tag == "date" and year_only:
                same = value == current  # a full date is shortened to the year
            elif tag == "date":
                same = _year(value) == _year(current)  # a full date is not needed
            else:
                same = value == current
            if same:
                continue
            ticked = not current or tag in TICKED_WHEN_DIFFERENT
            changes.append(Change(song.path, tag, current, value, ticked))
    return changes, len(songs) - len(matched)
