"""Casting to a web browser: a page MusicHive serves, opened on a TV (or anywhere).

    MusicHive ──events (what to play, play, pause, seek…)──> the page
    MusicHive <──status (playing, where, ended)── the page
    MusicHive ──songs, covers──> the page's audio player

The page (assets/tv/) plays the songs itself and draws the visualiser and lyrics
from the sound it plays, so they always match what is heard. To MusicHive it is
just another device: a CastSession, like a Chromecast.
"""

import queue
import threading

from PySide6.QtGui import QColor

from src import theme
from src.cast import CastDevice, CastMedia, CastSession, MediaServer
from src.cover_colours import CoverColours
from src.lyrics import parse_lyrics
from src.lyrics_settings import LyricsSettings
from src.now_playing import (
    FLAG_GRADIENT,
    GRADIENT,
    LOUD_DB,
    QUIET_DB,
    TILT_DB_PER_OCTAVE,
    _light_text,
)
from src.visualiser_settings import (
    COVER,
    FALL_PER_FRAME,
    HIGHLIGHT,
    RAINBOW,
    VisualiserSettings,
    bar_colours,
)

_KEPT = ("style", "volume", "mute", "party")  # sent again to a page that (re)connects
_PAGE_EVENTS = 256  # events waiting for a page that does not read them: it is let go


def screen_style(
    visual: VisualiserSettings,
    lyrics: LyricsSettings,
    cover: CoverColours | None,
) -> dict:
    """How the page should look: the visualiser and lyrics as MusicHive has them."""
    from_cover = visual.colours == COVER and cover is not None
    if from_cover:
        colours = [cover.bars] * visual.bars
    else:  # (without a cover, "From the cover" uses the highlight colour)
        colours = bar_colours(visual.colours, visual.bars, QColor(theme.active_highlight()))
    text = QColor(theme.colour(theme.TEXT))
    if lyrics.cover_colour and cover is not None and cover.lyrics is not None:
        text = cover.lyrics
    shading = GRADIENT if visual.colours in (HIGHLIGHT, RAINBOW, COVER) else FLAG_GRADIENT
    return {
        "bars": visual.bars,
        "gap": visual.gap,
        "opacity": visual.opacity / 100,
        "fall": FALL_PER_FRAME[visual.fall_speed],  # per frame at 30 a second
        "colours": [c.name() for c in colours],
        "shading": list(shading),  # percent darker at the foot, lighter at the top
        "glow": visual.glow,
        "coverBackground": visual.background,
        "blur": visual.background_blur,
        "darken": visual.background_darken / 100,
        "fit": visual.background_fit,  # 0 fill, 1 fit, 2 stretch
        "barBackground": cover.background.name() if from_cover else "",
        "quietDb": QUIET_DB,
        "loudDb": LOUD_DB,
        "tiltDb": TILT_DB_PER_OCTAVE,
        "lightText": _light_text(),
        "window": theme.colour("#151416"),  # the darkest surface, in this theme
        "text": text.name(),
        "neutral": QColor(theme.colour(theme.TEXT)).name(),  # white, or black in light mode
        "shadow": lyrics.shadow,
        "shadowBlur": lyrics.shadow_blur,
        "shadowDistance": lyrics.shadow_distance,
        "shadowStrength": lyrics.shadow_strength / 100,
        "showNoLyrics": lyrics.show_no_lyrics,
    }


class BrowserSession(CastSession):
    """The page as a device. Commands become events for every open copy of the
    page; the newest status a page sent is what the device "says"."""

    def __init__(self, device: CastDevice, server: MediaServer) -> None:
        self._server = server
        self._pages: list[queue.Queue] = []
        self._pages_lock = threading.Lock()
        self._kept: dict[str, dict] = {}  # the latest of each _KEPT event
        self._current: dict | None = None  # the latest "load", for a page that joins
        self._wants_playing = False
        self._load_id = 0
        self._page_status: dict | None = None
        # Offered at once, so the address can be shown straight away.
        self.address = server.open_browser(self, device.code)
        super().__init__(device)

    # ----- the page's side (web server threads) -----

    def attach(self) -> queue.Queue:
        """A page connected: it gets what it needs to show what plays now."""
        events: queue.Queue = queue.Queue(maxsize=_PAGE_EVENTS)
        with self._pages_lock:
            for name, data in self._kept.items():
                events.put_nowait((name, data))
            if self._current is not None:
                with self._lock:
                    position = self._position_ms
                load = dict(self._current, position_ms=position, autoplay=self._wants_playing)
                events.put_nowait(("load", load))
            self._pages.append(events)
        return events

    def detach(self, events: queue.Queue) -> None:
        with self._pages_lock:
            if events in self._pages:
                self._pages.remove(events)

    def receive(self, status: dict) -> None:
        """A page's status: {id, state, position_ms, duration_ms, ended, error}."""
        if isinstance(status, dict):
            with self._lock:
                self._page_status = status

    def request(self, action: str, value: int) -> None:
        """A button on the page: MusicHive does what its own button would."""
        if action in ("playpause", "next", "previous", "seek"):
            with self._lock:
                self._requests.append((action, value))

    def send_bars(self, bars: dict | None) -> None:
        """The bar map of the song playing now is ready (None: none can be made,
        so the page works the bars out itself)."""
        self._queue(self._send_bars, bars)

    def _send_bars(self, bars: dict | None) -> None:
        if self._current is None:
            return
        self._current["bars"] = bars
        self._current["barsComing"] = False
        self._send("bars", {"id": self._load_id, "bars": bars})

    def set_style(self, style: dict) -> None:
        """How the page should look (see screen_style)."""
        self._queue(self._send, "style", style)

    def set_party(self, card: dict) -> None:
        """The Party Mode card: {shown, qr (a data: address), text, address}."""
        self._queue(self._send, "party", card)

    # ----- events -----

    def _send(self, name: str, data: dict) -> None:
        with self._pages_lock:
            if name in _KEPT:
                self._kept[name] = data
            for events in list(self._pages):
                try:
                    events.put_nowait((name, data))
                except queue.Full:  # a page that stopped listening
                    self._pages.remove(events)
                    events.queue.clear()
                    events.put_nowait(None)

    # ----- CastSession -----

    def _connect(self) -> None:
        # The page connects to MusicHive whenever it is opened. It starts at full
        # volume, so the TV's own volume decides; the slider is moved to show it.
        self._send("volume", {"percent": 100})
        self._report_volume(100)

    def _quit(self) -> None:
        self._send("stop", {})

    def _disconnect(self) -> None:
        with self._pages_lock:
            pages, self._pages = self._pages, []
        for events in pages:
            events.queue.clear()
            events.put_nowait(None)

    def _do_load(self, media: CastMedia) -> None:
        self._starting_new_media()
        lyrics = parse_lyrics(media.lyrics or "")
        with self._lock:
            self._load_id += 1
            self._page_status = None
        self._wants_playing = media.autoplay
        self._current = {
            "id": self._load_id,
            "url": media.url,
            "kind": media.kind,
            "title": media.title,
            "artist": media.artist,
            "album": media.album,
            "addedBy": media.added_by,
            "cover": media.cover_url,
            "live": media.live,
            "lyrics": {"texts": lyrics.texts, "times": lyrics.times_ms},
            "noLyricsNote": media.lyrics is not None,  # a song, so "No lyrics" may show
            "gainDb": media.gain_db,
            "bars": media.bars,
            "barsComing": media.bars_coming,
        }
        self._send("load", dict(
            self._current, position_ms=media.position_ms, autoplay=media.autoplay
        ))

    def _do_play(self) -> None:
        self._wants_playing = True
        self._send("play", {})

    def _do_pause(self) -> None:
        self._wants_playing = False
        self._send("pause", {})

    def _do_seek(self, position_ms: int) -> None:
        self._send("seek", {"position_ms": position_ms})

    def _stop(self) -> None:
        self._wants_playing = False
        self._current = None
        self._send("stop", {})

    def _set_volume(self, percent: int) -> None:
        self._send("volume", {"percent": percent})

    def _do_mute(self, muted: bool) -> None:
        self._send("mute", {"muted": muted})

    def _poll(self) -> None:
        with self._lock:
            status, load_id = self._page_status, self._load_id
        if not status or status.get("id") != load_id:
            return  # no page open, or it still plays what came before
        state = status.get("state")
        if state not in ("playing", "paused", "buffering", "idle"):
            return
        ended = status.get("ended") or ""
        if status.get("error"):
            ended = "error"
        self._update(
            state,
            int(status.get("position_ms") or 0),
            int(status.get("duration_ms") or 0),
            ended if ended in ("finished", "error") else "",
        )
