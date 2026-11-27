"""The Subsonic tab: search the server (and, through octo-fiesta, streaming
services), have the server download songs, play them from it, or import them
into the library. Also the party's way to the server (SubsonicPartySource).
"""

import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import QIcon, QImage
from PySide6.QtWidgets import QListWidgetItem, QMenu

from src.formatting import format_time
from src.i18n import tr
from src.party import Guest, PartyError
from src.podcast_view import (
    HEADING_ROLE,
    ID_ROLE,
    LINES_ROLE,
    CoverRowDelegate,
    square_pixmap,
)
from src.subsonic import (
    SubsonicAlbum,
    SubsonicError,
    SubsonicService,
    SubsonicSong,
    provider_name,
)

COVER_SIDE = 96  # pixels asked of the server for a result's cover
ICON_SIDE = 48


def _from(item: SubsonicSong | SubsonicAlbum) -> str:
    """"from Deezer" for something only a streaming service has so far."""
    if not item.external:
        return ""
    return tr("from {service}").format(service=provider_name(item.provider))


class SubsonicTab(QObject):
    """The tab in player.ui. main.py plays songs and adds imported ones to the library."""

    play_song = Signal(object)  # a SubsonicSong, to play from the server
    songs_imported = Signal(object, object)  # library root, [(ScannedTrack, covers)]
    open_settings = Signal()

    def __init__(
        self,
        w,
        service: SubsonicService,
        library_root: Callable[[], Path | None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.w = w
        self.service = service
        self._library_root = library_root
        self._songs: list[SubsonicSong] = []
        self._albums: list[SubsonicAlbum] = []
        self._search_request = 0
        self._album_request = 0
        self._album_title = ""
        self._kept_results: tuple | None = None  # the search, while an album is open
        self._waiting: dict[int, Callable[[list[SubsonicSong]], None]] = {}  # album lists
        self._jobs: dict[int, tuple[str, object]] = {}  # job -> (kind, what)
        self._next_job = 0
        self._placeholder = QIcon(str(Path(__file__).parent.parent / "assets" / "eighth-notes.svg"))

        self._import_tip = w.subsonicImport.toolTip()  # as player.ui has it
        view = w.subsonicResults
        view.setItemDelegate(CoverRowDelegate(view))
        view.itemDoubleClicked.connect(self._on_double_clicked)
        view.itemSelectionChanged.connect(self._update_buttons)
        view.customContextMenuRequested.connect(self._show_menu)
        w.subsonicSearch.returnPressed.connect(self.search)
        w.subsonicSearchButton.clicked.connect(lambda _checked=False: self.search())
        w.subsonicBackButton.clicked.connect(lambda _checked=False: self._back_to_results())
        w.subsonicBackButton.hide()
        w.subsonicDownload.clicked.connect(lambda _checked=False: self.download_selected())
        w.subsonicDownloadPlay.clicked.connect(lambda _checked=False: self.play_selected())
        w.subsonicImport.clicked.connect(lambda _checked=False: self.import_selected())
        w.subsonicSetupButton.clicked.connect(lambda _checked=False: self.open_settings.emit())
        service.searched.connect(self._on_searched)
        service.album_listed.connect(self._on_album_listed)
        service.cover_ready.connect(self._on_cover)
        service.job_done.connect(self._on_job_done)
        self.refresh()

    # ----- the page -----

    def refresh(self) -> None:
        """The server settings changed: show the search, or how to connect."""
        w = self.w
        w.subsonicPages.setCurrentWidget(
            w.subsonicBrowsePage if self.service.configured else w.subsonicSetupPage
        )
        self._update_buttons()

    def _set_status(self, text: str) -> None:
        self.w.subsonicStatus.setText(text)
        self.w.subsonicStatus.setToolTip(text)

    def _show_jobs(self) -> None:
        """How many downloads and imports are still going."""
        downloads = sum(1 for kind, _what in self._jobs.values() if kind in ("server", "play"))
        imports = sum(1 for kind, _what in self._jobs.values() if kind == "import")
        parts = []
        if downloads:
            parts.append(tr("Downloading %n on the server…", "", downloads))
        if imports:
            parts.append(tr("Importing %n…", "", imports))
        self._set_status(" · ".join(parts))

    # ----- searching -----

    def search(self) -> None:
        text = self.w.subsonicSearch.text().strip()
        if not text:
            return
        self._kept_results = None
        self.w.subsonicBackButton.hide()
        self.w.subsonicTitle.setText(tr("Searching for “{text}”…").format(text=text))
        self.w.subsonicResults.clear()
        self._search_request = self.service.search(text)

    def _on_searched(self, request: int, songs: list, albums: list, error: str) -> None:
        if request != self._search_request:
            return  # a newer search is on its way
        text = self.w.subsonicSearch.text().strip()
        if error:
            self.w.subsonicTitle.setText(error)
            return
        if not songs and not albums:
            self.w.subsonicTitle.setText(tr("Nothing found for “{text}”").format(text=text))
        else:
            self.w.subsonicTitle.setText(tr("Results for “{text}”").format(text=text))
        self._show(songs, albums)

    def _show(self, songs: list[SubsonicSong], albums: list[SubsonicAlbum]) -> None:
        self._songs, self._albums = songs, albums
        view = self.w.subsonicResults
        view.clear()
        if songs:
            self._add_heading(tr("Songs"))
            for index, song in enumerate(songs):
                length = format_time(song.duration_s * 1000) if song.duration_s else ""
                second = " · ".join(p for p in (song.artist, song.album, length, _from(song)) if p)
                self._add_row(song.title, second, ("song", index), song.cover)
        if albums:
            self._add_heading(tr("Albums"))
            for index, album in enumerate(albums):
                count = tr("%n song(s)", "", album.song_count) if album.song_count else ""
                second = " · ".join(p for p in (album.artist, album.year, count, _from(album)) if p)
                self._add_row(album.name, second, ("album", index), album.cover)
        self._update_buttons()

    def _add_heading(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setData(HEADING_ROLE, True)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.w.subsonicResults.addItem(item)

    def _add_row(self, title: str, second: str, key: tuple[str, int], cover: str) -> None:
        item = QListWidgetItem(self._placeholder, title)
        item.setData(LINES_ROLE, [title, second])
        item.setData(ID_ROLE, key)
        item.setData(ID_ROLE + 10, cover)
        item.setToolTip(f"{title}\n{second}")
        self.w.subsonicResults.addItem(item)
        if cover:
            self.service.fetch_cover(cover, COVER_SIDE)

    def _on_cover(self, cover: str, data) -> None:
        if not data:
            return
        image = QImage.fromData(data)
        if image.isNull():
            return
        icon = QIcon(square_pixmap(image, ICON_SIDE))
        view = self.w.subsonicResults
        for row in range(view.count()):
            item = view.item(row)
            if item.data(ID_ROLE + 10) == cover:
                item.setIcon(icon)

    # ----- an album's songs -----

    def _open_album(self, album: SubsonicAlbum) -> None:
        if self._kept_results is None:
            self._kept_results = (self._songs, self._albums, self.w.subsonicTitle.text())
        self._album_title = " – ".join(p for p in (album.name, album.artist) if p)
        self.w.subsonicTitle.setText(tr("Opening {album}…").format(album=self._album_title))
        self.w.subsonicBackButton.show()
        self._album_request = self.service.list_album(album.id)
        self._waiting[self._album_request] = self._show_album

    def _show_album(self, songs: list[SubsonicSong]) -> None:
        self.w.subsonicTitle.setText(self._album_title)
        self._show(songs, [])

    def _back_to_results(self) -> None:
        if self._kept_results is not None:
            songs, albums, title = self._kept_results
            self._kept_results = None
            self.w.subsonicTitle.setText(title)
            self._show(songs, albums)
        self.w.subsonicBackButton.hide()

    def _on_album_listed(self, request: int, songs: list, error: str) -> None:
        then = self._waiting.pop(request, None)
        if then is None:
            return
        if error:
            self._set_status(error)
            return
        then(songs)

    # ----- what is chosen -----

    def _chosen(self) -> tuple[list[SubsonicSong], list[SubsonicAlbum]]:
        songs, albums = [], []
        for item in self.w.subsonicResults.selectedItems():
            key = item.data(ID_ROLE)
            if not key:
                continue
            kind, index = key
            if kind == "song" and index < len(self._songs):
                songs.append(self._songs[index])
            elif kind == "album" and index < len(self._albums):
                albums.append(self._albums[index])
        return songs, albums

    def _update_buttons(self) -> None:
        songs, albums = self._chosen()
        anything = bool(songs or albums)
        w = self.w
        w.subsonicDownload.setEnabled(anything)
        w.subsonicImport.setEnabled(anything and self._library_root() is not None)
        w.subsonicImport.setToolTip(
            self._import_tip if self._library_root() is not None else tr("Open a library first")
        )
        w.subsonicDownloadPlay.setEnabled(len(songs) == 1 and not albums)

    def _with_songs(self, then: Callable[[list[SubsonicSong]], None]) -> None:
        """The chosen songs, with the songs of the chosen albums (asked of the server)."""
        songs, albums = self._chosen()
        if not albums:
            if songs:
                then(songs)
            return
        collected = list(songs)
        left = {"count": len(albums)}

        def add(album_songs: list[SubsonicSong]) -> None:
            collected.extend(album_songs)
            left["count"] -= 1
            if left["count"] == 0 and collected:
                then(collected)

        for album in albums:
            request = self.service.list_album(album.id)
            self._waiting[request] = add

    def _on_double_clicked(self, item: QListWidgetItem) -> None:
        key = item.data(ID_ROLE)
        if not key:
            return
        kind, index = key
        if kind == "album" and index < len(self._albums):
            self._open_album(self._albums[index])
        elif kind == "song" and index < len(self._songs):
            self._play(self._songs[index])

    def _show_menu(self, position: QPoint) -> None:
        view = self.w.subsonicResults
        if view.itemAt(position) is None:
            return
        songs, albums = self._chosen()
        menu = QMenu(view)
        w = self.w
        for button in (w.subsonicDownloadPlay, w.subsonicDownload, w.subsonicImport):
            action = menu.addAction(button.icon(), button.text())
            action.setEnabled(button.isEnabled())
            action.triggered.connect(lambda _checked=False, b=button: b.click())
        if len(albums) == 1 and not songs:
            menu.addSeparator()
            menu.addAction(tr("Show Songs")).triggered.connect(
                lambda _checked=False, a=albums[0]: self._open_album(a)
            )
        menu.exec(view.mapToGlobal(position))

    # ----- the three buttons -----

    def _start_job(self, kind: str, what: object) -> int:
        self._next_job += 1
        job = -self._next_job  # the tab's jobs are negative; main.py's are positive
        self._jobs[job] = (kind, what)
        self._show_jobs()
        return job

    def download_selected(self) -> None:
        def download(songs: list[SubsonicSong]) -> None:
            job = self._start_job("server", songs)
            self.service.fetch_to_server(job, songs)

        self._with_songs(download)

    def play_selected(self) -> None:
        songs, albums = self._chosen()
        if len(songs) == 1 and not albums:
            self._play(songs[0])

    def _play(self, song: SubsonicSong) -> None:
        """Songs the server has play at once; others once the server downloaded them."""
        if not song.external:
            self.play_song.emit(song)
            return
        job = self._start_job("play", song)
        self.service.fetch_to_server(job, [song])

    def import_selected(self) -> None:
        root = self._library_root()
        if root is None:
            self._set_status(tr("Open a library first"))
            return

        def bring_in(songs: list[SubsonicSong]) -> None:
            job = self._start_job("import", root)
            self.service.import_songs(job, songs, root)

        self._with_songs(bring_in)

    def _on_job_done(self, job: int, result, error: str) -> None:
        entry = self._jobs.pop(job, None)
        if entry is None:
            return  # one of main.py's (a party guest's song)
        kind, what = entry
        self._show_jobs()
        if error:
            self._set_status(error)
            return
        if kind == "play":
            self.play_song.emit(what)
            if not self._jobs:
                self._set_status("")
        elif kind == "server":
            if not self._jobs:
                self._set_status(tr("Downloaded %n song(s) on the server", "", result))
        elif kind == "import":
            self.songs_imported.emit(what, result)
            if not self._jobs:
                self._set_status(tr("Imported %n song(s)", "", len(result)))


class SubsonicPartySource:
    """Party guests search the server too. Their song is imported into the library
    (tagged guestdownload=true) and queued once it is there; `request_import` does
    that, on the main thread."""

    name = "subsonic"
    threaded = True  # search and covers go over the network, off the main thread

    def __init__(
        self,
        service: SubsonicService,
        request_import: Callable[[SubsonicSong, Guest], str],
    ) -> None:
        self._service = service
        self._request_import = request_import
        self._found: dict[str, SubsonicSong] = {}  # what guests were shown, by id
        self._lock = threading.Lock()

    def search(self, text: str, limit: int) -> list[dict]:
        if not self._service.configured:
            return []
        try:
            songs, _albums = self._service.client.search(text)
        except SubsonicError:
            return []
        results = []
        with self._lock:
            for song in songs[:limit]:
                self._found[song.id] = song
                results.append({
                    "id": song.id,
                    "title": song.title,
                    "artist": song.artist,
                    "album": song.album,
                    "cover": song.cover,
                    "note": _from(song) or tr("on the server"),
                })
        return results

    def cover(self, key: str) -> bytes | None:
        return self._service.cover_now(key, COVER_SIDE)

    def request(self, item: str, guest: Guest) -> str:
        with self._lock:
            song = self._found.get(item)
        if song is None:
            raise PartyError(tr("That song is no longer there. Search again."))
        return self._request_import(song, guest)
