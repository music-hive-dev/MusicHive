"""Last.fm: logging in, scrobbling, "now playing" and play counts.

Logging in follows Last.fm's desktop flow: the user allows MusicHive in their
browser, so MusicHive never sees their password. Calls are signed the way
Last.fm documents it: sort the parameters by name, join name and value pairs,
append the shared secret and take the MD5 of the result.
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from src.library_db import track_key
from src.scrobble_backlog import DEFAULT_BACKLOG_PER_DAY, ImportResult, ScrobbleBacklog

API_URL = "https://ws.audioscrobbler.com/2.0/"
AUTH_URL = "https://www.last.fm/api/auth/"
USER_AGENT = "MusicHive/1.0"
REQUEST_TIMEOUT = 20  # seconds
LOGIN_POLL_SECONDS = 3  # how often to ask whether the user has allowed us yet
LOGIN_TIMEOUT_SECONDS = 300
SCROBBLE_BATCH = 50  # Last.fm accepts at most 50 scrobbles per request
RETRY_SECONDS = 300  # when to try again after a failed scrobble
IGNORED_DAILY_CAP = 5  # Last.fm's reason code for a scrobble over the daily cap
CAP_RETRY_SECONDS = 3600  # when to try again after reaching that cap
BACKLOG_PAUSE_SECONDS = 10  # between batches of an imported log
BACKLOG_CHECK_SECONDS = 600  # how often to see whether the next day's sending can start
PAGE_PAUSE_SECONDS = 0.4  # pause between pages when downloading play counts


class LastFmError(Exception):
    """An error answer from Last.fm. `code` is -1 for "could not connect"."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ----- the web service -----


def _signature(params: dict[str, str], secret: str) -> str:
    text = "".join(
        f"{name}{params[name]}"
        for name in sorted(params)
        if name not in ("format", "callback")
    )
    return hashlib.md5((text + secret).encode("utf-8")).hexdigest()


def _call(api_key, secret, method, params=None, *, post=False, sign=False) -> dict:
    """Call one Last.fm method and return its JSON answer."""
    data = {"method": method, "api_key": api_key}
    data.update(params or {})
    if sign:
        data["api_sig"] = _signature(data, secret)
    data["format"] = "json"
    encoded = urllib.parse.urlencode(data)

    headers = {"User-Agent": USER_AGENT}
    if post:
        request = urllib.request.Request(API_URL, data=encoded.encode("ascii"), headers=headers)
    else:
        request = urllib.request.Request(f"{API_URL}?{encoded}", headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        try:  # Last.fm explains its errors in the body of the answer
            payload = json.load(error)
        except ValueError:
            raise LastFmError(0, f"HTTP {error.code} {error.reason}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise LastFmError(-1, f"no connection to Last.fm ({error})") from error
    except ValueError as error:
        raise LastFmError(0, "unexpected answer from Last.fm") from error

    if isinstance(payload, dict) and "error" in payload:
        raise LastFmError(int(payload["error"]), str(payload.get("message", "unknown error")))
    return payload


def get_token(api_key: str, secret: str) -> str:
    return _call(api_key, secret, "auth.getToken", sign=True)["token"]


def get_session(api_key: str, secret: str, token: str) -> tuple[str, str]:
    """Exchange an allowed token for (username, session key)."""
    session = _call(api_key, secret, "auth.getSession", {"token": token}, sign=True)["session"]
    return session["name"], session["key"]


def update_now_playing(api_key: str, secret: str, session_key: str, entry: dict) -> None:
    params = {"artist": entry["artist"], "track": entry["title"], "sk": session_key}
    if entry.get("album"):
        params["album"] = entry["album"]
    if entry.get("album_artist"):
        params["albumArtist"] = entry["album_artist"]
    if entry.get("duration"):
        params["duration"] = str(int(entry["duration"]))
    _call(api_key, secret, "track.updateNowPlaying", params, post=True, sign=True)


def _ignored_codes(payload: dict) -> list[int]:
    """Last.fm's verdict on each scrobble of a request: 0, or why it was ignored."""
    scrobbles = payload.get("scrobbles")
    entries = scrobbles.get("scrobble", []) if isinstance(scrobbles, dict) else []
    if isinstance(entries, dict):  # a single scrobble is not wrapped in a list
        entries = [entries]
    codes = []
    for entry in entries:
        message = entry.get("ignoredMessage") if isinstance(entry, dict) else None
        code = str(message.get("code", "0")) if isinstance(message, dict) else "0"
        codes.append(int(code) if code.isdigit() else 0)
    return codes


def scrobble(api_key: str, secret: str, session_key: str, entries: list[dict]) -> list[int]:
    """Scrobble up to 50 plays in one request. Returns the verdict on each one.

    Over the daily cap, the request still succeeds: those plays are just ignored.
    """
    params = {"sk": session_key}
    for i, entry in enumerate(entries):
        params[f"artist[{i}]"] = entry["artist"]
        params[f"track[{i}]"] = entry["title"]
        params[f"timestamp[{i}]"] = str(int(entry["timestamp"]))
        if entry.get("album"):
            params[f"album[{i}]"] = entry["album"]
        if entry.get("album_artist"):
            params[f"albumArtist[{i}]"] = entry["album_artist"]
        if entry.get("duration"):
            params[f"duration[{i}]"] = str(int(entry["duration"]))
    return _ignored_codes(_call(api_key, secret, "track.scrobble", params, post=True, sign=True))


def fetch_play_counts(
    api_key: str, user: str, progress: Callable[[int, int], None]
) -> dict[str, int]:
    """Every track in the user's Last.fm history, with its play count."""
    counts: dict[str, int] = {}
    page, total_pages = 1, 1
    while page <= total_pages:
        payload = _call(
            api_key,
            "",
            "user.getTopTracks",
            {"user": user, "period": "overall", "limit": "1000", "page": str(page)},
        )
        top = payload.get("toptracks", {})
        total_pages = int(top.get("@attr", {}).get("totalPages", 1) or 1)
        tracks = top.get("track", [])
        if isinstance(tracks, dict):  # a single track is not wrapped in a list
            tracks = [tracks]
        for track in tracks:
            artist = track.get("artist", {})
            artist_name = artist.get("name", "") if isinstance(artist, dict) else str(artist)
            key = track_key(artist_name, track.get("name", ""))
            counts[key] = max(counts.get(key, 0), int(track.get("playcount", 0) or 0))
        progress(page, total_pages)
        page += 1
        if page <= total_pages:
            time.sleep(PAGE_PAUSE_SECONDS)
    return counts


def fetch_artist_bio(api_key: str, artist: str) -> str:
    """The short biography of an artist as plain text, "" if Last.fm has none."""
    try:
        payload = _call(api_key, "", "artist.getInfo", {"artist": artist, "autocorrect": "1"})
    except LastFmError as error:
        if error.code == 6:  # Last.fm does not know this artist
            return ""
        raise
    summary = payload.get("artist", {}).get("bio", {}).get("summary", "")
    summary = re.sub(r"<a\b[^>]*>\s*Read more on Last\.fm\s*</a>\.?", "", summary, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", summary)).strip()


def fetch_top_tags(api_key: str, artist: str, title: str, album: str) -> list[str]:
    """The tags listeners gave a song, or else its album, or else its artist; best first."""
    lookups = [
        ("track.getTopTags", {"artist": artist, "track": title}),
        ("album.getTopTags", {"artist": artist, "album": album}),
        ("artist.getTopTags", {"artist": artist}),
    ]
    for method, params in lookups:
        if not all(params.values()):
            continue  # the song does not have this information
        try:
            payload = _call(api_key, "", method, {**params, "autocorrect": "1"})
        except LastFmError as error:
            if error.code == 6:  # Last.fm does not know it
                continue
            raise
        tags = payload.get("toptags", {}).get("tag", [])
        if isinstance(tags, dict):  # a single tag is not wrapped in a list
            tags = [tags]
        names = [
            name
            for tag in tags
            if int(tag.get("count", 0) or 0) >= 10
            and not (name := str(tag.get("name", "")).strip()).isdigit()  # years are not genres
        ]
        if names:
            return names
    return []


# ----- running requests in the background -----


class _Signals(QObject):
    done = Signal(str, object)  # what was asked, the result
    failed = Signal(str, int, str)  # what was asked, Last.fm's error code, message
    progress = Signal(int, int)  # page, total pages (play count download)


class _Task(QRunnable):
    def __init__(self, tag: str, function: Callable, signals: _Signals) -> None:
        super().__init__()
        self._tag = tag
        self._function = function
        self._signals = signals

    def run(self) -> None:
        try:
            result = self._function()
        except LastFmError as error:
            self._signals.failed.emit(self._tag, error.code, error.message)
        except Exception as error:  # never let a thread die silently
            self._signals.failed.emit(self._tag, 0, str(error))
        else:
            self._signals.done.emit(self._tag, result)


class LastFmClient(QObject):
    """Everything MusicHive does with Last.fm."""

    state_changed = Signal()  # login state, or the number of waiting scrobbles, changed
    status_message = Signal(str)  # something worth telling the user
    counts_progress = Signal(int, int)
    counts_ready = Signal(object)  # {track_key: play count}
    counts_failed = Signal(str)

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        settings: QSettings,
        queue_path: Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._api_key = api_key
        self._secret = api_secret
        self._settings = settings
        self._queue_path = queue_path

        self.username: str | None = None
        self._session_key: str | None = None
        self.login_state = "out"  # "out", "waiting" (for the browser) or "in"
        self.is_downloading = False
        self.remember = settings.value("lastfm/remember", True, type=bool)

        self._token: str | None = None
        self._login_deadline = 0.0
        self._polling = False
        self._sending = False
        self._sent_count = 0
        self._sending_backlog = False  # the batch being sent comes from an imported log
        self._prepared_cursor = 0  # the backlog's re-date position after that batch
        self._hold_until = 0.0  # no plays are sent before this, after reaching the daily cap
        self._queue: list[dict] = self._load_queue()
        self._backlog = ScrobbleBacklog(queue_path.with_name("lastfm-backlog.json"))
        self.backlog_per_day = settings.value(
            "lastfm/backlogPerDay", DEFAULT_BACKLOG_PER_DAY, type=int
        )

        self._signals = _Signals(self)
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)
        self._signals.progress.connect(self.counts_progress)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(LOGIN_POLL_SECONDS * 1000)
        self._poll_timer.timeout.connect(self._poll_login)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self.flush)
        self._backlog_timer = QTimer(self)
        self._backlog_timer.setSingleShot(True)
        self._backlog_timer.timeout.connect(self._send_backlog)

        self._restore_session()
        if self._queue or self._backlog.count:
            QTimer.singleShot(5000, self.flush)

    # ----- state -----

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self._secret)

    @property
    def is_logged_in(self) -> bool:
        return self.login_state == "in"

    @property
    def pending_count(self) -> int:
        """Scrobbles that have not been sent to Last.fm yet."""
        return len(self._queue)

    @property
    def backlog_count(self) -> int:
        """Plays from an imported log that are still waiting their turn."""
        return self._backlog.count

    def set_backlog_per_day(self, per_day: int) -> None:
        self.backlog_per_day = per_day
        self._settings.setValue("lastfm/backlogPerDay", per_day)
        self.state_changed.emit()
        self.flush()

    def set_remember(self, remember: bool) -> None:
        self.remember = remember
        self._save_session()

    # ----- logging in and out -----

    def start_login(self) -> None:
        if not self.is_configured or self.login_state != "out":
            return
        self._set_state("waiting")
        self._run("token", lambda: get_token(self._api_key, self._secret))

    def cancel_login(self) -> None:
        if self.login_state == "waiting":
            self._poll_timer.stop()
            self._token = None
            self._set_state("out")

    def logout(self) -> None:
        self._poll_timer.stop()
        self._token = None
        self.username = None
        self._session_key = None
        self._queue.clear()  # plays of the old account are not sent to another
        self._save_queue()
        self._backlog.clear()
        self._save_session()
        self._set_state("out")
        self.status_message.emit("Logged out of Last.fm")

    def _poll_login(self) -> None:
        if self.login_state != "waiting" or self._token is None:
            self._poll_timer.stop()
            return
        if time.monotonic() > self._login_deadline:
            self._fail_login("Timed out waiting for you to allow MusicHive. Please try again.")
            return
        if self._polling:
            return
        self._polling = True
        token = self._token
        self._run("session", lambda: get_session(self._api_key, self._secret, token))

    def _fail_login(self, text: str) -> None:
        self._poll_timer.stop()
        self._token = None
        self._polling = False
        self._set_state("out")
        self.status_message.emit(text)

    def _session_expired(self) -> None:
        """Last.fm no longer accepts our session (for example it was revoked)."""
        self.username = None
        self._session_key = None
        self._save_session()
        self._set_state("out")
        self.status_message.emit("Your Last.fm login is no longer valid. Please log in again.")

    def _set_state(self, state: str) -> None:
        self.login_state = state
        self.state_changed.emit()

    # ----- scrobbling -----

    def enqueue_scrobble(self, entry: dict) -> None:
        """Remember a play to send to Last.fm (does nothing when logged out)."""
        if not self._session_key:
            return
        self._queue.append(entry)
        self._save_queue()
        self.state_changed.emit()
        self.flush()

    def flush(self) -> None:
        """Send waiting scrobbles, up to 50 at a time. Plays made here go first."""
        if self._sending or not self._session_key:
            return
        if not self._queue:
            self._arm_backlog(BACKLOG_PAUSE_SECONDS)
        elif time.time() >= self._hold_until:
            self._start_sending(self._queue[:SCROBBLE_BATCH], False)

    def _start_sending(self, batch: list[dict], from_backlog: bool) -> None:
        self._sent_count = len(batch)
        self._sending = True
        self._sending_backlog = from_backlog
        session_key = self._session_key
        self._run("scrobble", lambda: scrobble(self._api_key, self._secret, session_key, batch))

    # ----- an imported log -----

    def import_scrobble_log(self, path: Path) -> ImportResult:
        """Add a player's log to the backlog. Raises OSError if it cannot be read."""
        result = self._backlog.add(path)
        self.state_changed.emit()
        self.flush()
        return result

    def _arm_backlog(self, seconds: int) -> None:
        if self._backlog.count and not self._backlog_timer.isActive():
            self._backlog_timer.start(seconds * 1000)

    def _send_backlog(self) -> None:
        if self._sending or not self._session_key or not self._backlog.count:
            return
        if self._queue:
            self.flush()
            return
        now = time.time()
        per_day = self.backlog_per_day
        room = self._backlog.room(now, per_day)
        if room <= 0:
            self._arm_backlog(BACKLOG_CHECK_SECONDS)
            return
        batch, self._prepared_cursor = self._backlog.prepare(
            min(room, SCROBBLE_BATCH), now, per_day
        )
        self._start_sending(batch, True)

    def _on_scrobbled(self, codes: list[int]) -> None:
        """Last.fm answered a batch. Plays over its daily cap stay and are sent later."""
        self._sending = False
        count = self._sent_count
        capped = [
            position for position, code in enumerate(codes[:count]) if code == IGNORED_DAILY_CAP
        ]
        now = time.time()
        self._backlog.note_sent(count - len(capped), now, self._sending_backlog)
        if self._sending_backlog:
            self._backlog.finish(capped, self._prepared_cursor)
        else:
            self._queue[:count] = [self._queue[position] for position in capped]
            self._save_queue()
        if capped:
            self._backlog.mark_full(now)
            self._hold_until = now + CAP_RETRY_SECONDS * 0.9  # timers can fire a little early
            self._retry_timer.start(CAP_RETRY_SECONDS * 1000)
            self.status_message.emit("Last.fm's daily limit was reached. The rest is sent later.")
        self.state_changed.emit()
        self.flush()

    def now_playing(self, entry: dict) -> None:
        if self._session_key:
            session_key = self._session_key
            entry = dict(entry)
            self._run(
                "now_playing",
                lambda: update_now_playing(self._api_key, self._secret, session_key, entry),
            )

    def _on_scrobble_failed(self, code: int, message: str) -> None:
        self._sending = False
        if code == 9:  # invalid session key
            self._session_expired()
        elif code in (6, 13) and not self._sending_backlog:
            # The batch itself is bad: drop it so it cannot block the rest. An
            # imported log is not dropped, as a problem there would empty it batch by batch.
            del self._queue[: self._sent_count]
            self._save_queue()
            self.state_changed.emit()
            self.status_message.emit(f"Last.fm refused some scrobbles: {message}")
            self.flush()
        else:  # no connection, or Last.fm is busy: try again later
            self.status_message.emit(
                f"Could not reach Last.fm ({message}). {len(self._queue)} scrobble(s) will be sent later."
            )
            self._retry_timer.start(RETRY_SECONDS * 1000)

    # ----- play counts -----

    def download_play_counts(self) -> None:
        """Fetch the play count of every track in the user's Last.fm history."""
        if not self.is_logged_in or self.is_downloading or not self.username:
            return
        self.is_downloading = True
        self.state_changed.emit()
        user = self.username
        progress = self._signals.progress.emit
        self._run("counts", lambda: fetch_play_counts(self._api_key, user, progress))

    # ----- results from the background threads -----

    def _run(self, tag: str, function: Callable) -> None:
        self._pool.start(_Task(tag, function, self._signals))

    def _on_done(self, tag: str, result) -> None:
        if tag == "token":
            if self.login_state != "waiting":
                return  # the user cancelled in the meantime
            self._token = result
            url = f"{AUTH_URL}?api_key={self._api_key}&token={self._token}"
            QDesktopServices.openUrl(QUrl(url))
            self._login_deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
            self._poll_timer.start()
        elif tag == "session":
            self._polling = False
            if self.login_state != "waiting":
                return
            name, key = result
            self._poll_timer.stop()
            self._token = None
            self.username, self._session_key = name, key
            self._save_session()
            self._set_state("in")
            self.status_message.emit(f"Logged in to Last.fm as {name}")
            self.flush()
        elif tag == "scrobble":
            self._on_scrobbled(result)
        elif tag == "counts":
            self.is_downloading = False
            self.state_changed.emit()
            self.counts_ready.emit(result)

    def _on_failed(self, tag: str, code: int, message: str) -> None:
        print(f"MusicHive: Last.fm {tag} failed ({code}): {message}", file=sys.stderr)
        if tag == "token":
            self._fail_login(f"Could not start the Last.fm login: {message}")
        elif tag == "session":
            self._polling = False
            if code in (14, -1):  # not allowed yet, or no connection: keep waiting
                return
            self._fail_login(f"Last.fm login failed: {message}")
        elif tag == "scrobble":
            self._on_scrobble_failed(code, message)
        elif tag == "now_playing":
            if code == 9:
                self._session_expired()
        elif tag == "counts":
            self.is_downloading = False
            self.state_changed.emit()
            self.counts_failed.emit(message)

    # ----- saving -----

    def _restore_session(self) -> None:
        if not (self.remember and self.is_configured):
            return
        name = self._settings.value("lastfm/username", "", type=str)
        key = self._settings.value("lastfm/sessionKey", "", type=str)
        if name and key:
            self.username, self._session_key = name, key
            self.login_state = "in"

    def _save_session(self) -> None:
        """Keep the session key (never the password) if the user wants to stay logged in."""
        settings = self._settings
        if self.remember and self._session_key:
            settings.setValue("lastfm/username", self.username)
            settings.setValue("lastfm/sessionKey", self._session_key)
        else:
            settings.remove("lastfm/username")
            settings.remove("lastfm/sessionKey")
        settings.setValue("lastfm/remember", self.remember)
        settings.sync()
        try:
            os.chmod(settings.fileName(), 0o600)  # only the user may read it
        except OSError:
            pass

    def _load_queue(self) -> list[dict]:
        try:
            data = json.loads(self._queue_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save_queue(self) -> None:
        try:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._queue_path.with_suffix(".tmp")
            temp.write_text(json.dumps(self._queue), encoding="utf-8")
            os.replace(temp, self._queue_path)
        except OSError:
            pass

    def shutdown(self) -> None:
        self._poll_timer.stop()
        self._retry_timer.stop()
        self._backlog_timer.stop()
        self._save_queue()
        self._pool.waitForDone(3000)