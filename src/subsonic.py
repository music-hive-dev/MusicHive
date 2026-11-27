"""Talking to a Subsonic server, such as Navidrome with octo-fiesta in front of it.

octo-fiesta mixes songs from streaming services (Deezer, Qobuz…) into the
server's search results. Their ids start with "ext-", as in
"ext-deezer-song-123". Streaming one makes octo-fiesta download it into its own
folder on the server, after which the server has it like any other song.
Whether one song or its whole album is downloaded is octo-fiesta's own setting.

Nothing here touches the GUI; SubsonicService (below) runs the work on threads.
"""

import hashlib
import json
import os
import secrets
import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, QThreadPool, Signal

from src import library_db
from src.i18n import tr
from src.library_db import ScannedTrack
from src.level_volume import GAIN_TAG, PEAK_TAG, measure_loudness
from src.library_scanner import scan_file
from src.music_library import SUPPORTED_EXTENSIONS
from src.organise_files import (
    TEMPLATE_KEY,
    render_stem,
    tag_values,
    template_parts,
    template_problem,
)
from src.online_search import (
    HAS_LYRICS_TAG,
    LYRICS_PREFERENCE_KEY,
    LYRICS_SAVE_KEY,
    PREFER_TIMED,
    SAVE_TO_LRC,
    find_lyrics,
    is_instrumental,
    known_instrumental,
    lyrics_state,
    pick_lyrics,
)
from src.track_metadata import TagChanges, TrackInfo, read_track_info, write_tag_changes

URL_KEY = "subsonic/url"
USER_KEY = "subsonic/user"
PASSWORD_SECRET = "subsonic_password"  # kept in the secrets file, not in the settings
SECRETS_FILE_NAME = "secrets"  # as in main.py
API_VERSION = "1.16.1"
CLIENT_NAME = "MusicHive"
TIMEOUT = 20  # seconds for a normal request
DOWNLOAD_TIMEOUT = 600  # a server may take minutes to fetch a song or an album first
CHUNK = 256 * 1024
SEARCH_SONGS = 40
SEARCH_ALBUMS = 20
DEFAULT_TEMPLATE = "<albumartist>/<album>/<trackno> - <title> - <artist>"  # Organise Files' own
GUEST_DOWNLOAD_TAG = "guestdownload"  # "true" on songs a party guest asked for
LEVEL_KEY = "subsonic/levelImported"  # measure imported songs for Level Volume
LYRICS_KEY = "subsonic/lyricsImported"  # find lyrics for imported songs


@dataclass
class ImportExtras:
    """What else happens to a song as it comes in (Settings, Subsonic)."""

    level: bool = False  # measure its volume, as Tools > Level Volume does
    lyrics: bool = False  # look its lyrics up, as Tools > Find Lyrics does
    lyrics_preference: int = 0  # online_search's PREFER_TIMED and so on
    lyrics_save_to: int = SAVE_TO_LRC  # a .lrc file next to it, or in its tags


class SubsonicError(Exception):
    """What went wrong, in words for the user."""


# ----- the settings -----


@dataclass
class SubsonicConfig:
    url: str = ""
    user: str = ""
    password: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.url and self.user and self.password)


def _secrets_path() -> Path:
    return library_db.data_dir() / SECRETS_FILE_NAME


def _read_secrets() -> dict:
    try:
        data = json.loads(_secrets_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_config(settings: QSettings) -> SubsonicConfig:
    return SubsonicConfig(
        url=settings.value(URL_KEY, "", type=str).strip().rstrip("/"),
        user=settings.value(USER_KEY, "", type=str).strip(),
        password=str(_read_secrets().get(PASSWORD_SECRET, "")),
    )


def save_password(password: str) -> None:
    """Keep the password in the secrets file (only you can read it, and it is not
    part of an export of the settings). Raises OSError."""
    data = _read_secrets()
    data[PASSWORD_SECRET] = password
    path = _secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


# ----- what the server has -----


def provider_of(item_id: str) -> str:
    """"deezer" for "ext-deezer-song-123"; "" for a song the server has itself."""
    if not item_id.startswith("ext-"):
        return ""
    parts = item_id.split("-")
    return parts[1] if len(parts) > 2 else ""


def provider_name(provider: str) -> str:
    return {
        "deezer": "Deezer", "qobuz": "Qobuz", "tidal": "Tidal",
        "yandex": "Yandex Music", "squidwtf": "SquidWTF",
    }.get(provider, provider.capitalize())


@dataclass
class SubsonicSong:
    id: str
    title: str
    artist: str
    album: str
    album_id: str
    track: int | None
    year: str
    duration_s: int
    cover: str  # an id for getCoverArt, or ""
    suffix: str  # "flac", "mp3"… as the server says ("" if it doesn't)

    @property
    def provider(self) -> str:
        return provider_of(self.id)

    @property
    def external(self) -> bool:
        """Only on a streaming service so far: the server downloads it first."""
        return bool(self.provider)


@dataclass
class SubsonicAlbum:
    id: str
    name: str
    artist: str
    year: str
    song_count: int
    cover: str

    @property
    def provider(self) -> str:
        return provider_of(self.id)

    @property
    def external(self) -> bool:
        return bool(self.provider)


def _song(entry: dict) -> SubsonicSong:
    track = entry.get("track")
    return SubsonicSong(
        id=str(entry.get("id", "")),
        title=str(entry.get("title") or ""),
        artist=str(entry.get("artist") or ""),
        album=str(entry.get("album") or ""),
        album_id=str(entry.get("albumId") or ""),
        track=int(track) if isinstance(track, int) or str(track).isdigit() else None,
        year=str(entry.get("year") or ""),
        duration_s=int(entry.get("duration") or 0),
        cover=str(entry.get("coverArt") or ""),
        suffix=str(entry.get("suffix") or "").lower(),
    )


def _album(entry: dict) -> SubsonicAlbum:
    return SubsonicAlbum(
        id=str(entry.get("id", "")),
        name=str(entry.get("name") or entry.get("title") or ""),
        artist=str(entry.get("artist") or ""),
        year=str(entry.get("year") or ""),
        song_count=int(entry.get("songCount") or 0),
        cover=str(entry.get("coverArt") or ""),
    )


class SubsonicClient:
    """One server and account. Every call blocks, so call it off the main thread."""

    def __init__(self, config: SubsonicConfig) -> None:
        self.config = config

    def _query(self, **params) -> str:
        # Token login: the password itself never travels, only a salted hash of it.
        salt = secrets.token_hex(8)
        token = hashlib.md5((self.config.password + salt).encode("utf-8")).hexdigest()
        fields = {
            "u": self.config.user, "t": token, "s": salt,
            "v": API_VERSION, "c": CLIENT_NAME, "f": "json",
        }
        fields.update({key: value for key, value in params.items() if value is not None})
        return urllib.parse.urlencode(fields)

    def url(self, endpoint: str, **params) -> str:
        return f"{self.config.url}/rest/{endpoint}?{self._query(**params)}"

    def _call(self, endpoint: str, **params) -> dict:
        if not self.config.complete:
            raise SubsonicError(tr("Enter the server, user name and password in Settings, Subsonic."))
        request = urllib.request.Request(self.url(endpoint, **params))
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
                body = json.loads(answer.read())
        except urllib.error.HTTPError as error:
            raise SubsonicError(tr("The server answered: {error}").format(error=error)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise SubsonicError(tr("Cannot reach the server: {error}").format(error=reason)) from None
        except ValueError:
            raise SubsonicError(tr("That doesn't look like a Subsonic server.")) from None
        response = body.get("subsonic-response") if isinstance(body, dict) else None
        if not isinstance(response, dict):
            raise SubsonicError(tr("That doesn't look like a Subsonic server."))
        if response.get("status") != "ok":
            error = response.get("error") or {}
            raise SubsonicError(str(error.get("message") or tr("The server refused the request.")))
        return response

    def ping(self) -> str:
        """Check the login; returns the server's name and version, for the user."""
        response = self._call("ping")
        name = response.get("type") or "Subsonic"
        version = response.get("serverVersion") or response.get("version") or ""
        return f"{name} {version}".strip()

    def search(self, text: str) -> tuple[list[SubsonicSong], list[SubsonicAlbum]]:
        response = self._call(
            "search3", query=text, songCount=SEARCH_SONGS, albumCount=SEARCH_ALBUMS,
            artistCount=0,
        )
        found = response.get("searchResult3") or {}
        songs = [_song(entry) for entry in found.get("song") or [] if entry.get("id")]
        albums = [_album(entry) for entry in found.get("album") or [] if entry.get("id")]
        return songs, albums

    def album_songs(self, album_id: str) -> list[SubsonicSong]:
        album = self._call("getAlbum", id=album_id).get("album") or {}
        return [_song(entry) for entry in album.get("song") or [] if entry.get("id")]

    def cover(self, cover_id: str, size: int) -> bytes | None:
        try:
            with urllib.request.urlopen(
                self.url("getCoverArt", id=cover_id, size=size), timeout=TIMEOUT
            ) as answer:
                data = answer.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
        return None if data[:1] == b"{" else data  # an error comes back as JSON

    def stream_url(self, song: SubsonicSong) -> str:
        """Where to play the song from, as the file is (no converting on the server)."""
        return self.url("stream", id=song.id, format="raw")

    def fetch_to_server(self, song: SubsonicSong, cancel: threading.Event) -> None:
        """Have the server download the song into its folder (octo-fiesta), by
        streaming it and throwing the sound away. A song the server already has
        needs nothing."""
        if not song.external:
            return
        self._read(self.stream_url(song), None, cancel)

    def download(self, song: SubsonicSong, target: Path, cancel: threading.Event) -> None:
        """Save the song's file at `target`. Songs the server has come as they are
        stored; streaming-service songs are downloaded on the server first."""
        url = self.stream_url(song) if song.external else self.url("download", id=song.id)
        self._read(url, target, cancel)

    def _read(self, url: str, target: Path | None, cancel: threading.Event) -> None:
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as answer:
                kind = answer.headers.get("Content-Type", "")
                if "json" in kind or "xml" in kind:  # an error, not a song
                    raise SubsonicError(_error_text(answer.read()))
                file = open(target, "wb") if target is not None else None
                try:
                    while chunk := answer.read(CHUNK):
                        if cancel.is_set():
                            raise SubsonicError(tr("Stopped"))
                        if file is not None:
                            file.write(chunk)
                finally:
                    if file is not None:
                        file.close()
        except urllib.error.HTTPError as error:
            raise SubsonicError(tr("The server answered: {error}").format(error=error)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise SubsonicError(tr("The download failed: {error}").format(error=reason)) from None


def _error_text(body: bytes) -> str:
    try:
        error = json.loads(body)["subsonic-response"]["error"]
        return str(error.get("message") or "")
    except (ValueError, KeyError, TypeError):
        return tr("The server sent no song.")


# ----- bringing a song into the library -----


def audio_suffix(path: Path, fallback: str) -> str:
    """The file's real kind, from its first bytes: the server's "suffix" can be
    the streaming service's, not the file's."""
    try:
        with open(path, "rb") as file:
            head = file.read(12)
    except OSError:
        head = b""
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    return fallback or "mp3"


def _free_path(path: Path) -> Path:
    """`path`, or "name (2).flac" and so on if it is taken."""
    if not path.exists():
        return path
    for number in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise SubsonicError(tr("Too many files called {name}").format(name=path.name))


def _level(path: Path, cancel: threading.Event) -> None:
    """Measure the song and store its ReplayGain tags, as Level Volume does; it then
    plays at the library's own volume like the songs measured there."""
    measured = measure_loudness(path, cancel)
    if measured is None:
        return
    gain, peak = measured
    write_tag_changes(path, TagChanges({GAIN_TAG: f"{gain:+.2f} dB", PEAK_TAG: f"{peak:.6f}"}))


def _add_lyrics(path: Path, info: TrackInfo, extras: ImportExtras) -> None:
    """Look the song's lyrics up on LRCLIB, as Find Lyrics does, unless it has some."""
    existing, has_lyrics = lyrics_state(path)
    if existing is not None or known_instrumental(has_lyrics):
        return
    found = find_lyrics(info.artist, info.title, info.album, (info.duration_ms or 0) / 1000)
    if is_instrumental(found):
        write_tag_changes(path, TagChanges({HAS_LYRICS_TAG: "false"}))
        return
    text = pick_lyrics(found, extras.lyrics_preference)
    if not text:
        return
    if extras.lyrics_save_to == SAVE_TO_LRC:
        path.with_suffix(".lrc").write_text(text, encoding="utf-8")
        write_tag_changes(path, TagChanges({HAS_LYRICS_TAG: "true"}))
    else:
        write_tag_changes(path, TagChanges({"lyrics": text, HAS_LYRICS_TAG: "true"}))


def import_song(
    client: SubsonicClient,
    song: SubsonicSong,
    root: Path,
    template: str,
    guest_download: bool,
    cancel: threading.Event,
    extras: ImportExtras | None = None,
) -> tuple[ScannedTrack, dict[str, bytes]]:
    """Copy a song from the server into the library at `root`, named by the
    Organise Files template, and read it as a scan would. Songs a party guest
    asked for get the tag guestdownload=true. With `extras`, its volume is
    measured and its lyrics looked up on the way."""
    folder = Path(tempfile.mkdtemp(prefix="musichive-import-"))
    try:
        temp = folder / "song"
        client.download(song, temp, cancel)
        suffix = audio_suffix(temp, song.suffix)
        if f".{suffix}" not in SUPPORTED_EXTENSIONS:
            raise SubsonicError(
                tr("{title} is a .{kind} file, which the library can't take yet.").format(
                    title=song.title, kind=suffix
                )
            )
        named = temp.with_suffix(f".{suffix}")
        temp.rename(named)
        if guest_download:
            try:
                write_tag_changes(named, TagChanges({GUEST_DOWNLOAD_TAG: "true"}))
            except Exception:  # an odd file still comes in, just without the tag
                pass
        info, _cover = read_track_info(named)
        has_title = bool(info.title) and info.title != named.stem
        if not has_title:  # files from a stream may have no tags yet
            info.title = song.title
        info.artist = info.artist or song.artist
        info.album = info.album or song.album
        info.year = info.year or song.year
        if info.track_number is None:
            info.track_number = song.track
        if template_problem(template) is not None:
            template = DEFAULT_TEMPLATE
        stem = render_stem(template_parts(template), tag_values(info, True), song.title)
        target = _free_path(root / f"{stem}.{suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(named), str(target))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    extras = extras or ImportExtras()
    # Either failing leaves the song as it came: it is in the library all the same.
    if extras.level and not cancel.is_set():
        try:
            _level(target, cancel)
        except Exception as error:
            print(f"MusicHive: cannot level {target.name}: {error}")
    if extras.lyrics and not cancel.is_set():
        try:
            _add_lyrics(target, info, extras)
        except Exception as error:
            print(f"MusicHive: cannot find lyrics for {target.name}: {error}")
    covers: dict[str, bytes] = {}
    return scan_file(target, root, covers, set()), covers


# ----- running it all in the background -----


class _Signals(QObject):
    searched = Signal(int, object, object, str)  # request, songs, albums, error
    album_listed = Signal(int, object, str)  # request, songs, error
    cover_ready = Signal(str, object)  # cover id, bytes or None
    tested = Signal(str, str)  # what the server is, error
    job_done = Signal(int, object, str)  # job, result, error


class _Task(QRunnable):
    def __init__(self, work: Callable[[], None]) -> None:
        super().__init__()
        self._work = work

    def run(self) -> None:
        self._work()


class SubsonicService(QObject):
    """Searches, covers, server downloads and imports, on background threads.
    Each result comes back as a signal on the main thread."""

    searched = Signal(int, object, object, str)
    album_listed = Signal(int, object, str)
    cover_ready = Signal(str, object)
    tested = Signal(str, str)
    job_done = Signal(int, object, str)

    def __init__(self, settings: QSettings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.client = SubsonicClient(load_config(settings))
        self._signals = _Signals()
        for name in ("searched", "album_listed", "cover_ready", "tested", "job_done"):
            getattr(self._signals, name).connect(getattr(self, name))
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._downloads = QThreadPool(self)  # one at a time, so the server isn't flooded
        self._downloads.setMaxThreadCount(1)
        self._cancel = threading.Event()
        self._request = 0
        self._covers: dict[str, bytes | None] = {}  # kept for this session
        self._covers_lock = threading.Lock()

    # ----- settings -----

    @property
    def configured(self) -> bool:
        return self.client.config.complete

    def reload_config(self) -> None:
        self.client = SubsonicClient(load_config(self._settings))
        with self._covers_lock:
            self._covers.clear()

    def template(self) -> str:
        return self._settings.value(TEMPLATE_KEY, DEFAULT_TEMPLATE, type=str) or DEFAULT_TEMPLATE

    def extras(self) -> ImportExtras:
        settings = self._settings
        return ImportExtras(
            level=settings.value(LEVEL_KEY, True, type=bool),
            lyrics=settings.value(LYRICS_KEY, True, type=bool),
            lyrics_preference=settings.value(LYRICS_PREFERENCE_KEY, PREFER_TIMED, type=int),
            lyrics_save_to=settings.value(LYRICS_SAVE_KEY, SAVE_TO_LRC, type=int),
        )

    # ----- asking -----

    def test(self, config: SubsonicConfig) -> None:
        client = SubsonicClient(config)

        def work() -> None:
            try:
                self._signals.tested.emit(client.ping(), "")
            except SubsonicError as error:
                self._signals.tested.emit("", str(error))

        self._pool.start(_Task(work))

    def search(self, text: str) -> int:
        self._request += 1
        request, client = self._request, self.client

        def work() -> None:
            try:
                songs, albums = client.search(text)
                self._signals.searched.emit(request, songs, albums, "")
            except SubsonicError as error:
                self._signals.searched.emit(request, [], [], str(error))

        self._pool.start(_Task(work))
        return request

    def list_album(self, album_id: str) -> int:
        self._request += 1
        request, client = self._request, self.client

        def work() -> None:
            try:
                self._signals.album_listed.emit(request, client.album_songs(album_id), "")
            except SubsonicError as error:
                self._signals.album_listed.emit(request, [], str(error))

        self._pool.start(_Task(work))
        return request

    def fetch_cover(self, cover_id: str, size: int) -> None:
        if not cover_id:
            return
        key = f"{cover_id}@{size}"
        with self._covers_lock:
            if key in self._covers:
                self.cover_ready.emit(cover_id, self._covers[key])
                return
        client = self.client

        def work() -> None:
            data = client.cover(cover_id, size)
            with self._covers_lock:
                self._covers[key] = data
            self._signals.cover_ready.emit(cover_id, data)

        self._pool.start(_Task(work))

    def cover_now(self, cover_id: str, size: int) -> bytes | None:
        """The same, but waits for it: for threads that may wait (the party page)."""
        key = f"{cover_id}@{size}"
        with self._covers_lock:
            if key in self._covers:
                return self._covers[key]
        data = self.client.cover(cover_id, size)
        with self._covers_lock:
            self._covers[key] = data
        return data

    # ----- downloading (one job number each; job_done reports how it went) -----

    def fetch_to_server(self, job: int, songs: list[SubsonicSong]) -> None:
        """Have the server download these songs. The result is how many."""
        client, cancel = self.client, self._cancel

        def work() -> None:
            try:
                for song in songs:
                    client.fetch_to_server(song, cancel)
                self._signals.job_done.emit(job, len(songs), "")
            except SubsonicError as error:
                self._signals.job_done.emit(job, None, str(error))

        self._downloads.start(_Task(work))

    def import_songs(
        self, job: int, songs: list[SubsonicSong], root: Path, guest_download: bool = False
    ) -> None:
        """Copy songs into the library at `root`. The result is a list of
        (ScannedTrack, covers) for those that came in."""
        client, cancel, template, extras = self.client, self._cancel, self.template(), self.extras()

        def work() -> None:
            done, failed = [], ""
            for song in songs:
                try:
                    done.append(import_song(
                        client, song, root, template, guest_download, cancel, extras
                    ))
                except SubsonicError as error:
                    failed = str(error)
                    if cancel.is_set():
                        break
                except OSError as error:
                    failed = tr("Cannot save the song: {error}").format(error=error)
            self._signals.job_done.emit(job, done, "" if done else failed)

        self._downloads.start(_Task(work))

    def shutdown(self) -> None:
        self._cancel.set()
        self._pool.waitForDone(2000)
        self._downloads.waitForDone(2000)
