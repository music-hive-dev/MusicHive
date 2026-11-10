"""MusicHive: application startup and UI wiring."""

import hashlib
import html
import sqlite3
import sys
import time
from pathlib import Path
import random
import json
from dataclasses import dataclass

from PySide6.QtCore import (
    QDir,
    QEvent,
    QFile,
    QIODevice,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QRect,
    QSettings,
    QStandardPaths,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (
    QAction,
    QCursor,
    QDesktopServices,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPalette,
    QPixmap,
    QShortcut,
)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSlider,
    QApplication,
    QDialogButtonBox,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QProxyStyle,
    QSlider,
    QStyle,
    QToolTip,
    QInputDialog,
    QStackedWidget,
    QTabWidget,
    QWidget,
)

from src import library_db
from src.artist_info import ArtistInfoService
from src.audio_player import AudioPlayer
from src.cover_view import SquareCover
from src.formatting import format_long_duration, format_size, format_time
from src.library_db import LibraryInfo, SavedSession
from src.columns_dialog import ColumnsDialog
from src.filter_bar import FilterBar
from src.library_groups import GROUPERS, TrackGroup, group_key
from src.side_list import (
    COMPUTER_SECTION,
    DEVICE,
    DEVICES_SECTION,
    FOLDER,
    PIN,
    PLAYLIST,
    PLAYLISTS_SECTION,
    SECTION,
    SideListDelegate,
    SideListModel,
    entry_id,
)
from src.library_scanner import ScanSignals, ScanTask
from src.play_order import PlayOrder
from src.discord_cover import DiscordCovers
from src.discord_rpc import DiscordPresence, make_activity
from src.elided_label import ElidedLabel
from src.lastfm import LastFmClient
from src.lastfm_dialog import LastFmDialog, load_dialog
from src.listening import ListenTracker
from src.m3u import M3uTrack, read_m3u, write_m3u
from src.mpris import NO_TRACK, MprisService, cover_file_url, file_url
from src.track_list import (
    all_columns,
    columns_from_keys,
    text_key,
    tag_column,
    QueueModel,
    SortedTrackModel,
    TrackListModel,
    setup_track_view,
)
from src.online_search import (
    AUTO_TAG_CANCELLED,
    AutoTagSong,
    OnlineSearch,
    ask_auto_tag_count,
)
from src.convert_files import ConvertFilesTool, ConvertSong
from src.device_sync import DeviceSyncPanel
from src.devices import DeviceEjector, DevicesTab, connected_devices, open_in_dolphin
from src.equaliser import EqualiserWindow, load_equaliser
from src.level_volume import LevelVolumeTool
from src.sound_settings import SoundSettingsTab, load_sound_settings
from src.organise_files import FileMove, OrganiseDialog, OrganiseResult
from src.tag_editor import TagEditorDialog
from src.tag_writer import SaveResult, TagJob, TagWriter
from src.now_playing import LyricsView, Visualizer
from src.star_rating import StarRating
from src.tray import TrayMinimizer
from src.track_metadata import TagChanges, read_editable_file, read_track_details
from src.playlist_models import (
    ClickFilter,
    HistoryTrackModel,
    PlaylistSortedModel,
    PlaylistTrackModel,
    IconAnimation,
    is_animated_gif,
    make_square_icon,
)
from src.auto_playlist import TIME_TAG, apply_limit, song_matches
from src.playlist_dialog import AutoPlaylistEditDialog, PlaylistDetails, PlaylistEditDialog
from src.playlists import Playlist, PlaylistStore


UI_DIR = Path(__file__).parent / "ui"
UI_FILE = UI_DIR / "player.ui"
DEVICE_CHECK_MS = 3000  # how often to look for devices being plugged in or removed
ASSETS_DIR = Path(__file__).parent / "assets"
# The Application ID of your Discord application (public, not a secret).
# Leave it empty to disable the Discord menu item.
DISCORD_CLIENT_ID = "1568612126409498656"
DISCORD_PREUPLOAD_COUNT = 2  # how many of the next tracks get their cover uploaded early
# Your Last.fm API account (see the setup steps). The secret is not meant to be public.
def load_secrets() -> dict:
    """Read API secrets from ~/Music/.musichive/secrets (kept out of the code)."""
    path = library_db.data_dir() / "secrets"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
_SECRETS = load_secrets()
LASTFM_API_KEY = _SECRETS.get("lastfm_api_key", "")
LASTFM_API_SECRET = _SECRETS.get("lastfm_api_secret", "")
LASTFM_UI_FILE = UI_DIR / "lastfm.ui"
PLAYLIST_EDIT_UI_FILE = UI_DIR / "playlist_edit.ui"
AUTO_PLAYLIST_EDIT_UI_FILE = UI_DIR / "auto_playlist_edit.ui"
AUTO_PLAYLIST_RULE_UI_FILE = UI_DIR / "auto_playlist_rule.ui"
COLUMNS_EDIT_UI_FILE = UI_DIR / "columns_edit.ui"
TAG_EDIT_UI_FILE = UI_DIR / "tag_edit.ui"
SETTINGS_UI_FILE = UI_DIR / "settings.ui"
AUTO_TAG_UI_FILE = UI_DIR / "auto_tag_genres.ui"
ORGANISE_UI_FILE = UI_DIR / "organise_files.ui"
EQUALISER_UI_FILE = UI_DIR / "equaliser.ui"
LEVEL_VOLUME_UI_FILE = UI_DIR / "level_volume.ui"
CONVERT_UI_FILE = UI_DIR / "convert_files.ui"
COVER_WINDOW_UI_FILE = UI_DIR / "cover_window.ui"

# How long after a track starts before Last.fm is told what is playing.
NOW_PLAYING_DELAY_MS = 3000

# If the track is further in than this, Previous restarts it instead of
# going back to the previous track.
PREVIOUS_RESTART_THRESHOLD_MS = 5000

GROUP_ICON_SIDE = 128  # pixels, the image at the top of the Artists and Albums tabs
# Starts a saved play source that is an artist or album. Playlist names cannot
# contain "/", so this can never be mistaken for one.
GROUP_SOURCE_PREFIX = "/"

SONGS_FILTER_OWNER = "songs"  # the Songs tab, in the saved filters
SONG_SEARCH_DELAY_MS = 200
SEARCH_CLEAR_ICON_SCALE = 0.55  # how much of its button the search bar's clear icon fills
HISTORY_SOURCE = "history"  # the History tab, as a list that can be played from
# Offered in an auto-playlist's rules even before a library has been rescanned.
COMMON_RULE_TAGS = ("album", "albumartist", "artist", "date", "genre", "rating", "time", "title")

# What MPRIS calls the playback states and repeat modes.
MPRIS_STATUS = {"playing": "Playing", "paused": "Paused", "stopped": "Stopped"}
MPRIS_LOOP_STATUS = {"off": "None", "one": "Track", "all": "Playlist"}

# Volume change for one notch of the mouse wheel, in percent.
VOLUME_WHEEL_STEP = 1
VOLUME_WHEEL_SHIFT_STEP = 10
# Qt reports one wheel notch as this much angle delta.
WHEEL_NOTCH = 120
# Qt closes a tooltip after 10 seconds by default. The volume tooltip should
# wait for the mouse to move, so it gets a limit it will not reach in practice.
VOLUME_TOOLTIP_MS = 3_600_000
# How far (in pixels) the mouse can drift before the volume tooltip closes.
VOLUME_TOOLTIP_DRIFT = 2

REPEAT_STATUS = {
    "off": "Repeat off",
    "all": "Repeat all tracks",
    "one": "Repeat current track",
}

# Icon (a file name in assets/, without ".svg") for each context menu entry.
MENU_ICONS = {
    "play_now": "play",
    "play_next": "list-start",
    "add_to_queue": "list-plus",
    "add_to_playlist": "list-music",
    "edit": "pencil",
    "delete_from_playlist": "x",
    "delete_file": "trash",
    "go_to": "library",
    "remove_from_upcoming": "minus",
    "end_after": "eject",
    "new_playlist": "plus",
    "edit_playlist": "pencil",
    "play_playlist": "play",
    "rename_playlist": "pencil",
    "playlist_icon": "library",
    "remove_playlist_icon": "x",
    "delete_playlist": "trash",
    "lock_playlist": "lock",
    "import_playlist": "list-plus",
    "export_playlist": "list-music",
    "duplicate_playlist": "clipboard-copy",
    "new_folder": "folder-plus",
    "rename_folder": "pencil",
    "delete_folder": "trash",
    "move_to_folder": "list-start",
    "pin_folder": "pin",
    "open_folder": "door-open",
    "unpin_folder": "pin-off",
    "sync_device": "refresh-cw",
    "open_device": "folder-output",
    "remove_device": "eject",
}

# "Go to" entries: (menu label, name of the TrackInfo field to match)
GO_TO_FIELDS = (
    ("Artist", "artist"),
    ("Title", "title"),
    ("Album", "album"),
    ("Year", "year"),
    ("Album artist", "album_artist"),
    ("Genre", "genre"),
)

NO_PLAYLIST_TEXT = "No playlist selected"
M3U8_FILTER = "M3U8 playlist (*.m3u8)"
M3U_FILTER = "M3U playlist (*.m3u)"


def load_ui(path: Path):
    """Load a Qt Designer .ui file and return the window it describes."""
    ui_file = QFile(str(path))
    if not ui_file.open(QIODevice.ReadOnly):
        raise RuntimeError(f"Cannot open UI file {path}: {ui_file.errorString()}")
    try:
        loader = QUiLoader()
        loader.setWorkingDirectory(QDir(str(path.parent)))
        window = loader.load(ui_file)
    finally:
        ui_file.close()
    if window is None:
        raise RuntimeError(f"Failed to load {path}: {loader.errorString()}")
    return window


def asset_icon(name: str) -> QIcon:
    """An icon from the assets folder: asset_icon("play") loads assets/play.svg."""
    path = ASSETS_DIR / f"{name}.svg"
    icon = QIcon(str(path))
    if icon.isNull():
        print(f"MusicHive: missing or unreadable icon: {path}", file=sys.stderr)
    return icon


def small_asset_icon(name: str, scale: float) -> QIcon:
    """An icon from the assets folder, drawn smaller on a transparent square.

    For places where Qt always draws an icon at one size.
    """
    side = 64
    pixmap = QPixmap(side, side)
    pixmap.fill(Qt.GlobalColor.transparent)
    glyph = round(side * scale)
    margin = (side - glyph) // 2
    painter = QPainter(pixmap)
    asset_icon(name).paint(painter, QRect(margin, margin, glyph, glyph))
    painter.end()
    return QIcon(pixmap)


def setup_library_table(view, model) -> None:
    """Give the library table its model, look and column widths."""
    view.setModel(model)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setAlternatingRowColors(True)
    view.setShowGrid(False)
    view.setWordWrap(False)
    view.setTextElideMode(Qt.TextElideMode.ElideRight)
    view.verticalHeader().hide()
    view.verticalHeader().setDefaultSectionSize(26)

    header = view.horizontalHeader()
    header.setHighlightSections(False)
    header.setStretchLastSection(False)
    for index, column in enumerate(model.columns):
        view.setColumnWidth(index, column.width)

    # Click a column title to sort by it; click it again to reverse the order.
    view.setSortingEnabled(True)
    view.sortByColumn(-1, Qt.SortOrder.AscendingOrder)  # start in library order


class JumpToClickStyle(QProxyStyle):
    """Makes a left click on a slider's groove jump straight to that position."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_Slider_AbsoluteSetButtons:
            return Qt.MouseButton.LeftButton.value
        return super().styleHint(hint, option, widget, returnData)


class CenteredTabIconStyle(QProxyStyle):
    """Centres the icon of a tab with no text, where Qt would keep it at the left."""

    def drawControl(self, element, option, painter, widget=None):
        if (
            element != QStyle.ControlElement.CE_TabBarTabLabel
            or option.text
            or option.icon.isNull()
        ):
            super().drawControl(element, option, painter, widget)
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        area = option.rect
        if not selected:  # styles draw inactive tabs slightly lower
            shift = self.pixelMetric(QStyle.PixelMetric.PM_TabBarTabShiftVertical, option, widget)
            area = area.adjusted(0, shift, 0, 0)
        icon_rect = QRect(0, 0, option.iconSize.width(), option.iconSize.height())
        icon_rect.moveCenter(area.center())
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        option.icon.paint(
            painter,
            icon_rect,
            Qt.AlignmentFlag.AlignCenter,
            QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled,
            QIcon.State.On if selected else QIcon.State.Off,
        )


class VolumeWheelFilter(QObject):
    """Changes the volume when the mouse wheel is used on a watched widget.

    One notch is 1%, or 10% with Shift held. The new value is shown next to
    the cursor until the mouse moves.
    """

    def __init__(self, slider: QSlider) -> None:
        super().__init__(slider)
        self._slider = slider
        self._leftover = 0  # touchpads report fractions of a notch

    def eventFilter(self, watched, event) -> bool:
        if event.type() != QEvent.Type.Wheel:
            return False

        angle = event.angleDelta()
        self._leftover += angle.y() or angle.x()
        notches = int(self._leftover / WHEEL_NOTCH)
        self._leftover -= notches * WHEEL_NOTCH

        shift = event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        step = VOLUME_WHEEL_SHIFT_STEP if shift else VOLUME_WHEEL_STEP
        self._slider.setValue(self._slider.value() + notches * step)

        # Qt has already closed any open tooltip because of this wheel event,
        # so it is always shown again. It closes once the mouse leaves `area`.
        pos = event.position().toPoint()
        drift = VOLUME_TOOLTIP_DRIFT
        area = QRect(pos, pos).adjusted(-drift, -drift, drift, drift)
        QToolTip.showText(
            event.globalPosition().toPoint(),
            f"{self._slider.value()}%",
            watched,
            area,
            VOLUME_TOOLTIP_MS,
        )
        event.accept()
        return True


@dataclass
class GroupPage:
    """The Artists or Albums tab: its filter bar picks what its table shows."""

    source: str  # "artist" or "album"
    tracks: PlaylistTrackModel
    proxy: SortedTrackModel
    view: QAbstractItemView
    header_key: str  # the setting that holds the table's column layout
    filter_bar: FilterBar
    kind: str = ""  # the tag its filter bar is showing
    current: TrackGroup | None = None  # the filter entry on screen


class MusicHive:
    """Connects the widgets from player.ui to the application logic."""

    def __init__(self) -> None:
        self.window = load_ui(UI_FILE)
        self.player = AudioPlayer()
        self.settings = QSettings("MusicHive", "MusicHive")
        w = self.window

        # Libraries
        self.libraries: list[LibraryInfo] = []
        self.current_library: LibraryInfo | None = None  # chosen in the drop-down
        self.loaded_library: LibraryInfo | None = None  # whose tracks are shown
        self.tracks: list[Path] = []  # indexed by library row
        self.current_index = -1  # library row of the playing track, or -1 if none

        # Which track plays next (play order, queue, shuffle, repeat)
        self.play_order = PlayOrder()

        # Playback
        self.playback_state = "stopped"  # "playing", "paused" or "stopped"
        self.position_ms = 0  # last reported playback position

        #Discord
        self.duration_ms = 0  # length of the loaded track, as the player reports it
        self.discord_active = False  # True once playback has started this session

        #LastFm
        self.listening = ListenTracker()  # measures how long the current track is heard
        self.listen_entry: dict | None = None  # what the current play is, captured when it began

        # Play counts and Last.fm scrobbling
        self.lastfm = LastFmClient(
            LASTFM_API_KEY,
            LASTFM_API_SECRET,
            self.settings,
            library_db.data_dir() / "lastfm-queue.json",
            w,
        )
        self.lastfm.status_message.connect(self.show_status)
        self._lastfm_dialog: LastFmDialog | None = None
        w.actionAudioScrobbleLastFm.triggered.connect(self.show_lastfm_dialog)

        self.listen_timer = QTimer(w)  # checks once a second whether a play now counts
        self.listen_timer.setInterval(1000)
        self.listen_timer.timeout.connect(self.check_listening)
        self.listen_timer.start()
        self.now_playing_timer = QTimer(w)
        self.now_playing_timer.setSingleShot(True)
        self.now_playing_timer.timeout.connect(self.send_now_playing)

        # Background scanning (one scan at a time)
        self.scan_pool = QThreadPool(self.window)
        self.scan_pool.setMaxThreadCount(1)
        self.scan_signals = ScanSignals(self.window)
        self.scan_task: ScanTask | None = None
        self.scan_library: LibraryInfo | None = None


        # Discord Rich Presence (off until the user turns it on in the menu)
        # Lets desktop widgets show and control the player
        self.mpris = MprisService(w)
        self.mpris_art_url = ""
        self.mpris.raise_requested.connect(self.raise_window)
        self.mpris.quit_requested.connect(self.quit_app)
        self.mpris.next_requested.connect(self.play_next)
        self.mpris.previous_requested.connect(self.play_previous)
        self.mpris.play_requested.connect(self.on_mpris_play)
        self.mpris.pause_requested.connect(self.on_mpris_pause)
        self.mpris.play_pause_requested.connect(self.on_play_pause)
        self.mpris.stop_requested.connect(self.player.stop)
        self.mpris.seek_requested.connect(self.on_mpris_seek)
        self.mpris.shuffle_requested.connect(w.shuffleButton.setChecked)
        self.mpris.loop_requested.connect(self.on_mpris_loop)
        self.mpris.volume_requested.connect(self.on_mpris_volume)

        self.discord = DiscordPresence(DISCORD_CLIENT_ID, self.build_discord_activity, w)
        self.discord.status_message.connect(self.show_status)
        self.discord_covers = DiscordCovers(self.settings, w)
        self.discord_covers.uploaded.connect(self.on_discord_cover_uploaded)
        discord_on =bool(DISCORD_CLIENT_ID) and self.settings.value(
            "discordPresence", False, type=bool
        )
        w.actionDiscordPresence.setCheckable(True)
        w.actionDiscordPresence.setChecked(discord_on)
        if not DISCORD_CLIENT_ID:
            w.actionDiscordPresence.setEnabled(False)
            w.actionDiscordPresence.setToolTip("Set DISCORD_CLIENT_ID in main.py first")
        self.discord.set_enabled(discord_on)
        w.actionDiscordPresence.toggled.connect(self.on_discord_toggled)

        # Icons used from code. (The rest are set in Qt Designer.)
        self.icon_play = asset_icon("play")
        self.icon_pause = asset_icon("pause")
        self.repeat_icons = {
            "off": asset_icon("repeat-off"),
            "all": asset_icon("repeat"),
            "one": asset_icon("repeat-1"),
        }
        self.shuffle_icons = {
            False: asset_icon("shuffle_off"),
            True: asset_icon("shuffle"),
        }
        self.menu_icons = {key: asset_icon(name) for key, name in MENU_ICONS.items()}

        self.volume_icons = {
            "muted": asset_icon("volume-off"),
            "silent": asset_icon("volume"),
            "low": asset_icon("volume-1"),
            "high": asset_icon("volume-2"),
        }

        # Left-click on a slider bar jumps to that position (instead of nudging).
        self._slider_style = JumpToClickStyle()
        w.progressSlider.setStyle(self._slider_style)
        w.volumeSlider.setStyle(self._slider_style)

        # Library table: the model holds the tracks (row n is library row n),
        # and the sorted model on top decides the order they are shown in.
        self.library_model = TrackListModel(w)
        self.library_model.set_columns(columns_from_keys(self.load_json_setting("tableColumns")))
        self.library_proxy = SortedTrackModel(w)
        self.library_proxy.setSourceModel(self.library_model)
        setup_library_table(w.libraryList, self.library_proxy)
        self.restore_table_layout()
        w.libraryList.doubleClicked.connect(self.play_item)

        # Upcoming tracks: the playing track first, then the tracks that will
        # follow it in play order. It reads its data from the library model.
        self.queue_model = QueueModel(self.library_model, w)
        setup_track_view(w.upcomingList, self.queue_model)
        w.upcomingList.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        w.upcomingList.doubleClicked.connect(self.play_upcoming_item)
        w.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, w.upcomingDock)
        self.setup_dock_toggles()

        # Right-click menus on the song lists
        for view, source in (
            (w.libraryList, "library"),
            (w.upcomingList, "upcoming"),
            (w.playlistSongList, "playlist"),
            (w.artistSongList, "artist"),
            (w.albumSongList, "album"),
            (w.historyList, HISTORY_SOURCE),
        ):
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(
                lambda pos, v=view, s=source: self.show_track_menu(v, pos, s)
            )

        # Playlists: saved in the library's data folder, one file per playlist
        self.playlist_store: PlaylistStore | None = None
        self.current_playlist: str | None = None  # the one on screen
        self.play_source: str | None = None  # the one being played (None = the library)
        self.source_group: str | None = None  # or the tab being played ("artist", "album")
        self.playlist_placeholder = asset_icon("list-music")  # for playlists without an icon
        self.auto_playlist_cache: dict[str, list[int]] = {}  # rules as text -> library rows

        # The side list: Playlists and Computer, which open and close when clicked.
        self.side_model = SideListModel(
            asset_icon("chevron-right"),
            asset_icon("chevron-down"),
            asset_icon("list-music"),
            asset_icon("bot"),
            asset_icon("folder"),
            asset_icon("external_device"),
            w,
        )
        w.sideList.setItemDelegate(SideListDelegate(w.sideList))
        self.side_expanded: set[str] = set(self.load_json_setting("sideListExpanded"))
        self.pinned_folders: list[str] = self.load_json_setting("pinnedFolders")
        w.sideList.setModel(self.side_model)
        self.playlist_selection = w.sideList.selectionModel()
        self.playlist_selection.currentChanged.connect(self.on_playlist_selected)
        w.sideList.clicked.connect(self.on_side_list_clicked)
        w.sideList.doubleClicked.connect(self.on_side_list_double_clicked)
        w.sideList.expanded.connect(lambda index: self.on_side_list_toggled(index, True))
        w.sideList.collapsed.connect(lambda index: self.on_side_list_toggled(index, False))
        w.sideList.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        w.sideList.customContextMenuRequested.connect(self.show_side_menu)
        # Dropping on a row always means "add to this playlist", never "insert between".
        w.sideList.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        w.sideList.setDragDropOverwriteMode(True)

        self.playlist_tracks = PlaylistTrackModel(self.library_model, w)
        self.playlist_proxy = PlaylistSortedModel(w)
        self.playlist_proxy.setSourceModel(self.playlist_tracks)
        setup_library_table(w.playlistSongList, self.playlist_proxy)
        self.restore_table_layout(w.playlistSongList, "playlistHeaderState")
        w.playlistSongList.doubleClicked.connect(self.play_playlist_item)
        w.playlistSongList.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        w.playlistSongList.setDragDropOverwriteMode(False)

        # Next to each song table: pick a tag, then one of its values, to show
        # only those songs. Each playlist remembers its own choice, and so does each tab.
        self.playlist_filter = self.make_filter_bar(
            w.thisComboBox, w.thisList, w.playlistFilterButton, "playlistFilterVisible"
        )
        self.playlist_filter.chosen.connect(self.save_playlist_filter)
        self.song_filter = self.make_filter_bar(
            w.songFilterCombo, w.songFilterList, w.songFilterButton, "songFilterVisible"
        )
        self.song_filter.chosen.connect(self.on_song_filter_chosen)

        # The search bar of the Songs tab, which waits for a pause in the typing
        self.song_search_index: list[str] | None = None  # one text per library row
        self.song_search_timer = QTimer(w)
        self.song_search_timer.setSingleShot(True)
        self.song_search_timer.setInterval(SONG_SEARCH_DELAY_MS)
        self.song_search_timer.timeout.connect(self.show_song_filter)
        w.lineEdit.textChanged.connect(lambda _text: self.song_search_timer.start())
        w.lineEdit.returnPressed.connect(self.search_songs_now)
        clear_action = w.lineEdit.findChild(QAction, "_q_qlineeditclearaction")  # Qt's own
        if clear_action is not None:
            clear_action.setIcon(small_asset_icon("x", SEARCH_CLEAR_ICON_SCALE))
        w.pushButton.clicked.connect(lambda _checked=False: self.search_songs_now())

        # The Artists and Albums tabs: the filter bar picks an artist or album.
        self.artist_info = ArtistInfoService(LASTFM_API_KEY, library_db.data_dir() / "artists", w)
        self.artist_info.artist_updated.connect(self.on_artist_updated)
        self.artist_description = ElidedLabel(w.artistsTabDescription, max_lines=4)
        self.group_pages = {
            "artist": self.make_group_page(
                "artist",
                w.artistSongList,
                "artistHeaderState",
                self.make_filter_bar(
                    w.artistFilterCombo, w.artistFilterList, w.artistFilterButton, "artistFilterVisible"
                ),
            ),
            "album": self.make_group_page(
                "album",
                w.albumSongList,
                "albumHeaderState",
                self.make_filter_bar(
                    w.albumFilterCombo, w.albumFilterList, w.albumFilterButton, "albumFilterVisible"
                ),
            ),
        }
        # The History tab: songs in the order they were last played
        self.history_tracks = HistoryTrackModel(self.library_model, w)
        self.history_proxy = SortedTrackModel(w)
        self.history_proxy.setSourceModel(self.history_tracks)
        setup_library_table(w.historyList, self.history_proxy)
        self.restore_table_layout(w.historyList, "historyHeaderState")
        w.historyList.doubleClicked.connect(self.play_history_item)

        self.library_model.modelReset.connect(self.refresh_filter_bars)

        for button in (
            w.songColumnsButton,
            w.playlistColumnsButton,
            w.artistColumnsButton,
            w.albumColumnsButton,
        ):
            button.clicked.connect(lambda _checked=False: self.edit_table_columns())

        # Queued, so the views finish handling the drop before their models change.
        queued = Qt.ConnectionType.QueuedConnection
        self.side_model.songs_dropped.connect(self.add_to_playlist, queued)
        self.playlist_proxy.songs_added.connect(self.add_to_shown_playlist, queued)
        self.playlist_proxy.songs_rearranged.connect(self.rearrange_playlist, queued)
        self.side_model.drop_refused.connect(self.show_status)
        self.playlist_proxy.drop_refused.connect(self.show_status)

        # Holding a dragged song over a tab opens that tab.
        tab_bar = w.podcastsTab.tabBar()
        self._tab_style = CenteredTabIconStyle()
        tab_bar.setStyle(self._tab_style)
        tab_bar.setChangeCurrentOnDrag(True)
        tab_bar.setAcceptDrops(True)

        QShortcut(QKeySequence(QKeySequence.StandardKey.SelectAll), w).activated.connect(
            self.select_all_in_table
        )
        w.playlistLockButton.clicked.connect(self.on_playlist_lock_clicked)
        w.playlistEditButton.setToolTip("Edit this playlist's title, image and description")
        w.playlistEditButton.clicked.connect(lambda _checked=False: self.edit_playlist())
        self.playlist_description = ElidedLabel(w.playlistsTabDescription)
        self.tab_icon_animation: IconAnimation | None = None

        # Track info panel (filled whenever a track starts playing)
        self.cover_view = SquareCover(w.trackInfoCoverImg, asset_icon("eighth-notes"))
        self.full_cover: QImage | None = None  # the playing track's cover, at full size
        self.cover_window = None  # created the first time a cover is opened
        self._cover_click = ClickFilter(w.trackInfoCoverImg)
        self._cover_click.clicked.connect(self.show_full_cover)
        self.visualizer = Visualizer(w.nowPlayingVisualizer)
        self.player.audio_buffer_ready.connect(self.visualizer.add_buffer)
        self.lyrics_view = LyricsView(w.nowPlayingLyrics)
        self.rating_stars = StarRating(
            w.trackRatingStars, asset_icon("star"), asset_icon("star_full")
        )
        self.rating_stars.rated.connect(self.on_track_rated)
        self.online_search = OnlineSearch(LASTFM_API_KEY, w)
        self.online_search.auto_tag_progress.connect(self.on_auto_tag_progress)
        self.online_search.auto_tag_finished.connect(self.on_auto_tag_finished)
        self.auto_tag_library: LibraryInfo | None = None  # set while auto-tagging runs
        self.auto_tag_text = w.actionAutoTagGenres.text()
        self.tags_saved_note = ""  # added to the next "saved" message
        w.actionAutoTagGenres.triggered.connect(lambda _checked=False: self.on_auto_tag_clicked())
        self.organise_error = ""  # why the library could not follow the moved files
        w.actionOrganiseFiles.triggered.connect(lambda _checked=False: self.on_organise_clicked())
        self.level_tool: LevelVolumeTool | None = None  # made when first opened
        w.actionLevelVolume.triggered.connect(lambda _checked=False: self.show_level_volume())
        self.convert_tool: ConvertFilesTool | None = None  # made when first opened
        w.actionConvertFiles.triggered.connect(lambda _checked=False: self.show_convert_files())
        self.tag_writer = TagWriter(w)
        self.tag_writer.rating_failed.connect(self.on_rating_write_failed)
        self.tag_writer.tags_saved.connect(self.on_tags_saved)
        for label in (
            w.trackInfoTitle,
            w.trackInfoArtistAlbumArtist,
            w.trackInfoAlbumYear,
            w.trackInfoFile,
        ):
            label.setWordWrap(True)
        for label in (w.trackInfoTitle, w.trackInfoArtistAlbumArtist, w.trackInfoAlbumYear):
            label.linkActivated.connect(self.on_track_info_link)

        # Shuffle and repeat buttons (settings are remembered between sessions)
        w.shuffleButton.setCheckable(True)
        w.repeatButton.setCheckable(True)

        shuffle_on = self.settings.value("shuffle", False, type=bool)
        w.shuffleButton.setChecked(shuffle_on)
        w.shuffleButton.setIcon(self.shuffle_icons[shuffle_on])
        self.play_order.set_shuffle(shuffle_on)
        w.shuffleButton.toggled.connect(self.on_shuffle_toggled)

        self.play_order.set_repeat(self.settings.value("repeat", "off", type=str))
        self.update_repeat_button()
        w.repeatButton.clicked.connect(self.cycle_repeat)

        # Library selector and buttons
        w.libraryCombo.setPlaceholderText("No library yet")
        w.libraryCombo.activated.connect(self.on_library_chosen)
        w.addLibraryButton.clicked.connect(self.add_library)
        w.rescanButton.clicked.connect(self.rescan)

        # Menu actions (created in Qt Designer)
        w.actionAddLibrary.triggered.connect(self.add_library)
        w.actionRescanLibrary.triggered.connect(self.rescan)
        w.actionRemoveLibrary.triggered.connect(self.remove_library)
        w.actionImportPlaylist.triggered.connect(self.import_playlist)
        w.actionExportPlaylist.triggered.connect(lambda: self.export_playlist())
        w.actionQuit.triggered.connect(lambda _checked=False: self.quit_app())
        self.settings_window = None  # created the first time it is opened
        w.actionSettings.triggered.connect(lambda _checked=False: self.show_settings())
        self.setup_tray()
        w.actionPlayPause.triggered.connect(self.on_play_pause)
        w.actionPrevious.triggered.connect(self.play_previous)
        w.actionNext.triggered.connect(self.play_next)
        w.actionAbout.triggered.connect(self.show_about)

        # Transport buttons
        w.playButton.clicked.connect(self.on_play_pause)
        w.previousButton.clicked.connect(self.play_previous)
        w.nextButton.clicked.connect(self.play_next)

        # Seek slider (user interaction)
        w.progressSlider.sliderMoved.connect(self.on_slider_moved)
        w.progressSlider.sliderReleased.connect(self.on_slider_released)
        w.progressSlider.actionTriggered.connect(self.on_slider_action)

        # Volume slider and mute button (the volume is remembered between sessions)
        w.muteButton.setCheckable(True)
        saved_volume = self.settings.value("volume", w.volumeSlider.value(), type=int)
        w.volumeSlider.setValue(saved_volume)
        self.player.set_volume(w.volumeSlider.value())
        self.player.set_equalizer(load_equaliser(self.settings))
        self.player.set_sound_settings(load_sound_settings(self.settings))
        self.equaliser_window: EqualiserWindow | None = None  # made when first opened
        w.equalizerButton.clicked.connect(lambda _checked=False: self.show_equaliser())
        self.update_volume_ui()
        w.volumeSlider.valueChanged.connect(self.on_volume_changed)
        w.muteButton.toggled.connect(self.on_mute_toggled)
        self._volume_wheel = VolumeWheelFilter(w.volumeSlider)
        w.volumeSlider.installEventFilter(self._volume_wheel)
        w.muteButton.installEventFilter(self._volume_wheel)

        # Player -> UI
        self.player.track_started.connect(self.on_track_started)
        self.player.track_finished.connect(self.on_track_finished)
        self.player.track_ending.connect(self.on_track_ending)
        self.player.state_changed.connect(self.on_state_changed)
        self.player.position_changed.connect(self.on_position_changed)
        self.player.duration_changed.connect(self.on_duration_changed)
        self.player.error_occurred.connect(self.on_player_error)

        # Scanner -> UI
        self.scan_signals.progress.connect(self.on_scan_progress)
        self.scan_signals.finished.connect(self.on_scan_finished)
        self.scan_signals.failed.connect(self.on_scan_failed)
        self.scan_signals.cancelled.connect(self.on_scan_cancelled)

        # Library totals, at the right end of the status bar
        self.stats_label = QLabel()
        self.stats_label.setContentsMargins(0, 0, 8, 0)
        w.statusBar().addPermanentWidget(self.stats_label)

        # The playlist icon at the top of the tab can be clicked to change it
        icon_widget = w.playlistsTabIcon
        icon_widget.setToolTip("Click to choose an icon for this playlist")
        if hasattr(icon_widget, "clicked"):  # a button
            icon_widget.clicked.connect(self.choose_playlist_icon)
        else:  # a label
            self._icon_click = ClickFilter(icon_widget)
            self._icon_click.clicked.connect(self.choose_playlist_icon)
        self.device_sync = DeviceSyncPanel(
            w, self.settings, self.device_sync_source, self.show_status
        )
        self.refresh_playlist_list()
        self.refresh_filter_bars()
        self.devices: list[tuple[str, str]] = []
        self.device_ejector = DeviceEjector(w)
        self.device_ejector.finished.connect(self.on_device_ejected)
        self.devices_tab = DevicesTab(w, self.device_sync.set_device)
        self.refresh_devices()
        self.device_timer = QTimer(w)
        self.device_timer.setInterval(DEVICE_CHECK_MS)
        self.device_timer.timeout.connect(self.refresh_devices)
        self.device_timer.start()

        self.setup_compact_tabs()
        self.on_state_changed("stopped")
        self.clear_track_info()
        self.restore_libraries()
        self.restore_open_tab()
        self.update_mpris()
        self.mpris.start()

    def show(self) -> None:
        self.window.show()

    @staticmethod
    def quit_app() -> None:
        """End the program. Closing the window may only hide it to the tray."""
        QApplication.quit()

    def show_settings(self) -> None:
        if self.settings_window is None:
            self.settings_window = d = load_dialog(SETTINGS_UI_FILE, self.window)
            self.sound_settings_tab = SoundSettingsTab(
                d, self.settings, self.player.set_sound_settings
            )
            d.settingsButtonBox.rejected.connect(d.reject)
            # Enter confirms a typed number; it must not press Close as well.
            d.settingsButtonBox.button(QDialogButtonBox.StandardButton.Close).setAutoDefault(False)
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def show_equaliser(self) -> None:
        if self.equaliser_window is None:
            self.equaliser_window = EqualiserWindow(
                EQUALISER_UI_FILE, self.window, self.settings, self.player.set_equalizer
            )
        self.equaliser_window.show()

    def setup_tray(self) -> None:
        """Create the tray icon and connect the checkbox in the View menu."""
        w = self.window
        menu = QMenu(w)
        for label, icon, callback in (
            ("Show MusicHive", "library", self.raise_window),
            ("Play/Pause", "play", self.on_play_pause),
            ("Next", "skip-forward", self.play_next),
            ("Previous", "skip-back", self.play_previous),
            ("Quit", "door-open", self.quit_app),
        ):
            action = menu.addAction(asset_icon(icon), label)
            action.triggered.connect(lambda _checked=False, c=callback: c())
        folder = Path(__file__).parent
        self.tray = TrayMinimizer(
            w,
            QIcon(str(folder / "musichive_dark.svg")),
            QIcon(str(folder / "musichive_light.svg")),
            menu,
            self.raise_window,
        )

        action = w.actionMinimizeToTray
        if not TrayMinimizer.is_available():
            action.setEnabled(False)
            action.setToolTip("Your desktop has no system tray")
        action.setChecked(self.settings.value("minimizeToTray", False, type=bool))
        action.toggled.connect(self.on_minimize_to_tray_toggled)
        self.tray.set_enabled(action.isChecked())

    def on_minimize_to_tray_toggled(self, enabled: bool) -> None:
        self.settings.setValue("minimizeToTray", enabled)
        self.tray.set_enabled(enabled)

    def setup_dock_toggles(self) -> None:
        """Add a View menu item for each dock, and remember which ones are shown."""
        w = self.window
        for dock in (w.upcomingDock, w.libraryDock, w.controlsWidget):
            dock.setTitleBarWidget(QWidget(dock))  # an empty one: no title bar
            action = dock.toggleViewAction()
            w.menuView.addAction(action)
            key = f"dockVisible/{dock.objectName()}"
            action.setChecked(self.settings.value(key, True, type=bool))
            action.toggled.connect(
                lambda visible, key=key: self.settings.setValue(key, visible)
            )

    def setup_compact_tabs(self) -> None:
        """Tabs show only their icon, and their name (from player.ui) once selected."""
        tabs = self.window.podcastsTab
        self.tab_titles = [tabs.tabText(index) for index in range(tabs.count())]
        for index, title in enumerate(self.tab_titles):
            tabs.setTabToolTip(index, title)
        tabs.currentChanged.connect(self.show_selected_tab_title)
        self.show_selected_tab_title(tabs.currentIndex())

    def show_selected_tab_title(self, selected: int) -> None:
        tabs = self.window.podcastsTab
        for index, title in enumerate(self.tab_titles):
            tabs.setTabText(index, title if index == selected else "")

    def restore_open_tab(self) -> None:
        """Open the tab that was open when the app last closed."""
        tabs = self.window.podcastsTab
        saved = self.settings.value("openTab", "", type=str)
        for index in range(tabs.count()):
            if tabs.widget(index).objectName() == saved:
                tabs.setCurrentIndex(index)
                return

    def shutdown(self) -> None:
        """Save the session, stop background work and playback. Called on quit."""
        self.check_listening()  # a play that has just reached its threshold still counts
        if self.scan_task is not None:
            self.scan_task.cancel()
        self.scan_pool.waitForDone()  # a scan must be finished before we save
        self.save_table_layouts()
        self.settings.setValue("openTab", self.window.podcastsTab.currentWidget().objectName())
        self.save_session()
        self.artist_info.shutdown()
        self.online_search.shutdown()
        if self.level_tool is not None:
            self.level_tool.shutdown()
        if self.convert_tool is not None:
            self.convert_tool.shutdown()
        self.device_sync.shutdown()
        self.tag_writer.shutdown()
        self.mpris.shutdown()
        self.discord.shutdown()
        self.discord_covers.shutdown()
        self.lastfm.shutdown()
        self.player.shutdown()

    def show_status(self, text: str) -> None:
        self.window.statusBar().showMessage(text)

    def show_about(self) -> None:
        QMessageBox.about(
            self.window,
            "About MusicHive",
            "<b>MusicHive</b><br>A simple local music player built with "
            "PySide6 and Qt Multimedia.",
        )

    # ----- volume and window title -----

    def on_volume_changed(self, value: int) -> None:
        """The volume slider moved (dragged, clicked, scrolled or by keyboard)."""
        w = self.window
        self.player.set_volume(value)
        self.settings.setValue("volume", value)
        if w.muteButton.isChecked():
            w.muteButton.setChecked(False)  # changing the volume unmutes
        self.update_volume_ui()
        self.update_mpris()
        if w.volumeSlider.isSliderDown():
            # Show the value next to the cursor while dragging.
            QToolTip.showText(QCursor.pos(), f"{value}%", w.volumeSlider)

    def on_mute_toggled(self, muted: bool) -> None:
        self.player.set_muted(muted)
        self.update_volume_ui()
        self.update_mpris()

    def update_volume_ui(self) -> None:
        """Match the mute button's icon and both tooltips to the volume."""
        w = self.window
        volume = w.volumeSlider.value()
        muted = w.muteButton.isChecked()

        w.volumeSlider.setToolTip(f"Volume: {volume}%")
        if muted:
            key = "muted"
            w.muteButton.setToolTip(f"Muted ({volume}%) – click to unmute")
        else:
            key = "silent" if volume == 0 else "low" if volume <= 50 else "high"
            w.muteButton.setToolTip(f"Volume: {volume}% – click to mute")
        w.muteButton.setIcon(self.volume_icons[key])

    # ----- MPRIS (desktop widgets) -----

    def mpris_metadata(self) -> dict:
        """What MPRIS shows about the current track."""
        row = self.current_index
        info = self.library_model.info(row)
        if info is None or not 0 <= row < len(self.tracks):
            return {"mpris:trackid": ("o", NO_TRACK)}
        metadata = {
            "mpris:trackid": ("o", f"/org/musichive/track/{row}"),
            "xesam:title": ("s", info.title),
            "xesam:url": ("s", file_url(self.tracks[row])),
        }
        duration_ms = self.duration_ms or info.duration_ms or 0
        if duration_ms:
            metadata["mpris:length"] = ("x", duration_ms * 1000)
        if self.mpris_art_url:
            metadata["mpris:artUrl"] = ("s", self.mpris_art_url)
        if info.artist:
            metadata["xesam:artist"] = ("as", [info.artist])
        if info.album_artist:
            metadata["xesam:albumArtist"] = ("as", [info.album_artist])
        if info.album:
            metadata["xesam:album"] = ("s", info.album)
        if info.genre:
            metadata["xesam:genre"] = ("as", [info.genre])
        if info.track_number:
            metadata["xesam:trackNumber"] = ("i", info.track_number)
        if info.disc_number:
            metadata["xesam:discNumber"] = ("i", info.disc_number)
        return metadata

    def update_mpris(self) -> None:
        """Tell the desktop the player's current state."""
        w = self.window
        metadata = self.mpris_metadata()
        self.mpris.set_properties(
            PlaybackStatus=MPRIS_STATUS[self.playback_state],
            LoopStatus=MPRIS_LOOP_STATUS[self.play_order.repeat],
            Shuffle=w.shuffleButton.isChecked(),
            Volume=0.0 if w.muteButton.isChecked() else w.volumeSlider.value() / 100,
            Metadata=metadata,
            CanSeek=metadata["mpris:trackid"][1] != NO_TRACK,
        )

    def raise_window(self) -> None:
        w = self.window
        w.setWindowState(w.windowState() & ~Qt.WindowState.WindowMinimized)
        w.show()
        w.raise_()
        w.activateWindow()

    def on_mpris_play(self) -> None:
        if self.playback_state != "playing":
            self.on_play_pause()

    def on_mpris_pause(self) -> None:
        if self.playback_state == "playing":
            self.player.pause()

    def on_mpris_seek(self, position_ms: int) -> None:
        self.player.seek(position_ms)
        self.discord.request_update()

    def on_mpris_loop(self, status: str) -> None:
        for mode, name in MPRIS_LOOP_STATUS.items():
            if name == status:
                self.set_repeat_mode(mode)

    def on_mpris_volume(self, volume: float) -> None:
        self.window.volumeSlider.setValue(round(volume * 100))

    def update_window_title(self, title: str = "", artist: str = "") -> None:
        """The window title: "MusicHive", or "MusicHive – Title – Artist" while
        a track is loaded."""
        parts = ["MusicHive"] + [part for part in (title, artist) if part]
        self.window.setWindowTitle(" – ".join(parts))

    # ----- the library table -----

    def song_tables(self) -> list[tuple[QAbstractItemView, str]]:
        """Every song table, with the setting that holds its layout."""
        w = self.window
        pages = [(page.view, page.header_key) for page in self.group_pages.values()]
        return [
            (w.libraryList, "libraryHeaderState"),
            (w.playlistSongList, "playlistHeaderState"),
            (w.historyList, "historyHeaderState"),
            *pages,
        ]

    def save_table_layouts(self) -> None:
        for view, key in self.song_tables():
            self.settings.setValue(key, view.horizontalHeader().saveState())

    def edit_table_columns(self) -> None:
        """Open the column window and apply what the user chooses."""
        library = self.loaded_library
        try:
            names = library_db.all_tag_names(library.db_path) if library else []
        except sqlite3.Error:
            names = []
        shown = self.library_model.columns
        shown_keys = {column.key for column in shown}
        available = [column for column in all_columns(names) if column.key not in shown_keys]
        keys = ColumnsDialog(COLUMNS_EDIT_UI_FILE, self.window, shown, available).run()
        if keys is not None and keys != [column.key for column in shown]:
            self.apply_table_columns(keys)

    def apply_table_columns(self, keys: list[str]) -> None:
        """Show these columns in every song table, keeping widths and sorting."""
        before = []
        for view, _key in self.song_tables():
            header = view.horizontalHeader()
            old_columns = view.model().columns
            widths = {
                column.key: view.columnWidth(index) for index, column in enumerate(old_columns)
            }
            section = header.sortIndicatorSection()
            sort_key = old_columns[section].key if 0 <= section < len(old_columns) else None
            before.append((view, widths, sort_key, header.sortIndicatorOrder()))

        columns = columns_from_keys(keys)
        self.load_column_tags(columns)
        self.library_model.set_columns(columns)

        for view, widths, sort_key, order in before:
            view_columns = view.model().columns  # History adds a column of its own
            view_keys = [column.key for column in view_columns]
            for index, column in enumerate(view_columns):
                view.setColumnWidth(index, widths.get(column.key, column.width))
            view.sortByColumn(view_keys.index(sort_key) if sort_key in view_keys else -1, order)

        self.settings.setValue("tableColumns", json.dumps([column.key for column in columns]))
        self.save_table_layouts()
        if self.current_index >= 0:
            self.select_in_table(self.current_index)

    def shown_tag_names(self) -> list[str]:
        """The tags that have a column in the song tables."""
        return [column.tag for column in self.library_model.columns if column.tag]

    def load_column_tags(self, columns) -> None:
        """Read the values of these columns' tags into the loaded tracks."""
        library = self.loaded_library
        if library is None:
            return
        names = [column.tag for column in columns if column.tag]
        try:
            values = library_db.load_tag_values(library.db_path, names)
        except sqlite3.Error:
            values = {}
        for row, path in enumerate(self.tracks):
            self.library_model.info(row).tags = values.get(self.relative_path(path), {})

    def restore_table_layout(self, view=None, key: str = "libraryHeaderState") -> None:
        """Bring back a table's column widths and sort order from the last session."""
        if view is None:
            view = self.window.libraryList
        header = view.horizontalHeader()
        minimum_width = header.minimumSectionSize()  # as set in player.ui
        state = self.settings.value(key)
        if state:
            header.restoreState(state)
            header.setMinimumSectionSize(minimum_width)  # a saved layout has its own
        # Layouts saved by older versions can bring back a column that stretches.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

    def display_rows(self) -> list[int]:
        """Library rows in the order the table currently shows them."""
        return self.library_proxy.source_rows()

    def table_index(self, row: int) -> QModelIndex:
        """The table's index for library row `row` (it accounts for sorting)."""
        return self.library_proxy.mapFromSource(self.library_model.index(row, 0))

    def select_in_table(self, row: int, at_top: bool = False) -> None:
        """Highlight library row `row` in the table and scroll it into view.

        `at_top` means the user asked to go there; otherwise (a new track
        started, say) a selection of several songs is left alone.
        """
        index = self.table_index(row)
        view = self.window.libraryList
        if not index.isValid() or (not at_top and self.has_multi_selection(view)):
            return
        view.setCurrentIndex(index)
        hint = (
            QAbstractItemView.ScrollHint.PositionAtTop
            if at_top
            else QAbstractItemView.ScrollHint.EnsureVisible
        )
        view.scrollTo(index, hint)

    @staticmethod
    def has_multi_selection(view) -> bool:
        return len(view.selectionModel().selectedRows()) > 1

    def selected_library_rows(self, source: str) -> list[int]:
        """Library rows selected in a table ("library" or "playlist"), as shown."""
        w = self.window
        if source == "upcoming":
            model = self.queue_model
            return [
                model.library_row(model.index(offset, 0))
                for offset in self.selected_upcoming_offsets()
            ]
        if source == "library":
            view, shown = w.libraryList, self.display_rows()
        elif source in self.group_pages:
            page = self.group_pages[source]
            view, shown = page.view, self.group_display_rows(page)
        elif source == HISTORY_SOURCE:
            view, shown = w.historyList, self.history_display_rows()
        else:
            view, shown = w.playlistSongList, self.playlist_display_rows()
        positions = sorted(index.row() for index in view.selectionModel().selectedRows())
        return [shown[position] for position in positions]

    def select_all_in_table(self) -> None:
        """Ctrl+A: select every song in the table on screen."""
        w = self.window
        group_views = [page.view for page in self.group_pages.values()]
        for view in (w.libraryList, w.playlistSongList, w.historyList, *group_views):
            if view.isVisible():
                view.setFocus()
                view.selectAll()

    # ----- play counts and Last.fm -----

    def begin_listening(self) -> None:
        """A track was loaded: start measuring how long it is listened to."""
        self.check_listening()  # settle the previous play first
        library = self.loaded_library
        info = self.library_model.info(self.current_index)
        if library is None or info is None or not 0 <= self.current_index < len(self.tracks):
            self.listening.reset()
            self.listen_entry = None
            return
        path = self.tracks[self.current_index]
        self.listen_entry = {
            "db_path": library.db_path,
            "path": path,
            "rel_path": self.relative_path(path),
            "artist": info.artist,
            "title": info.title,
            "album": info.album,
            "album_artist": info.album_artist,
            "duration": (info.duration_ms or 0) // 1000,
        }
        self.listening.begin(info.duration_ms or 0, self.playback_state == "playing")

    def check_listening(self) -> None:
        """Count the current play if it has been listened to for long enough."""
        if self.listening.check() and self.listen_entry is not None:
            self.count_play(self.listen_entry, self.listening.started_at)

    def count_play(self, entry: dict, started_at: int | None) -> None:
        """A play counts: raise the play count and scrobble it to Last.fm."""
        played_at = time.time()
        try:
            count = library_db.increment_play_count(entry["db_path"], entry["rel_path"], played_at)
        except sqlite3.Error:
            count = None

        library = self.loaded_library
        if count is not None and library is not None and library.db_path == entry["db_path"]:
            try:
                row = self.tracks.index(entry["path"])
            except ValueError:
                row = -1
            if row >= 0:
                self.library_model.set_play_count(row, count)
                self.library_model.set_last_played(row, played_at)
                self.refresh_history()

        if entry["artist"] and entry["title"]:  # Last.fm needs both
            self.lastfm.enqueue_scrobble(
                {
                    "artist": entry["artist"],
                    "title": entry["title"],
                    "album": entry["album"],
                    "album_artist": entry["album_artist"],
                    "duration": entry["duration"],
                    "timestamp": started_at or int(time.time()),
                }
            )

    def send_now_playing(self) -> None:
        if self.listen_entry is not None and self.playback_state == "playing":
            if self.listen_entry["artist"] and self.listen_entry["title"]:
                self.lastfm.now_playing(self.listen_entry)

    def show_lastfm_dialog(self) -> None:
        if self._lastfm_dialog is None:
            self._lastfm_dialog = LastFmDialog(
                LASTFM_UI_FILE, self.lastfm, self.window, self.apply_lastfm_counts
            )
        self._lastfm_dialog.open_dialog()

    def apply_lastfm_counts(self, counts: dict) -> tuple[int, int]:
        """Put Last.fm's play counts into every library. Returns (songs, libraries)."""
        updated = 0
        libraries = 0
        for library in library_db.list_libraries():
            try:
                changed = library_db.apply_lastfm_counts(library.db_path, counts)
            except sqlite3.Error:
                continue
            if changed:
                updated += changed
                libraries += 1
        self.reload_play_counts()
        return updated, libraries

    def reload_play_counts(self) -> None:
        """Refresh the Plays column from the database, without touching playback."""
        library = self.loaded_library
        if library is None:
            return
        try:
            stored = library_db.load_play_counts(library.db_path)
        except sqlite3.Error:
            return
        self.library_model.set_all_play_counts(
            [stored.get(self.relative_path(path), 0) for path in self.tracks]
        )


    # ----- Discord -----

    def build_discord_activity(self) -> dict | None:
        """What Discord should show right now, or None to show nothing."""
        if not self.discord_active or self.current_index < 0:
            return None
        info = self.library_model.info(self.current_index)
        if info is None:
            return None
        cover_url = ""
        if self.discord.is_ready:  # nothing is uploaded while Discord isn't running
            cover_url = self.discord_covers.link_for(
                self.tracks[self.current_index], info.cover_key
            )
            self.preupload_discord_covers()
        return make_activity(
            title=info.title,
            artist=info.artist,
            album=info.album_line,
            audio_format=self.window.trackInfoFile.text(),
            position_ms=self.position_ms,
            duration_ms=self.duration_ms or info.duration_ms or 0,
            paused=self.playback_state == "paused",
            cover_url=cover_url,
        )

    def preupload_discord_covers(self) -> None:
        """Upload the next tracks' covers, so they are ready when those tracks start."""
        for row in self.play_order.upcoming_rows()[1 : 1 + DISCORD_PREUPLOAD_COUNT]:
            info = self.library_model.info(row)
            if info is not None:
                self.discord_covers.link_for(self.tracks[row], info.cover_key)

    def on_discord_cover_uploaded(self, cover_key: str) -> None:
        info = self.library_model.info(self.current_index)
        if info is not None and info.cover_key == cover_key:
            self.discord.request_update()

    def on_discord_toggled(self, enabled: bool) -> None:
        self.settings.setValue("discordPresence", enabled)
        self.discord.set_enabled(enabled)
        self.show_status("Discord Rich Presence on" if enabled else "Discord Rich Presence off")


    # ----- track info panel -----

    def update_track_info(self, track: Path) -> None:
        """Fill the track info panel from the file that just started playing."""
        details = read_track_details(track)
        w = self.window
        w.currentTrackLabel.setText(details.title)
        w.trackInfoTitle.ensurePolished()  # its text colour is needed for the links
        link = lambda text, field: self.track_info_link(text, field)
        w.trackInfoTitle.setText(link(details.title, "title"))
        artist, album_artist = link(details.artist, "artist"), link(details.album_artist, "album_artist")
        w.trackInfoArtistAlbumArtist.setText(
            f"{artist} ({album_artist})" if artist and album_artist else artist or album_artist
        )
        album, year = link(details.album, "album"), link(details.year, "year")
        w.trackInfoAlbumYear.setText(
            f"{album} ({year})" if album and year else album or (f"({year})" if year else "")
        )
        w.trackInfoFile.setText(details.audio_format)
        self.update_window_title(details.title, details.artist)
        cover = QImage.fromData(details.cover) if details.cover else None
        self.full_cover = cover if cover is not None and not cover.isNull() else None
        self.mpris_art_url = cover_file_url(details.cover) if details.cover else ""
        self.lyrics_view.set_lyrics(details.lyrics)
        self.lyrics_view.set_position(self.position_ms)
        self.adopt_file_rating(details.rating)
        self.show_current_rating()
        self.cover_view.set_image(cover)

    def adopt_file_rating(self, rating: int) -> None:
        """Use the rating found in the file that started, if another program changed it."""
        library = self.loaded_library
        info = self.library_model.info(self.current_index)
        if library is None or info is None or not rating or rating == info.rating:
            return
        self.library_model.set_rating(self.current_index, rating)
        try:
            path = self.relative_path(self.tracks[self.current_index])
            library_db.set_rating(library.db_path, path, rating)
        except sqlite3.Error:
            pass  # the next rescan reads it from the file again
        self.update_rating_playlists()

    def update_rating_playlists(self) -> None:
        """A rating changed: auto-playlists with a rating rule must look again."""
        for key in [key for key in self.auto_playlist_cache if '"tag": "rating"' in key]:
            del self.auto_playlist_cache[key]
        store = self.playlist_store
        if store is None:
            return
        for name in {self.current_playlist, self.play_source} - {None}:
            playlist = store.load(name)
            if playlist.is_auto and any(rule["tag"] == "rating" for rule in playlist.rules):
                self.playlist_edited(name)

    def show_current_rating(self) -> None:
        info = self.library_model.info(self.current_index)
        self.rating_stars.set_rating(info.rating if info else 0, enabled=info is not None)

    def on_track_rated(self, rating: int) -> None:
        """The user clicked a star: save the rating of the playing track."""
        library = self.loaded_library
        info = self.library_model.info(self.current_index)
        if library is None or info is None:
            return
        path = self.relative_path(self.tracks[self.current_index])
        try:
            library_db.set_rating(library.db_path, path, rating)
        except sqlite3.Error as error:
            self.show_status(f"Cannot save the rating: {error}")
            return
        self.library_model.set_rating(self.current_index, rating)
        self.show_current_rating()
        self.tag_writer.write_rating(self.tracks[self.current_index], rating)
        self.update_rating_playlists()
        stars = f"{rating} star{'' if rating == 1 else 's'}"
        self.show_status(f"Rated '{info.title}' {stars}" if rating else f"Cleared the rating of '{info.title}'")

    def on_rating_write_failed(self, file_name: str, message: str) -> None:
        self.show_status(f"Could not write the rating to {file_name}: {message}")

    def show_full_cover(self) -> None:
        """Open the playing track's cover at its full resolution."""
        image = self.full_cover
        if image is None:
            return
        if self.cover_window is None:
            self.cover_window = load_dialog(COVER_WINDOW_UI_FILE, self.window)
        window = self.cover_window
        title = self.window.currentTrackLabel.text()
        window.setWindowTitle(f"{title} – {image.width()} × {image.height()}")

        # One pixel of the image on one pixel of the screen, also on HiDPI screens.
        ratio = window.devicePixelRatioF()
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(ratio)
        window.coverWindowImage.setPixmap(pixmap)

        # A cover bigger than the screen can be scrolled.
        screen = window.screen().availableGeometry()
        window.resize(
            min(round(image.width() / ratio), int(screen.width() * 0.9)),
            min(round(image.height() / ratio), int(screen.height() * 0.9)),
        )
        window.show()
        window.raise_()
        window.activateWindow()

    def track_info_link(self, text: str, field: str) -> str:
        """A link that looks like the label's normal text ("" if there is no text)."""
        if not text:
            return ""
        color = self.window.trackInfoTitle.palette().color(QPalette.ColorRole.WindowText).name()
        style = f"color: {color}; text-decoration: none"
        return f'<a href="{field}" style="{style}">{html.escape(text)}</a>'

    def on_track_info_link(self, field: str) -> None:
        """A label of the track info panel was clicked: go to what it names."""
        self.go_to(self.current_index, field)

    def clear_track_info(self) -> None:
        w = self.window
        for label in (
            w.trackInfoTitle,
            w.trackInfoArtistAlbumArtist,
            w.trackInfoAlbumYear,
            w.trackInfoFile,
        ):
            label.clear()
        self.full_cover = None
        self.mpris_art_url = ""
        self.lyrics_view.set_lyrics("")
        self.rating_stars.set_rating(0, enabled=False)
        self.cover_view.set_image(None)
        self.update_window_title()

    def show_nothing_playing(self) -> None:
        """Nothing is playing: reset the now-playing label, the info panel and Discord."""
        self.window.currentTrackLabel.setText("Nothing playing")
        self.clear_track_info()
        self.discord_active = False
        self.discord.request_update()
        self.update_mpris()

    # ----- play order, shuffle and repeat -----

    def refresh_upcoming(self, keep_scroll: bool = False) -> None:
        """Show the playing track and the ones after it in the Upcoming list.

        With `keep_scroll`, the list stays where it was scrolled to (used when
        the user edits the queue); otherwise it goes back to the top.
        """
        view = self.window.upcomingList
        scroll_bar = view.verticalScrollBar()
        scroll = scroll_bar.value()

        self.queue_model.set_rows(
            self.play_order.upcoming_rows(), self.play_order.stop_after_offset()
        )
        if self.queue_model.rowCount() > 0:
            # The playing track is always the first row of the Upcoming list.
            view.setCurrentIndex(self.queue_model.index(0, 0))

        if keep_scroll:
            QTimer.singleShot(0, lambda: scroll_bar.setValue(scroll))
        else:
            view.scrollToTop()

    def reset_play_order(self) -> None:
        """Nothing is playing: start a fresh play order for the current tracks."""
        self.current_index = -1
        self.play_order.reset(len(self.tracks))
        self.refresh_upcoming()

    def on_shuffle_toggled(self, enabled: bool) -> None:
        self.window.shuffleButton.setIcon(self.shuffle_icons[enabled])
        self.play_order.set_shuffle(enabled)
        self.settings.setValue("shuffle", enabled)
        self.refresh_upcoming()
        self.show_status("Shuffle on" if enabled else "Shuffle off")
        self.update_mpris()

    def cycle_repeat(self) -> None:
        """Repeat button: off -> all -> one -> off."""
        modes = PlayOrder.REPEAT_MODES
        self.set_repeat_mode(modes[(modes.index(self.play_order.repeat) + 1) % len(modes)])

    def set_repeat_mode(self, mode: str) -> None:
        self.play_order.set_repeat(mode)
        self.settings.setValue("repeat", mode)
        self.update_repeat_button()
        self.show_status(REPEAT_STATUS[mode])
        self.update_mpris()

    def update_repeat_button(self) -> None:
        """Make the repeat button show the current mode."""
        button = self.window.repeatButton
        mode = self.play_order.repeat
        button.setChecked(mode != "off")
        button.setIcon(self.repeat_icons[mode])
        button.setToolTip(REPEAT_STATUS[mode])

    # ----- right-click menu -----

    def show_track_menu(self, view, pos, source: str) -> None:
        """Right-click menu for a song. `source` is "library", "upcoming", "playlist",
        "artist", "album" or "history"."""
        index = view.indexAt(pos)
        if not index.isValid():
            return
        context = None  # the songs that follow when this one is played from here
        group = None  # the artist or album it is played from
        if source in self.group_pages:
            offset = -1
            page = self.group_pages[source]
            row = page.tracks.library_row(page.proxy.mapToSource(index).row())
            context = self.group_display_rows(page)
            group = source
        elif source == HISTORY_SOURCE:
            offset = -1
            row = self.history_tracks.library_row(self.history_proxy.mapToSource(index).row())
            context = self.history_display_rows()
            group = source
        elif source == "upcoming":
            offset = index.row()  # places down the Upcoming list (0 = playing)
            row = self.queue_model.library_row(index)
        elif source == "playlist":
            offset = -1
            playlist_row = self.playlist_proxy.mapToSource(index).row()
            row = self.playlist_tracks.library_row(playlist_row)
            context = self.playlist_display_rows()
        else:
            offset = -1
            row = self.library_proxy.mapToSource(index).row()
        info = self.library_model.info(row)
        if info is None or not 0 <= row < len(self.tracks):
            return

        # Actions apply to the whole selection if the click was inside it.
        selected = self.selected_library_rows(source)
        rows = selected if row in selected else [row]
        several = f" ({len(rows)} songs)" if len(rows) > 1 else ""

        scanning = self.scan_task is not None
        playlist_names = self.playlist_store.names() if self.playlist_store else []
        menu = QMenu(view)
        menu.setToolTipsVisible(True)

        def add(target, label, icon_key, callback, enabled=True, tip=""):
            action = target.addAction(self.menu_icons.get(icon_key, QIcon()), label)
            action.setEnabled(enabled)
            if tip:
                action.setToolTip(tip)
            action.triggered.connect(lambda _checked=False: callback())
            return action

        add(menu, "Play now", "play_now", lambda: self.play_now(row, offset, context, group))
        add(
            menu, f"Play next{several}", "play_next",
            lambda: self.queue_tracks(rows, play_next=True),
        )
        add(
            menu, f"Add to queue{several}", "add_to_queue",
            lambda: self.queue_tracks(rows, play_next=False),
        )

        playlist_menu = menu.addMenu(
            self.menu_icons["add_to_playlist"], f"Add to playlist{several}"
        )
        playlist_menu.setToolTipsVisible(True)
        for name in playlist_names:
            locked = self.side_model.is_locked(name)
            add(
                playlist_menu, name.replace("&", "&&"), None,
                lambda n=name: self.add_to_playlist(n, rows),
                enabled=not locked,
                tip="This playlist is locked" if locked else "",
            )
        if playlist_names:
            playlist_menu.addSeparator()
        add(
            playlist_menu, "New playlist…", None,
            lambda: self.new_playlist(add_rows=rows),
            enabled=self.playlist_store is not None,
        )
        menu.addSeparator()

        add(
            menu, f"Edit…{several}", "edit",
            lambda: self.edit_tags(rows, self.track_menu_neighbours(source, context)),
            enabled=not scanning,
            tip="Wait for the scan to finish" if scanning else "",
        )
        in_playlist = source == "playlist" and not self.is_auto_playlist(self.current_playlist)
        add(
            menu, f"Delete from playlist{several}", "delete_from_playlist",
            lambda: self.remove_from_playlist(rows),
            enabled=in_playlist,
            tip="" if in_playlist else "Only available inside a normal playlist",
        )
        add(
            menu, "Delete file", "delete_file", lambda: self.delete_file(row),
            enabled=not scanning and not several,
            tip=(
                "Wait for the scan to finish" if scanning
                else "Select a single song to delete its file" if several
                else ""
            ),
        )
        menu.addSeparator()

        go_menu = menu.addMenu(self.menu_icons["go_to"], "Go to")
        go_menu.setToolTipsVisible(True)
        for label, field in GO_TO_FIELDS:
            has_value = field == "title" or bool(getattr(info, field))
            add(
                go_menu, label, None, lambda f=field: self.go_to(row, f),
                enabled=has_value,
                tip="" if has_value else "This track has no such tag (rescan to read new ones)",
            )

        if source == "upcoming":
            menu.addSeparator()
            offsets = self.selected_upcoming_offsets()
            removable = [o for o in (offsets if offset in offsets else [offset]) if o > 0]
            count = f" ({len(removable)} songs)" if len(removable) > 1 else ""
            add(
                menu, f"Remove from upcoming{count}", "remove_from_upcoming",
                lambda: self.remove_from_upcoming(removable),
                enabled=bool(removable),
                tip="" if removable else "The playing track cannot be removed",
            )
            marked = self.play_order.stop_after_offset() == offset
            add(
                menu,
                "Don't end playback after track" if marked else "End playback after track",
                "end_after",
                lambda: self.toggle_stop_after(offset),
            )

        menu.exec(view.viewport().mapToGlobal(pos))

        if source == "upcoming" and self.queue_model.rowCount() > 0:
            # The highlight in the Upcoming list marks the playing track.
            view.setCurrentIndex(self.queue_model.index(0, 0))

    def play_now(
        self,
        row: int,
        upcoming_offset: int = -1,
        context: list[int] | None = None,
        group: str | None = None,
    ) -> None:
        """Play a song. From the Upcoming list (offset >= 0) the order is kept.
        Otherwise a fresh order starts at `row`, following `context` (the rows of
        a playlist, or of the Artists or Albums tab named by `group`) or, by default,
        the library table's current sort order."""
        if upcoming_offset >= 0:
            row = self.play_order.jump_to_upcoming(upcoming_offset)
            if row < 0:
                return
        else:
            from_playlist = context is not None and group is None
            self.set_play_source(self.current_playlist if from_playlist else None)
            self.source_group = group
            self.play_order.start_at(
                row, context if context is not None else self.display_rows()
            )
        self.play_index(row)

    def queue_tracks(self, rows: list[int], play_next: bool) -> None:
        """Play next / Add to queue, keeping the songs in the order given."""
        if self.current_index < 0:
            # Nothing is playing, so the first song starts and the rest queue behind it.
            self.play_order.start_at(rows[0], self.play_source_rows())
            self.play_index(rows[0])
            for row in rows[1:]:
                self.play_order.add_to_queue(row)
            self.refresh_upcoming(keep_scroll=True)
            return
        what = self.library_model.info(rows[0]).title if len(rows) == 1 else f"{len(rows)} songs"
        if play_next:
            for row in reversed(rows):  # each one goes right after the playing track
                self.play_order.play_next(row)
            self.show_status(f"Playing next: {what}")
        else:
            for row in rows:
                self.play_order.add_to_queue(row)
            self.show_status(f"Added to queue: {what}")
        self.refresh_upcoming(keep_scroll=True)

    def selected_upcoming_offsets(self) -> list[int]:
        """The selected rows of the Upcoming list, top to bottom."""
        selection = self.window.upcomingList.selectionModel()
        return sorted(index.row() for index in selection.selectedRows())

    def remove_from_upcoming(self, offsets: list[int]) -> None:
        # From the bottom up, so the offsets still to come stay correct.
        results = [self.play_order.remove_upcoming(o) for o in sorted(offsets, reverse=True)]
        if any(results):
            self.refresh_upcoming(keep_scroll=True)

    def toggle_stop_after(self, offset: int) -> None:
        enabled = self.play_order.toggle_stop_after(offset)
        self.refresh_upcoming(keep_scroll=True)
        if enabled:
            self.show_status("Playback will end after that track")
        else:
            self.show_status("Playback will no longer end after that track")

    def go_to(self, row: int, field: str) -> None:
        """Open the tab for a song's `field`, with that value picked in its filter bar.

        Artists and album artists open the Artists tab and albums the Albums tab;
        a year or genre filters the Songs tab, and a title selects the song there.
        """
        info = self.library_model.info(row)
        if info is None or not 0 <= row < len(self.tracks):
            return
        page = self.group_pages.get("album" if field == "album" else "artist")
        if field in ("artist", "album_artist", "album"):
            self.show_tab_of(page.view)
            self.on_group_filter_chosen(page, field, group_key(field, info, self.tracks[row].parent))
            self.highlight_row(page.tracks, page.proxy, page.view, row)
            self.show_status(f"Went to '{page.current.name}'")
            return

        library_view = self.window.libraryList
        self.show_tab_of(library_view)
        self.clear_song_search()  # it could be hiding the song
        if field == "title":
            if not self.table_index(row).isValid():  # the Songs filter hides this track
                self.on_song_filter_chosen(self.load_filter(SONGS_FILTER_OWNER)[0], "")
            self.show_status(f"Went to '{info.title}'")
        else:
            key = group_key(field, info, self.tracks[row].parent)
            self.on_song_filter_chosen(field, key)
            self.show_status(f"Went to '{self.song_filter.group(key).name}'")
        self.select_in_table(row, at_top=True)
        library_view.setFocus()

    def delete_file(self, row: int) -> None:
        """Move a track's file to the Trash (after asking) and drop it from the library."""
        library = self.loaded_library
        if library is None or not 0 <= row < len(self.tracks):
            return
        path = self.tracks[row]
        title = self.library_model.info(row).title

        answer = QMessageBox.question(
            self.window,
            "Delete file",
            f"Move this file to the Trash?\n\n{path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        if path.exists():
            result = QFile.moveToTrash(str(path))
            trashed = result[0] if isinstance(result, tuple) else bool(result)
            if not trashed:
                answer = QMessageBox.question(
                    self.window,
                    "Trash not available",
                    f"'{path.name}' could not be moved to the Trash.\n\n"
                    "Delete it permanently instead? This cannot be undone.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                try:
                    path.unlink()
                except OSError as error:
                    QMessageBox.warning(self.window, "Cannot delete file", str(error))
                    return

        try:
            library_db.remove_track(library.db_path, self.relative_path(path))
        except sqlite3.Error:
            pass  # the next rescan will bring the library data up to date
        self.update_library_stats()

        if self.playlist_store is not None:
            try:
                self.playlist_store.remove_track_everywhere(self.relative_path(path))
            except OSError:
                pass

        was_playing = row == self.current_index
        table_bar = self.window.libraryList.verticalScrollBar()
        table_scroll = table_bar.value()

        del self.tracks[row]
        self.library_model.remove_row(row)  # resets the table
        self.refresh_playlist_list(select=self.current_playlist)
        if was_playing:
            self.player.unload()
            self.show_nothing_playing()
            self.reset_play_order()
        else:
            self.play_order.library_row_removed(row)
            if self.current_index > row:
                self.current_index -= 1
            if self.current_index >= 0:
                self.select_in_table(self.current_index)
            self.refresh_upcoming(keep_scroll=True)
        QTimer.singleShot(0, lambda: table_bar.setValue(table_scroll))
        self.show_status(f"Deleted '{title}'")

    # ----- saving and restoring the session -----

    def relative_path(self, path: Path) -> str:
        """A track's path relative to the library folder, as stored in the database."""
        return path.relative_to(self.loaded_library.root).as_posix()

    def save_session(self) -> None:
        """Remember the playing track, its position, the play order and the queue."""
        library = self.loaded_library
        if library is None:
            return
        try:
            if 0 <= self.current_index < len(self.tracks):
                rows, queued, order_position, stop_index = self.play_order.snapshot()
                library_db.save_session(
                    library.db_path,
                    SavedSession(
                        rel_path=self.relative_path(self.tracks[self.current_index]),
                        position_ms=self.position_ms,
                        order=rows,
                        order_position=order_position,
                        queued=queued,
                        stop_after_index=stop_index,
                        playlist=self.saved_play_source(),
                    ),
                )
            else:
                library_db.clear_session(library.db_path)
        except (sqlite3.Error, OSError):
            pass  # failing to save must never get in the way of quitting

    def restore_session(self, info: LibraryInfo) -> bool:
        """Put back what was playing when the app last quit, paused.

        Returns False if there is nothing usable to restore.
        """
        try:
            session = library_db.load_session(info.db_path)
        except sqlite3.Error:
            return False
        if session is None or not self.tracks:
            return False

        # Go back to the list the song was played from, if it still exists.
        store = self.playlist_store
        if session.playlist.startswith(GROUP_SOURCE_PREFIX):
            self.restore_group_source(session.playlist)
        elif session.playlist and store is not None and session.playlist in store.names():
            self.select_playlist(session.playlist)
            self.set_play_source(session.playlist)

        queued = session.queued
        if len(queued) != len(session.order):
            queued = [False] * len(session.order)

        # Normal case: the saved order still fits this library.
        row = -1
        if self.play_order.restore(
            session.order, queued, session.order_position, session.stop_after_index
        ):
            row = self.play_order.current_row
            if self.relative_path(self.tracks[row]) != session.rel_path:
                row = -1

        # Otherwise the library changed since then: keep the song if it still
        # exists, and rebuild the order from it.
        if row < 0:
            self.play_order.reset(len(self.tracks))
            row = next(
                (
                    index
                    for index, path in enumerate(self.tracks)
                    if self.relative_path(path) == session.rel_path
                ),
                -1,
            )
            if row < 0:
                return False
            self.play_order.start_at(row, self.play_source_rows())

        self.play_order.set_context(self.play_source_rows())
        return self.load_track_paused(row, session.position_ms)

    def load_track_paused(self, row: int, position_ms: int = 0) -> bool:
        """Make `row` the current track, loaded but paused at `position_ms`."""
        self.current_index = row
        self.position_ms = position_ms
        self.select_in_table(row)
        self.refresh_upcoming()
        info = self.library_model.info(row)
        duration_ms = (info.duration_ms or 0) if info is not None else 0
        if not self.player.load_file(self.tracks[row], position_ms, duration_ms):
            self.reset_play_order()  # the song file is gone
            return False
        return True


    # ----- artists and albums -----

    def make_filter_bar(self, combo, view, button, visible_key: str) -> FilterBar:
        bar = FilterBar(
            combo, view, button, self.filter_groups, self.filter_cover, self.settings, visible_key
        )
        bar.album_menu_requested.connect(self.show_album_menu)
        return bar

    # ----- the tag editor -----

    def show_album_menu(self, rows: list[int], position) -> None:
        """Right-click menu of an album in a filter list."""
        menu = QMenu(self.window)
        action = menu.addAction(self.menu_icons["edit"], "Edit…")
        action.setEnabled(self.scan_task is None)
        action.triggered.connect(lambda _checked=False: self.edit_tags(rows))
        menu.exec(position)

    def track_menu_neighbours(self, source: str, context: list[int] | None) -> list[int]:
        """The songs around one in the list it was right-clicked in, in shown order."""
        if context is not None:
            return context
        return self.play_order.upcoming_rows() if source == "upcoming" else self.display_rows()

    def edit_tags(self, rows: list[int], neighbours: list[int] | None = None) -> None:
        """Open the tag editor for these songs and write what the user changes.

        The editor's arrow buttons go through `neighbours`.
        """
        library = self.loaded_library
        if library is None:
            return
        paths = [self.tracks[row] for row in rows]
        jobs = TagEditorDialog(
            TAG_EDIT_UI_FILE,
            self.window,
            paths,
            [read_editable_file(path) for path in paths],
            asset_icon("image"),
            asset_icon("star"),
            asset_icon("star_full"),
            self.online_search,
            [self.tracks[row] for row in dict.fromkeys(neighbours or [])],
        ).run()
        if jobs:
            self.show_status(f"Saving tags of {len(jobs)} song{'' if len(jobs) == 1 else 's'}…")
            self.tag_writer.save_tags(jobs, library.root, library.db_path)

    def selected_song_rows(self) -> list[int]:
        """The songs selected in the song table on screen."""
        w = self.window
        pages = [(page.view, page.source) for page in self.group_pages.values()]
        tables = [
            (w.libraryList, "library"),
            (w.playlistSongList, "playlist"),
            (w.historyList, HISTORY_SOURCE),
            *pages,
        ]
        for view, source in tables:
            if view.isVisible():
                return self.selected_library_rows(source)
        return []

    def on_auto_tag_clicked(self) -> None:
        """Tools menu: start auto-tagging the selected songs, or stop if it is running."""
        if self.auto_tag_library is not None:
            self.online_search.stop_auto_tag()
            return
        library = self.loaded_library
        rows = self.selected_song_rows()
        if library is None or not rows:
            self.show_status("Select the songs to tag in a table first")
            return
        if self.scan_task is not None:
            self.show_status("Wait for the scan to finish")
            return
        limit = ask_auto_tag_count(AUTO_TAG_UI_FILE, self.window, len(rows))
        if limit == AUTO_TAG_CANCELLED:
            return
        songs = []
        for row in rows:
            info = self.library_model.info(row)
            artist = info.artist or info.album_artist
            songs.append(AutoTagSong(self.tracks[row], artist, info.title, info.album))
        self.auto_tag_library = library
        self.window.actionAutoTagGenres.setText("Stop Auto-Tagging Genres")
        self.online_search.auto_tag(songs, limit)

    def on_auto_tag_progress(self, done: int, total: int) -> None:
        self.show_status(f"Looking up genres: {done} of {total}…")

    def on_auto_tag_finished(self, results: list, not_found: int, error: str, stopped: bool) -> None:
        library = self.auto_tag_library
        self.auto_tag_library = None
        self.window.actionAutoTagGenres.setText(self.auto_tag_text)
        if stopped:
            self.show_status("Stopped auto-tagging. No files were changed.")
            return
        if error:
            QMessageBox.warning(self.window, "Auto-tag genres", f"The lookup stopped early: {error}")
        if not results:
            self.show_status("No genres were found for the selected songs")
            return
        self.tags_saved_note = f" (no genres found for {not_found})" if not_found else ""
        jobs = [
            TagJob(path, TagChanges({"genre": "; ".join(genres)})) for path, genres in results
        ]
        self.show_status(f"Saving the genres of {len(jobs)} song{'' if len(jobs) == 1 else 's'}…")
        self.tag_writer.save_tags(jobs, library.root, library.db_path)

    # ----- organising files -----

    def on_organise_clicked(self) -> None:
        """Tools menu: move the library's files to where a naming template says."""
        library = self.loaded_library
        if library is None:
            self.show_status("Open a library first")
            return
        if self.scan_task is not None:
            self.show_status("Wait for the scan to finish")
            return
        if self.auto_tag_library is not None or self.tag_writer.is_busy():
            self.show_status("Wait for the tags to finish saving")
            return
        if self.level_tool is not None and self.level_tool.is_running:
            self.show_status("Wait for the volume levelling to finish")
            return
        if self.convert_tool is not None and self.convert_tool.is_running:
            self.show_status("Wait for the conversion to finish")
            return
        if self.device_sync.is_running:
            self.show_status("Wait for the synchronisation to finish")
            return
        try:
            titled = set(library_db.load_tag_values(library.db_path, ["title"]))
        except sqlite3.Error as error:
            self.show_status(f"Cannot read library '{library.name}': {error}")
            return
        self.organise_error = ""
        result = OrganiseDialog(
            ORGANISE_UI_FILE,
            self.window,
            self.settings,
            library.root,
            [self.relative_path(path) for path in self.tracks],
            [self.library_model.info(row) for row in range(len(self.tracks))],
            titled,
            self.apply_file_moves,
        ).run()
        self.show_organise_result(result)

    def library_songs(self) -> list[ConvertSong] | None:
        """Every song of the open library, for sending elsewhere. None if there is no library."""
        library = self.loaded_library
        if library is None:
            self.show_status("Open a library first")
            return None
        try:
            titled = set(library_db.load_tag_values(library.db_path, ["title"]))
        except sqlite3.Error as error:
            self.show_status(f"Cannot read library '{library.name}': {error}")
            return None
        songs = []
        for row, path in enumerate(self.tracks):
            rel_path = self.relative_path(path)
            songs.append(
                ConvertSong(path, rel_path, self.library_model.info(row), rel_path in titled)
            )
        return songs

    def device_sync_source(self) -> tuple[list[ConvertSong], dict[str, list[Path]]] | None:
        """The open library's songs, and the song files of each of its playlists."""
        songs = self.library_songs()
        if songs is None:
            return None
        names = self.playlist_store.names() if self.playlist_store is not None else []
        return songs, {
            name: [self.tracks[row] for row in self.playlist_rows(name)] for name in names
        }

    def show_convert_files(self) -> None:
        songs = self.library_songs()
        if songs is None:
            return
        selected_rows = list(dict.fromkeys(self.selected_song_rows()))
        if self.convert_tool is None:
            self.convert_tool = ConvertFilesTool(
                CONVERT_UI_FILE, self.window, self.settings, self.show_status
            )
        self.convert_tool.show(songs, [songs[row] for row in selected_rows])

    def show_level_volume(self) -> None:
        library = self.loaded_library
        if library is None:
            self.show_status("Open a library first")
            return
        if self.level_tool is None:
            self.level_tool = LevelVolumeTool(
                LEVEL_VOLUME_UI_FILE,
                self.window,
                self.tag_writer,
                self.level_volume_busy_reason,
                self.on_level_volume_finished,
                self.show_status,
            )
        self.level_tool.show(library)

    def level_volume_busy_reason(self) -> str | None:
        """Why levelling cannot start right now, or None if it can."""
        if self.scan_task is not None:
            return "Wait for the scan to finish."
        if self.auto_tag_library is not None:
            return "Wait for the auto-tagging to finish."
        return None

    def on_level_volume_finished(self, library: LibraryInfo, message: str) -> None:
        self.show_status(message)
        loaded = self.loaded_library
        if loaded is not None and loaded.id == library.id:
            self.apply_library_volume(library)

    def apply_library_volume(self, library: LibraryInfo) -> None:
        """Play this library at its own remembered volume, if it has one."""
        try:
            target = library_db.load_level_target(library.db_path)
        except sqlite3.Error:
            target = None
        self.player.set_level_offset(-target if target is not None else 0.0)

    def apply_file_moves(self, moves: list[FileMove]) -> bool:
        """Some files moved: follow them. Returns False if they could not be saved."""
        library = self.loaded_library
        renamed = {move.old: move.new for move in moves}
        for move in moves:
            path = library.root / move.new
            self.tracks[move.row] = path
            self.library_model.set_path(move.row, path)
        entry = self.listen_entry
        if entry is not None and entry["rel_path"] in renamed:
            entry["rel_path"] = renamed[entry["rel_path"]]
            entry["path"] = library.root / entry["rel_path"]
        try:
            library_db.move_tracks(library.db_path, renamed)
            if self.playlist_store is not None:
                self.playlist_store.rename_tracks(renamed)
        except (sqlite3.Error, OSError) as error:
            self.organise_error = str(error)
            return False
        return True

    def show_organise_result(self, result: OrganiseResult) -> None:
        if result.moved:
            self.refresh_filter_bars()
            self.refresh_playlist_list(select=self.current_playlist)
            self.refresh_upcoming(keep_scroll=True)
            if self.current_index in {move.row for move in result.moved}:
                self.update_track_info(self.tracks[self.current_index])
                self.update_mpris()
                self.discord.request_update()
        if self.organise_error:
            QMessageBox.warning(
                self.window,
                "Organise files",
                "Files were moved, but the library could not be updated:\n"
                f"{self.organise_error}",
            )
        if result.failures:
            lines = [f"{name}: {reason}" for name, reason in result.failures[:10]]
            if len(result.failures) > len(lines):
                lines.append(f"…and {len(result.failures) - len(lines)} more")
            QMessageBox.warning(
                self.window, "Some files could not be moved", "\n".join(lines)
            )
        if result.moved or result.failures:
            count = len(result.moved)
            note = " before stopping" if result.stopped else ""
            failed = f", {len(result.failures)} failed" if result.failures else ""
            self.show_status(f"Moved {count} file{'' if count == 1 else 's'}{note}{failed}")

    def on_tags_saved(self, result: SaveResult) -> None:
        """Files were edited: bring the database and everything on screen up to date."""
        try:
            library_db.update_tracks(result.db_path, result.tracks, result.covers)
        except sqlite3.Error as error:
            self.show_status(f"Tags were written, but the library could not be updated: {error}")
            return
        library = self.loaded_library
        if library is not None and library.db_path == result.db_path:
            self.show_saved_tags(result)
        if self.level_tool is not None and self.level_tool.owns(result):
            return  # the levelling tool reports its own progress
        if result.failures:
            name, reason = result.failures[0]
            more = f" and {len(result.failures) - 1} more" if len(result.failures) > 1 else ""
            self.show_status(f"Could not save {name}{more}: {reason}")
        else:
            count = f"{len(result.tracks)} song{'' if len(result.tracks) == 1 else 's'}"
            self.show_status(f"Saved tags of {count}{self.tags_saved_note}")
        self.tags_saved_note = ""

    def show_saved_tags(self, result: SaveResult) -> None:
        rows = self.rows_by_relative_path()
        new_ratings = {
            self.relative_path(job.path): job.changes.rating
            for job in result.jobs
            if job.changes.rating is not None
        }
        shown_tags = self.shown_tag_names()
        for track in result.tracks:
            row = rows.get(track.rel_path, -1)
            old = self.library_model.info(row)
            if old is None:
                continue
            info = track.info
            info.tags = {name: info.tags[name] for name in shown_tags if name in info.tags}
            info.play_count, info.added_at, info.last_played = (
                old.play_count, old.added_at, old.last_played,
            )
            info.file_size = track.file_size
            if track.rel_path in new_ratings:
                info.rating = new_ratings[track.rel_path]
                library_db.set_rating(result.db_path, track.rel_path, info.rating)
            else:
                info.rating = old.rating
            self.library_model.update_track(row, info, result.covers)

        self.refresh_filter_bars()
        self.refresh_playlist_list(select=self.current_playlist)
        self.refresh_upcoming(keep_scroll=True)
        if 0 <= self.current_index < len(self.tracks):
            edited = {job.path for job in result.jobs}
            if self.tracks[self.current_index] in edited:
                self.update_track_info(self.tracks[self.current_index])
                self.update_mpris()
                self.discord.request_update()

    def make_group_page(
        self, source: str, view, header_key: str, filter_bar: FilterBar
    ) -> GroupPage:
        """Set up the song table of one tab."""
        w = self.window
        tracks = PlaylistTrackModel(self.library_model, w)
        proxy = SortedTrackModel(w)
        proxy.setSourceModel(tracks)
        setup_library_table(view, proxy)
        self.restore_table_layout(view, header_key)

        page = GroupPage(source, tracks, proxy, view, header_key, filter_bar)
        view.doubleClicked.connect(lambda index, p=page: self.play_group_item(p, index))
        filter_bar.chosen.connect(
            lambda kind, value, p=page: self.on_group_filter_chosen(p, kind, value)
        )
        return page

    def refresh_filter_bars(self) -> None:
        """The library changed: rebuild the filter bars of every tab."""
        self.auto_playlist_cache.clear()  # library rows and tags may be different now
        self.song_search_index = None
        self.refresh_history()
        for bar in self.filter_bars():
            bar.forget()
        for page in self.group_pages.values():
            self.show_group_songs(page)
        self.show_song_filter()

    def show_group_songs(self, page: GroupPage) -> None:
        """Fill the Artists or Albums tab from its filter, over the whole library."""
        rows = list(range(len(self.tracks)))
        kind, value = self.load_filter(page.source)
        kind = kind or page.source  # a new tab starts on its own tag
        shown = page.filter_bar.show(page.source, rows, kind, value)
        group = page.filter_bar.group(value)
        if group.key:
            shown = group.rows  # sorted by album and track; positions are library rows here
        page.kind, page.current = kind, group
        page.tracks.set_rows(shown)
        if page.source == "artist":
            self.window.artistsTabStats.setText(self.describe_playlist(shown))
            self.show_artist_header(page)
        else:
            self.show_album_header(page)

    # ----- the filter bars -----

    def filter_bars(self) -> list[FilterBar]:
        pages = [page.filter_bar for page in self.group_pages.values()]
        return [self.playlist_filter, self.song_filter, *pages]

    def filter_groups(self, kind: str, rows: list[int]) -> list[TrackGroup]:
        """The values of a tag among these songs. Each group's rows are positions in `rows`."""
        infos = [self.library_model.info(row) for row in rows]
        folders = [self.tracks[row].parent for row in rows]
        return GROUPERS[kind](infos, folders)

    def filter_cover(self, kind: str, group: TrackGroup, row: int) -> QImage | None:
        """The picture of a filter row: the artist's, or the album's cover."""
        if kind in ("artist", "album_artist"):
            return self.artist_info.thumbnail(group.name)
        if kind == "album":
            index = self.library_model.index(row, 0)
            return self.library_model.data(index, TrackListModel.CoverRole)
        return None

    def filter_setting(self, owner: str) -> str | None:
        """The setting that holds a filter, or None if no library is loaded."""
        if self.loaded_library is None:
            return None
        digest = hashlib.sha1(owner.encode("utf-8")).hexdigest()  # owners can hold any text
        return f"filters/{self.loaded_library.id}/{digest}"

    def load_filter(self, owner: str) -> tuple[str, str]:
        """The saved (kind, value) of the Songs, Artists or Albums tab."""
        key = self.filter_setting(owner)
        text = self.settings.value(key, "", type=str) if key else ""
        kind, _, value = text.partition("\n")
        return kind, value

    def save_filter(self, owner: str, kind: str, value: str) -> None:
        key = self.filter_setting(owner)
        if key:
            self.settings.setValue(key, f"{kind}\n{value}")

    def show_song_filter(self) -> None:
        """Fill the Songs tab's filter bar and apply it to the table."""
        rows = list(range(len(self.tracks)))
        shown = self.song_filter.show(SONGS_FILTER_OWNER, rows, *self.load_filter(SONGS_FILTER_OWNER))
        shown = self.song_search_matches(shown)
        self.library_proxy.set_shown_rows(None if len(shown) == len(rows) else shown)

    def search_songs_now(self) -> None:
        self.song_search_timer.stop()
        self.show_song_filter()

    def song_search_matches(self, rows: list[int]) -> list[int]:
        """The rows containing every word typed in the search bar, in any tag."""
        words = text_key(self.window.lineEdit.text()).split()
        if not words:
            return rows
        if self.song_search_index is None:
            self.song_search_index = self.build_song_search_index()
        index = self.song_search_index
        return [row for row in rows if all(word in index[row] for word in words)]

    def build_song_search_index(self) -> list[str]:
        library = self.loaded_library
        try:
            stored = library_db.load_search_text(library.db_path) if library else {}
        except sqlite3.Error:
            stored = {}
        index = []
        for row, path in enumerate(self.tracks):
            info = self.library_model.info(row)
            fields = (info.title, info.artist, info.album_artist, info.album, info.year, info.genre)
            index.append(text_key(" ".join((*fields, stored.get(self.relative_path(path), "")))))
        return index

    def clear_song_search(self) -> None:
        """Empty the search bar, so that it hides no songs."""
        if self.window.lineEdit.text():
            self.window.lineEdit.clear()
            self.search_songs_now()

    def on_song_filter_chosen(self, kind: str, value: str) -> None:
        self.save_filter(SONGS_FILTER_OWNER, kind, value)
        self.show_song_filter()
        if self.current_index >= 0:
            self.select_in_table(self.current_index)
            if self.play_source is None and self.source_group is None:
                self.update_play_context()

    def on_group_filter_chosen(self, page: GroupPage, kind: str, value: str) -> None:
        self.save_filter(page.source, kind, value)
        self.show_group_songs(page)
        if self.current_index >= 0 and self.source_group == page.source:
            self.update_play_context()

    def update_play_context(self) -> None:
        """The list being played now shows other songs: follow it."""
        self.play_order.context_changed(self.play_source_rows())
        self.refresh_upcoming(keep_scroll=True)

    def show_artist_header(self, page: GroupPage) -> None:
        """Picture and description only make sense when one artist is chosen."""
        w = self.window
        info = self.artist_info
        group = page.current
        is_artist = bool(group.key) and page.kind in ("artist", "album_artist")
        w.artistsTabArtistName.setText(group.name)
        self.artist_description.set_text(info.description(group.name) if is_artist else "")
        self.set_group_icon(w.artistsTabIcon, info.image(group.name) if is_artist else None, "user")

    def show_album_header(self, page: GroupPage) -> None:
        """The cover only makes sense when one album is chosen."""
        w = self.window
        group = page.current
        is_album = bool(group.key) and page.kind == "album"
        w.albumsTabAlbumName.setText(group.name)
        w.albumsTabArtist.setText(group.artist)
        w.albumsTabYear.setText(group.year)
        self.set_group_icon(w.albumsTabIcon, self.album_image(group) if is_album else None, "disc")

    @staticmethod
    def set_group_icon(label: QLabel, image: QImage | None, placeholder: str) -> None:
        if image is None:
            label.setPixmap(asset_icon(placeholder).pixmap(GROUP_ICON_SIDE, GROUP_ICON_SIDE))
        else:
            label.setPixmap(QPixmap.fromImage(make_square_icon(image)))

    def album_image(self, group: TrackGroup) -> QImage | None:
        """The album's full-size cover, read from its first song."""
        cover = read_track_details(self.tracks[group.rows[0]]).cover
        image = QImage.fromData(cover) if cover else None
        return image if image is not None and not image.isNull() else None

    def on_artist_updated(self, name: str) -> None:
        """A picture or description arrived from the internet."""
        key = name.casefold()
        for bar in self.filter_bars():
            bar.refresh(key)
        page = self.group_pages["artist"]
        if page.current is not None and page.current.key == key:
            self.show_artist_header(page)

    def group_display_rows(self, page: GroupPage) -> list[int]:
        """Library rows of the group on screen, in the order its table shows them."""
        return [page.tracks.library_row(row) for row in page.proxy.source_rows()]

    def play_group_item(self, page: GroupPage, model_index: QModelIndex) -> None:
        """The user double-clicked a song in the artist or album table."""
        row = page.tracks.library_row(page.proxy.mapToSource(model_index).row())
        if row >= 0:
            self.play_now(row, context=self.group_display_rows(page), group=page.source)

    def source_group_rows(self) -> list[int]:
        """Library rows of the tab being played, as its table shows them."""
        if self.source_group == HISTORY_SOURCE:
            return self.history_display_rows()
        return self.group_display_rows(self.group_pages[self.source_group])

    # ----- the History tab -----

    def refresh_history(self) -> None:
        """Show every song that has been played, the most recent first."""
        library = self.library_model
        rows = [row for row in range(library.rowCount()) if library.info(row).last_played]
        rows.sort(key=lambda row: library.info(row).last_played, reverse=True)
        bar = self.window.historyList.verticalScrollBar()
        scroll = bar.value()
        self.history_tracks.set_rows(rows)
        QTimer.singleShot(0, lambda: bar.setValue(scroll))

    def history_display_rows(self) -> list[int]:
        """Library rows of the History table, in the order it shows them."""
        return [self.history_tracks.library_row(row) for row in self.history_proxy.source_rows()]

    def play_history_item(self, model_index: QModelIndex) -> None:
        """The user double-clicked a song in the History table."""
        row = self.history_tracks.library_row(self.history_proxy.mapToSource(model_index).row())
        if row >= 0:
            self.play_now(row, context=self.history_display_rows(), group=HISTORY_SOURCE)

    def saved_play_source(self) -> str:
        """What the session remembers as being played ("" means the library)."""
        if self.source_group is not None:
            return f"{GROUP_SOURCE_PREFIX}{self.source_group}"
        return self.play_source or ""

    def restore_group_source(self, saved: str) -> None:
        """Go back to playing from the tab named by saved_play_source."""
        source = saved[len(GROUP_SOURCE_PREFIX):].partition("/")[0]
        if source in self.group_pages or source == HISTORY_SOURCE:
            self.set_play_source(None)
            self.source_group = source

    # ----- the side list -----

    def load_json_setting(self, key: str) -> list[str]:
        """A list of text saved as JSON (QSettings mangles lists of one item)."""
        try:
            data = json.loads(self.settings.value(key, "[]", type=str))
        except ValueError:
            return []
        return [item for item in data if isinstance(item, str)] if isinstance(data, list) else []

    def save_side_expanded(self) -> None:
        self.settings.setValue("sideListExpanded", json.dumps(sorted(self.side_expanded)))

    def open_side_entries(self, *entries: str) -> None:
        """Make these rows open the next time the list is built."""
        self.side_expanded.update(entries)
        self.save_side_expanded()

    def apply_side_expansion(self) -> None:
        view = self.window.sideList
        for index in self.side_model.opening_indexes():
            view.setExpanded(index, self.side_model.id_of(index) in self.side_expanded)

    def on_side_list_toggled(self, index: QModelIndex, is_open: bool) -> None:
        """A row opened or closed: update its arrow and remember it."""
        self.side_model.set_open(index, is_open)
        entry = self.side_model.id_of(index)
        if is_open != (entry in self.side_expanded):
            (self.side_expanded.add if is_open else self.side_expanded.discard)(entry)
            self.save_side_expanded()

    def refresh_devices(self) -> None:
        """Show the storage devices that are plugged in, if that has changed."""
        devices = connected_devices()
        if devices != self.devices:
            self.devices = devices
            self.side_model.set_devices(devices)
            self.apply_side_expansion()
            self.devices_tab.set_devices(devices)
        elif self.window.devicesTab.isVisible() and not self.device_sync.is_running:
            self.devices_tab.refresh_levels()  # a sync must not be interrupted by these checks

    def show_device_tab(self, folder: str) -> None:
        self.window.podcastsTab.setCurrentWidget(self.window.devicesTab)
        self.devices_tab.select(folder)

    def device_name(self, folder: str) -> str:
        return next((name for name, path in self.devices if path == folder), folder)

    def open_device(self, folder: str) -> None:
        if not open_in_dolphin(folder):
            self.show_status("Could not start Dolphin")

    def safely_remove_device(self, folder: str) -> None:
        if self.device_sync.is_running:
            self.show_status("Wait for the synchronisation to finish")
            return
        self.show_status(f"Safely removing {self.device_name(folder)}…")
        self.device_ejector.eject(self.device_name(folder), folder)

    def on_device_ejected(self, message: str) -> None:
        self.show_status(message)
        self.refresh_devices()

    def on_side_list_clicked(self, index: QModelIndex) -> None:
        model = self.side_model
        kind = model.kind(index)
        if kind == PLAYLIST:
            self.show_tab_of(self.window.playlistSongList)
            return
        if kind in (SECTION, FOLDER):
            view = self.window.sideList
            view.setExpanded(index, not view.isExpanded(index))
        # Clicking a row that cannot be selected clears the highlight: put it back.
        if self.current_playlist is not None:
            self.playlist_selection.select(
                model.playlist_index(self.current_playlist),
                QItemSelectionModel.SelectionFlag.ClearAndSelect,
            )

    def show_side_menu(self, pos) -> None:
        """Right-click menu of the side list."""
        view = self.window.sideList
        model = self.side_model
        index = view.indexAt(pos)
        kind, value = model.kind(index), model.value(index)
        menu = QMenu(view)

        def add(label, icon_key, callback, enabled=True, target=menu):
            action = target.addAction(self.menu_icons.get(icon_key, QIcon()), label)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _checked=False: callback())

        if kind == PIN or (kind == SECTION and value == COMPUTER_SECTION):
            add("Pin a folder…", "pin_folder", self.pin_folder)
            if kind == PIN:
                menu.addSeparator()
                add("Open", "open_folder", lambda: self.open_pinned_folder(value))
                add("Unpin", "unpin_folder", lambda: self.unpin_folder(value))
        elif kind == DEVICE:
            add("Synchronise…", "sync_device", lambda: self.show_device_tab(value))
            add("Open in Dolphin", "open_device", lambda: self.open_device(value))
            add("Safely Remove", "remove_device", lambda: self.safely_remove_device(value))
        elif kind == SECTION and value == DEVICES_SECTION:
            pass  # nothing to offer yet
        elif self.playlist_store is not None:
            self.fill_playlist_menu(menu, add, kind, value)
        if not menu.isEmpty():
            menu.exec(view.viewport().mapToGlobal(pos))

    def fill_playlist_menu(self, menu: QMenu, add, kind: str | None, value: str | None) -> None:
        """The menu entries for the Playlists section, a folder or a playlist."""
        store = self.playlist_store
        name = value if kind == PLAYLIST else None
        if name is not None and name != self.current_playlist:
            self.select_playlist(name)

        folder = value if kind == FOLDER else ""
        add("New playlist…", "new_playlist", lambda: self.new_playlist(folder=folder))
        add("New auto-playlist…", "new_playlist", lambda: self.new_auto_playlist(folder=folder))
        add("New folder…", "new_folder", self.new_playlist_folder)
        add("Import playlist…", "import_playlist", self.import_playlist)
        if kind == FOLDER:
            menu.addSeparator()
            add("Rename folder…", "rename_folder", lambda: self.rename_playlist_folder(value))
            add("Delete folder", "delete_folder", lambda: self.delete_playlist_folder(value))
        if name is None:
            return

        menu.addSeparator()
        add("Play", "play_playlist", lambda: self.play_playlist(name))
        add("Edit…", "edit_playlist", lambda: self.edit_playlist(name))
        add("Rename…", "rename_playlist", lambda: self.rename_playlist(name))
        add("Duplicate", "duplicate_playlist", lambda: self.duplicate_playlist(name))
        add("Change icon…", "playlist_icon", self.choose_playlist_icon)
        add(
            "Remove icon", "remove_playlist_icon",
            lambda: self.remove_playlist_icon(name),
            enabled=store.icon_file(name).exists(),
        )
        if not store.load(name).is_auto:
            locked = self.side_model.is_locked(name)
            add(
                "Unlock playlist" if locked else "Lock playlist", "lock_playlist",
                lambda: self.set_playlist_locked(name, not locked),
            )
        add("Export playlist…", "export_playlist", lambda: self.export_playlist(name))

        current_folder = store.load(name).folder
        move_menu = menu.addMenu(self.menu_icons["move_to_folder"], "Move to folder")
        add(
            "No folder", None, lambda: self.move_to_folder(name, ""),
            enabled=bool(current_folder), target=move_menu,
        )
        for target in store.folders():
            add(
                target.replace("&", "&&"), None,
                lambda t=target: self.move_to_folder(name, t),
                enabled=target != current_folder, target=move_menu,
            )

        menu.addSeparator()
        add("Delete playlist…", "delete_playlist", lambda: self.delete_playlist(name))

    def ask_folder_name(self, title: str, initial: str = "", ignore: str | None = None) -> str | None:
        """Ask for a folder name until it is valid. None if the user cancels."""
        text = initial
        while True:
            text, accepted = QInputDialog.getText(self.window, title, "Folder name:", text=text)
            if not accepted:
                return None
            problem = self.playlist_store.check_folder_name(text, ignore)
            if problem is None:
                return text.strip()
            QMessageBox.warning(self.window, "Folder name", problem)

    def change_playlist_folders(self, change) -> None:
        """Run a change to the folders and show the result."""
        try:
            change()
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot update folders", str(error))
            return
        self.refresh_playlist_list(select=self.current_playlist)

    def new_playlist_folder(self) -> None:
        name = self.ask_folder_name("New folder")
        if name is not None:
            self.open_side_entries(entry_id(SECTION, PLAYLISTS_SECTION))
            self.change_playlist_folders(lambda: self.playlist_store.create_folder(name))

    def rename_playlist_folder(self, old: str) -> None:
        new = self.ask_folder_name("Rename folder", initial=old, ignore=old)
        if new is None or new == old:
            return
        if entry_id(FOLDER, old) in self.side_expanded:
            self.side_expanded.discard(entry_id(FOLDER, old))
            self.open_side_entries(entry_id(FOLDER, new))
        self.change_playlist_folders(lambda: self.playlist_store.rename_folder(old, new))

    def delete_playlist_folder(self, name: str) -> None:
        answer = QMessageBox.question(
            self.window,
            "Delete folder",
            f"Delete the folder '{name}'?\n\nThe playlists in it are kept, outside any folder.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.change_playlist_folders(lambda: self.playlist_store.delete_folder(name))

    def move_to_folder(self, name: str, folder: str) -> None:
        if folder:
            self.open_side_entries(entry_id(FOLDER, folder))
        self.change_playlist_folders(lambda: self.playlist_store.set_folder(name, folder))

    def save_pinned_folders(self) -> None:
        self.settings.setValue("pinnedFolders", json.dumps(self.pinned_folders))
        self.refresh_playlist_list(select=self.current_playlist)

    def pin_folder(self) -> None:
        start_dir = QStandardPaths.writableLocation(QStandardPaths.HomeLocation)
        path = QFileDialog.getExistingDirectory(self.window, "Pin a folder", start_dir)
        if path and path not in self.pinned_folders:
            self.pinned_folders.append(path)
            self.open_side_entries(entry_id(SECTION, COMPUTER_SECTION))
            self.save_pinned_folders()

    def unpin_folder(self, path: str) -> None:
        self.pinned_folders.remove(path)
        self.save_pinned_folders()

    def open_pinned_folder(self, path: str) -> None:
        """Open a pinned folder in the system's file manager."""
        if not Path(path).is_dir():
            self.show_status(f"This folder no longer exists: {path}")
        elif not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            self.show_status(f"Could not open {path}")

    # ----- playlists -----

    def load_playlists(self) -> None:
        """Open the playlists of the loaded library (none if no library is loaded)."""
        library = self.loaded_library
        self.playlist_store = (
            PlaylistStore(library.db_path.parent / "playlists") if library else None
        )
        self.current_playlist = None
        self.play_source = None
        self.source_group = None
        self.refresh_playlist_list(select=self.saved_last_playlist())

    def last_playlist_key(self, library: LibraryInfo) -> str:
        """The setting that holds a library's last played playlist."""
        return f"lastPlaylist/{library.id}"

    def saved_last_playlist(self) -> str | None:
        """The last playlist played in the loaded library, if any."""
        if self.loaded_library is None:
            return None
        key = self.last_playlist_key(self.loaded_library)
        return self.settings.value(key, "", type=str) or None

    def set_play_source(self, name: str | None) -> None:
        """Note which list is being played: a playlist's name, or None for the library."""
        self.play_source = name
        self.source_group = None
        if name is not None and self.loaded_library is not None:
            self.settings.setValue(self.last_playlist_key(self.loaded_library), name)

    def play_source_rows(self) -> list[int]:
        """Library rows of the list being played, in the order they are played."""
        if self.source_group is not None:
            rows = self.source_group_rows()
            if rows:
                return rows
        name = self.play_source
        if name is None:
            return self.display_rows()
        rows = (
            self.playlist_display_rows()  # on screen, so its sort order applies
            if name == self.current_playlist
            else self.filtered_playlist_rows(name)
        )
        return rows or self.display_rows()

    def rows_by_relative_path(self) -> dict[str, int]:
        if self.loaded_library is None:
            return {}
        return {self.relative_path(path): row for row, path in enumerate(self.tracks)}

    def playlist_rows(self, name: str) -> list[int]:
        """Library rows of a playlist's songs, in the order it was saved."""
        if self.playlist_store is None:
            return []
        playlist = self.playlist_store.load(name)
        if playlist.is_auto:
            return self.auto_playlist_rows(playlist)
        rows_by_rel = self.rows_by_relative_path()
        return [rows_by_rel[rel] for rel in playlist.tracks if rel in rows_by_rel]

    def auto_playlist_rows(self, playlist: Playlist) -> list[int]:
        """Library rows of a playlist's songs: in library order, or as its limit chose them."""
        library = self.loaded_library
        if library is None:
            return []
        rules = playlist.rules
        key = json.dumps(
            [rules, playlist.match_any, playlist.no_duplicates, playlist.limit], sort_keys=True
        )
        if key not in self.auto_playlist_cache:
            names = sorted({rule["tag"] for rule in rules} - {"rating", TIME_TAG})

            def tags_of(row: int, path: Path) -> dict[str, str]:
                """The song's tags, plus what MusicHive itself knows about it."""
                info = self.library_model.info(row)
                return {
                    **values.get(self.relative_path(path), {}),
                    "rating": str(info.rating),  # 0 for an unrated song
                    TIME_TAG: str((info.duration_ms or 0) // 1000),
                }

            try:
                values = library_db.load_tag_values(library.db_path, names)
            except sqlite3.Error:
                values = {}
            rows = [
                row
                for row, path in enumerate(self.tracks)
                if song_matches(rules, tags_of(row, path), playlist.match_any)
            ]
            if playlist.no_duplicates:
                rows = self.without_duplicates(rows)
            if playlist.limit is not None:
                rows = apply_limit(rows, self.library_model.info, self.album_key, playlist.limit)
            self.auto_playlist_cache[key] = rows
        return list(self.auto_playlist_cache[key])

    def album_key(self, row: int) -> str:
        return group_key("album", self.library_model.info(row), self.tracks[row].parent)

    def without_duplicates(self, rows: list[int]) -> list[int]:
        """Keep the first of the songs with the same artist and title.

        Without an artist there is too little to tell two songs are the same.
        """
        seen: set[str] = set()
        kept = []
        for row in rows:
            info = self.library_model.info(row)
            key = library_db.track_key(info.artist, info.title) if info.artist else None
            if key is None or key not in seen:
                kept.append(row)
            if key is not None:
                seen.add(key)
        return kept

    def is_auto_playlist(self, name: str | None) -> bool:
        store = self.playlist_store
        return store is not None and name is not None and store.load(name).is_auto

    def refresh_playlist_list(self, select: str | None = None) -> None:
        """Re-read the playlists (names, icons, song counts) and show one of them."""
        store = self.playlist_store
        names = store.names() if store is not None else []
        entries = []
        for name in names:
            playlist = store.load(name)
            entries.append((name, playlist.folder, playlist.read_only, playlist.is_auto))
        self.device_sync.set_playlists(names)
        folders = store.folders() if store is not None else []
        self.side_model.rebuild(folders, entries, self.pinned_folders)
        self.apply_side_expansion()

        target = select if select in names else (names[0] if names else None)
        if target is None:
            self.show_playlist(None)
        else:
            self.select_playlist(target)

    def on_playlist_selected(self, current: QModelIndex, previous: QModelIndex) -> None:
        if self.side_model.kind(current) == PLAYLIST:
            self.show_playlist(self.side_model.value(current))

    def select_playlist(self, name: str) -> None:
        index = self.side_model.playlist_index(name)
        if not index.isValid():
            return
        self.playlist_selection.setCurrentIndex(
            index, QItemSelectionModel.SelectionFlag.ClearAndSelect
        )
        if self.current_playlist != name:  # the selection did not change on its own
            self.show_playlist(name)

    def show_playlist(self, name: str | None) -> None:
        """Show a playlist's name, icon, stats and songs (None shows an empty tab)."""
        self.current_playlist = name
        w = self.window
        store = self.playlist_store
        w.playlistEditButton.setEnabled(bool(name) and store is not None)
        if not name or store is None:
            self.playlist_tracks.set_playlist(None, [], False)
            self.playlist_filter.clear()
            w.playlistsTabPlaylistName.setText(NO_PLAYLIST_TEXT)
            w.playlistsTabStats.setText("")
            self.playlist_description.set_text("")
            self.show_playlist_icon(None)
            self.show_lock_state(None, False)
            return

        rows = self.playlist_rows(name)
        playlist = store.load(name)
        shown = self.playlist_filter.show(name, rows, playlist.filter_kind, playlist.filter_value)
        self.playlist_tracks.set_playlist(name, shown, playlist.read_only)
        self.show_lock_state(name, playlist.locked)
        if playlist.is_auto:
            w.playlistLockButton.setEnabled(False)
            w.playlistLockButton.setToolTip("An auto-playlist fills itself from its rules.")
        w.playlistsTabPlaylistName.setText(name)
        w.playlistsTabStats.setText(self.describe_playlist(shown))
        self.playlist_description.set_text(playlist.description)
        self.show_playlist_icon(name)

    def filtered_playlist_rows(self, name: str) -> list[int]:
        """Library rows of a playlist's songs that pass its filter."""
        if self.playlist_store is None:
            return []
        playlist = self.playlist_store.load(name)
        return self.playlist_filter.filtered(
            self.playlist_rows(name), playlist.filter_kind, playlist.filter_value
        )

    def save_playlist_filter(self, kind: str, value: str) -> None:
        store = self.playlist_store
        name = self.current_playlist
        if store is None or name is None:
            return
        try:
            store.set_filter(name, kind, value)
        except OSError as error:
            self.show_status(f"Cannot save the filter: {error}")
            return
        self.playlist_edited(name)

    def describe_playlist(self, rows: list[int]) -> str:
        total_ms = sum(self.library_model.info(row).duration_ms or 0 for row in rows)
        noun = "Song" if len(rows) == 1 else "Songs"
        return f"{len(rows)} {noun}, {format_long_duration(total_ms, seconds=True)}"

    def playlist_pixmap(self, name: str | None, side: int) -> QPixmap:
        """The playlist's icon at `side` pixels, or the placeholder if it has none."""
        store = self.playlist_store
        if name and store is not None and store.icon_file(name).exists():
            image = QImage(str(store.icon_file(name)))
            if not image.isNull():
                scaled = image.scaled(
                    side,
                    side,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                return QPixmap.fromImage(scaled)
        return self.playlist_placeholder.pixmap(side, side)

    def show_playlist_icon(self, name: str | None) -> None:
        """Show the playlist's icon in the widget at the top of the tab."""
        store = self.playlist_store
        gif = store.animated_icon(name) if name and store is not None else None
        playing = self.tab_icon_animation
        if playing is not None:
            if playing.matches(gif):
                return  # already on screen: don't restart it
            playing.dispose()
            self.tab_icon_animation = None

        widget = self.window.playlistsTabIcon
        side = min(widget.width(), widget.height()) - (0 if hasattr(widget, "setPixmap") else 8)
        if side < 32:  # the tab has not been laid out yet
            side = 96
        self.set_tab_icon(self.playlist_pixmap(name, side))

        if gif is not None:
            self.tab_icon_animation = IconAnimation(gif)
            self.tab_icon_animation.frame_ready.connect(
                lambda frame: self.set_tab_icon(QPixmap.fromImage(frame))
            )
            self.tab_icon_animation.start()

    def set_tab_icon(self, pixmap: QPixmap) -> None:
        widget = self.window.playlistsTabIcon
        if hasattr(widget, "setPixmap"):
            widget.setPixmap(pixmap)
        else:
            widget.setIcon(QIcon(pixmap))
            widget.setIconSize(pixmap.size())

    def choose_playlist_icon(self) -> None:
        store = self.playlist_store
        name = self.current_playlist
        if store is None or name is None:
            self.show_status("Select or create a playlist first")
            return
        start_dir = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            "Choose an icon for the playlist",
            start_dir,
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
        )
        if not path:
            return
        image = QImage(path)
        if image.isNull():
            QMessageBox.warning(self.window, "Playlist icon", "That image could not be read.")
            return
        animation = Path(path) if is_animated_gif(path) else None
        if self.save_playlist_icon(name, make_square_icon(image), animation):
            self.refresh_playlist_list(select=name)

    def save_playlist_icon(
        self, name: str, image: QImage, animation: Path | None = None
    ) -> bool:
        """Store a playlist's icon, telling the user if that fails.

        `image` is the still icon; `animation` is the GIF to play, if there is one.
        """
        store = self.playlist_store
        try:
            store.folder.mkdir(parents=True, exist_ok=True)
            saved = image.save(str(store.icon_file(name)), "PNG")
            if saved:
                store.set_animated_icon(name, animation)
        except OSError:
            saved = False
        if not saved:
            QMessageBox.warning(self.window, "Playlist icon", "The icon could not be saved.")
        return saved

    def remove_playlist_icon(self, name: str) -> None:
        if self.playlist_store is not None:
            self.playlist_store.remove_icon(name)
            self.refresh_playlist_list(select=name)

    def ask_playlist_name(
        self, title: str, label: str, initial: str = "", ignore: str | None = None
    ) -> str | None:
        """Ask for a playlist name until it is valid. None if the user cancels."""
        text = initial
        while True:
            text, accepted = QInputDialog.getText(self.window, title, label, text=text)
            if not accepted:
                return None
            problem = self.playlist_store.check_name(text, ignore)
            if problem is None:
                return text.strip()
            QMessageBox.warning(self.window, "Playlist name", problem)

    def new_playlist(self, add_rows: list[int] | None = None, folder: str = "") -> None:
        """Create a playlist, in `folder` if given, and add songs if `add_rows` is given."""
        store = self.playlist_store
        if store is None:
            return
        details = self.ask_playlist_details("New playlist")
        if details is None:
            return
        name = details.name
        try:
            store.save(Playlist(name, self.relative_paths(add_rows or []), folder=folder))
            self.save_playlist_details(name, details)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot create playlist", str(error))
            return
        self.open_side_entries(entry_id(SECTION, PLAYLISTS_SECTION))
        if folder:
            self.open_side_entries(entry_id(FOLDER, folder))
        self.refresh_playlist_list(select=name)
        if add_rows:
            self.show_status(f"Added {self.describe_songs(add_rows)} to {name}")

    def ask_playlist_details(
        self,
        window_title: str,
        name: str = "",
        description: str = "",
        rules: list[dict] | None = None,
        match_any: bool = False,
        no_duplicates: bool = False,
        limit: dict | None = None,
    ) -> PlaylistDetails | None:
        """Open the playlist window, or the auto-playlist one if `rules` is given.

        None if the user cancels.
        """
        store = self.playlist_store
        icon = None
        if name and store.icon_file(name).exists():
            loaded = QImage(str(store.icon_file(name)))
            icon = loaded if not loaded.isNull() else None
        arguments = (
            self.window,
            self.playlist_placeholder,
            lambda text: store.check_name(text, name or None),
            window_title,
            name,
            description,
            icon,
            store.animated_icon(name) if name else None,
        )
        if rules is None:
            return PlaylistEditDialog(PLAYLIST_EDIT_UI_FILE, *arguments).run()
        return AutoPlaylistEditDialog(
            AUTO_PLAYLIST_EDIT_UI_FILE,
            AUTO_PLAYLIST_RULE_UI_FILE,
            self.rule_tag_choices(rules),
            rules,
            match_any,
            no_duplicates,
            limit,
            *arguments,
        ).run()

    def rule_tag_choices(self, rules: list[dict]) -> list[tuple[str, str]]:
        """The tags an auto-playlist's rules can use, as (name, title) by title."""
        library = self.loaded_library
        try:
            names = library_db.all_tag_names(library.db_path) if library else []
        except sqlite3.Error:
            names = []
        names = {*names, *COMMON_RULE_TAGS, *(rule["tag"] for rule in rules)}
        titled = [(name, tag_column(name).title) for name in names]
        return sorted(titled, key=lambda choice: choice[1].casefold())

    def new_auto_playlist(self, folder: str = "") -> None:
        """Create a playlist that fills itself from rules."""
        store = self.playlist_store
        if store is None:
            return
        details = self.ask_playlist_details("New auto-playlist", rules=[])
        if details is None:
            return
        try:
            store.save(Playlist(details.name, folder=folder, rules=details.rules))
            self.save_playlist_details(details.name, details)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot create playlist", str(error))
            return
        self.open_side_entries(entry_id(SECTION, PLAYLISTS_SECTION))
        if folder:
            self.open_side_entries(entry_id(FOLDER, folder))
        self.refresh_playlist_list(select=details.name)

    def save_playlist_details(self, name: str, details: PlaylistDetails) -> None:
        """Store the description and image from the playlist window. Raises OSError."""
        store = self.playlist_store
        store.set_description(name, details.description)
        if details.rules is not None:
            store.set_rules(
                name, details.rules, details.match_any, details.no_duplicates, details.limit
            )
        if details.icon_changed:
            if details.icon is None:
                store.remove_icon(name)
            else:
                self.save_playlist_icon(name, details.icon, details.animation)

    def edit_playlist(self, name: str | None = None) -> None:
        """Open the playlist window for a playlist (by default the one on screen)."""
        store = self.playlist_store
        name = name or self.current_playlist
        if store is None or name is None:
            self.show_status("Select or create a playlist first")
            return
        playlist = store.load(name)
        details = self.ask_playlist_details(
            "Edit auto-playlist" if playlist.is_auto else "Edit playlist",
            name,
            playlist.description,
            playlist.rules,
            playlist.match_any,
            playlist.no_duplicates,
            playlist.limit,
        )
        if details is None:
            return
        current = name
        try:
            if details.name != name:
                self.move_playlist(name, details.name)
                current = details.name
            self.save_playlist_details(current, details)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot update playlist", str(error))
        bar = self.window.playlistSongList.verticalScrollBar()
        scroll = bar.value()
        self.refresh_playlist_list(select=current)
        QTimer.singleShot(0, lambda: bar.setValue(scroll))
        if details.rules is not None and current == self.play_source and self.current_index >= 0:
            self.update_play_context()  # the rules may have changed what comes next

    def move_playlist(self, old: str, new: str) -> None:
        """Rename a playlist and everything that refers to it. Raises OSError."""
        self.playlist_store.rename(old, new)
        if self.play_source == old:
            self.play_source = new
        if self.saved_last_playlist() == old:
            self.settings.setValue(self.last_playlist_key(self.loaded_library), new)

    def rename_playlist(self, name: str) -> None:
        store = self.playlist_store
        if store is None:
            return
        new = self.ask_playlist_name("Rename playlist", "New name:", initial=name, ignore=name)
        if new is None or new == name:
            return
        try:
            self.move_playlist(name, new)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot rename playlist", str(error))
            return
        self.refresh_playlist_list(select=new)

    def duplicate_playlist(self, name: str) -> None:
        """Make a copy of a playlist, next to it, and show the copy."""
        store = self.playlist_store
        if store is None:
            return
        new = store.copy_name(name)
        try:
            store.duplicate(name, new)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot duplicate playlist", str(error))
            return
        self.refresh_playlist_list(select=new)
        self.show_status(f"Created '{new}'")

    def delete_playlist(self, name: str) -> None:
        store = self.playlist_store
        if store is None:
            return
        answer = QMessageBox.question(
            self.window,
            "Delete playlist",
            f"Delete the playlist '{name}'?\n\nThe songs themselves are not deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            store.delete(name)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot delete playlist", str(error))
            return
        if self.play_source == name:
            self.play_source = None
        if self.saved_last_playlist() == name:
            self.settings.remove(self.last_playlist_key(self.loaded_library))
        self.refresh_playlist_list()

    def relative_paths(self, rows: list[int]) -> list[str]:
        """Library-relative paths of these library rows (rows that no longer exist are left out)."""
        return [self.relative_path(self.tracks[row]) for row in rows if 0 <= row < len(self.tracks)]

    def describe_songs(self, rows: list[int]) -> str:
        """For messages: a song's title in quotes, or "3 songs"."""
        if len(rows) == 1:
            return f"'{self.library_model.info(rows[0]).title}'"
        return f"{len(rows)} songs"

    def add_to_playlist(self, name: str, rows: list[int], before: int = -1) -> None:
        """Add songs to a playlist, in front of library row `before` (-1 means the end)."""
        store = self.playlist_store
        if store is None:
            return
        if self.side_model.is_locked(name):
            self.show_status(f"'{name}' is locked")
            return
        paths = self.relative_paths(rows)
        anchor = self.relative_paths([before])
        try:
            added = store.add_tracks(name, paths, anchor[0] if anchor else None)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot add to playlist", str(error))
            return
        if added == len(paths):
            self.show_status(f"Added {self.describe_songs(rows)} to {name}")
        elif added == 0:
            what = self.describe_songs(rows)
            self.show_status(f"{what} {'is' if len(rows) == 1 else 'are'} already in {name}")
        else:
            self.show_status(f"Added {added} songs to {name} ({len(paths) - added} already there)")
        self.playlist_edited(name)

    def add_to_shown_playlist(self, rows: list[int], before: int) -> None:
        """Songs from elsewhere were dropped on the playlist table."""
        if self.current_playlist is not None:
            self.add_to_playlist(self.current_playlist, rows, before)

    def rearrange_playlist(self, rows: list[int], before: int) -> None:
        """Songs were dragged within the playlist table."""
        store = self.playlist_store
        name = self.current_playlist
        if store is None or name is None:
            return
        anchor = self.relative_paths([before])
        try:
            store.move_tracks(name, self.relative_paths(rows), anchor[0] if anchor else None)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot update playlist", str(error))
            return
        self.playlist_edited(name, select=rows)

    def playlist_edited(self, name: str, select: list[int] | None = None) -> None:
        """A playlist's songs changed: update what is on screen and what plays next.

        The song table keeps its scroll position, and its selection (or, if
        `select` is given, those library rows become the selection).
        """
        self.refresh_playlist_entry(name)
        if name == self.current_playlist:
            bar = self.window.playlistSongList.verticalScrollBar()
            scroll = bar.value()
            keep = self.selected_library_rows("playlist") if select is None else select
            self.show_playlist(name)
            self.select_playlist_songs(keep)
            QTimer.singleShot(0, lambda: bar.setValue(scroll))
        if name == self.play_source and self.current_index >= 0:
            self.play_order.context_changed(self.play_source_rows())
            self.refresh_upcoming(keep_scroll=True)

    def refresh_playlist_entry(self, name: str) -> None:
        """Update a playlist's row in the side list."""
        self.side_model.set_locked(name, self.playlist_store.load(name).read_only)

    def select_playlist_songs(self, rows: list[int]) -> None:
        """Make these library rows the selection in the playlist table."""
        wanted = set(rows)
        selection = QItemSelection()
        first = QModelIndex()
        for position, row in enumerate(self.playlist_display_rows()):
            if row in wanted:
                index = self.playlist_proxy.index(position, 0)
                selection.select(index, index)
                first = first if first.isValid() else index
        model = self.window.playlistSongList.selectionModel()
        model.select(
            selection,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        # The selection needs an anchor, or the next Shift-click starts from the top.
        model.setCurrentIndex(first, QItemSelectionModel.SelectionFlag.NoUpdate)

    def remove_from_playlist(self, rows: list[int]) -> None:
        """Remove songs from the playlist on screen (their files are not touched)."""
        store = self.playlist_store
        name = self.current_playlist
        if store is None or name is None:
            return
        try:
            store.remove_tracks(name, self.relative_paths(rows))
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot update playlist", str(error))
            return
        self.playlist_edited(name, select=[])

    # ----- locking -----

    def show_lock_state(self, name: str | None, locked: bool) -> None:
        """Match the lock button at the top of the Playlists tab to a playlist."""
        button = self.window.playlistLockButton
        button.setEnabled(name is not None)
        button.setChecked(locked)
        button.setToolTip(
            "Locked: songs cannot be added or rearranged. Click to unlock."
            if locked
            else "Lock this playlist so songs cannot be added or rearranged."
        )

    def on_playlist_lock_clicked(self, checked: bool) -> None:
        if self.current_playlist is not None:
            self.set_playlist_locked(self.current_playlist, checked)

    def set_playlist_locked(self, name: str, locked: bool) -> None:
        store = self.playlist_store
        if store is None:
            return
        try:
            store.set_locked(name, locked)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot update playlist", str(error))
        locked = store.load(name).locked  # what was really saved
        self.refresh_playlist_entry(name)
        if name == self.current_playlist:
            self.playlist_tracks.locked = locked
            self.show_lock_state(name, locked)
        self.show_status(f"{'Locked' if locked else 'Unlocked'} '{name}'")

    # ----- importing and exporting -----

    def import_playlist(self) -> None:
        """Make a new playlist from the songs of an .m3u or .m3u8 file."""
        store = self.playlist_store
        if store is None:
            self.show_status("Choose a library first")
            return
        start_dir = QStandardPaths.writableLocation(QStandardPaths.MusicLocation)
        chosen, _ = QFileDialog.getOpenFileName(
            self.window, "Import playlist", start_dir, "Playlists (*.m3u8 *.m3u);;All files (*)"
        )
        if not chosen:
            return
        file = Path(chosen)
        try:
            listed = list(dict.fromkeys(read_m3u(file)))
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot import playlist", str(error))
            return

        rows_by_path = {path: row for row, path in enumerate(self.tracks)}
        rows = []
        for path in listed:
            row = rows_by_path.get(path, rows_by_path.get(path.resolve(), -1))
            if row >= 0:
                rows.append(row)
        rows = list(dict.fromkeys(rows))
        if not rows:
            QMessageBox.information(
                self.window,
                "Import playlist",
                "None of the songs in that playlist are in the current library.",
            )
            return

        name = self.ask_playlist_name("Import playlist", "Name of the new playlist:", file.stem)
        if name is None:
            return
        try:
            store.save(Playlist(name, self.relative_paths(rows)))
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot import playlist", str(error))
            return
        self.refresh_playlist_list(select=name)
        self.show_tab_of(self.window.playlistSongList)
        missing = len(listed) - len(rows)
        note = f" ({missing} not found in this library)" if missing else ""
        self.show_status(f"Imported {len(rows)} songs into {name}{note}")

    def export_playlist(self, name: str | None = None) -> None:
        """Save a playlist (by default the one on screen) as .m3u8 or .m3u."""
        name = name or self.current_playlist
        if name is None or self.playlist_store is None:
            self.show_status("Select a playlist to export first")
            return
        start_dir = Path(QStandardPaths.writableLocation(QStandardPaths.MusicLocation))
        chosen, used_filter = QFileDialog.getSaveFileName(
            self.window,
            "Export playlist",
            str(start_dir / f"{name}.m3u8"),
            f"{M3U8_FILTER};;{M3U_FILTER}",
        )
        if not chosen:
            return
        file = Path(chosen)
        if file.suffix.lower() not in (".m3u", ".m3u8"):
            suffix = ".m3u" if used_filter == M3U_FILTER else ".m3u8"
            file = file.with_name(file.name + suffix)

        tracks = []
        for row in self.playlist_rows(name):
            info = self.library_model.info(row)
            seconds = info.duration_ms // 1000 if info.duration_ms else -1
            label = f"{info.artist} - {info.title}" if info.artist else info.title
            tracks.append(M3uTrack(self.tracks[row], seconds, label))
        try:
            write_m3u(file, tracks)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot export playlist", str(error))
            return
        self.show_status(f"Exported {len(tracks)} songs to {file}")

    def playlist_display_rows(self) -> list[int]:
        """Library rows of the playlist on screen, in the order its table shows them."""
        return [
            self.playlist_tracks.library_row(row)
            for row in self.playlist_proxy.source_rows()
        ]

    def play_playlist_item(self, model_index: QModelIndex) -> None:
        """The user double-clicked a song in the playlist table."""
        playlist_row = self.playlist_proxy.mapToSource(model_index).row()
        row = self.playlist_tracks.library_row(playlist_row)
        if row >= 0:
            self.play_now(row, context=self.playlist_display_rows())

    def play_playlist(self, name: str) -> None:
        """Play a playlist from its first song (a random one if shuffle is on)."""
        if name != self.current_playlist:
            self.select_playlist(name)
        rows = self.playlist_display_rows()
        if not rows:
            self.show_status(f"'{name}' has no songs yet")
            return
        first = random.choice(rows) if self.play_order.shuffle else rows[0]
        self.play_now(first, context=rows)

    def on_side_list_double_clicked(self, model_index: QModelIndex) -> None:
        """Double-clicking plays a playlist and opens a pinned folder."""
        kind = self.side_model.kind(model_index)
        if kind == PLAYLIST:
            self.show_tab_of(self.window.playlistSongList)
            self.play_playlist(self.side_model.value(model_index))
        elif kind == PIN:
            self.open_pinned_folder(self.side_model.value(model_index))

    def select_in_playlist(self, row: int) -> None:
        """Highlight a library row in the playlist table, if it is in that playlist."""
        self.highlight_row(self.playlist_tracks, self.playlist_proxy, self.window.playlistSongList, row)

    def highlight_row(self, tracks, proxy, view, row: int) -> None:
        """Highlight a library row in a song table, if that table lists it."""
        position = tracks.playlist_row_of(row)
        if position < 0:
            return
        index = proxy.mapFromSource(tracks.index(position, 0))
        if index.isValid() and not self.has_multi_selection(view):
            view.setCurrentIndex(index)
            view.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)

    def show_tab_of(self, widget) -> None:
        """Switch to the tab that contains `widget`."""
        page = widget
        while page is not None:
            parent = page.parentWidget()
            if isinstance(parent, QStackedWidget) and isinstance(parent.parentWidget(), QTabWidget):
                parent.parentWidget().setCurrentWidget(page)
                return
            page = parent


    # ----- libraries -----

    def restore_libraries(self) -> None:
        """At startup: fill the drop-down and reopen the last used library."""
        library_db.migrate_old_libraries()
        libraries = library_db.list_libraries()
        ids = [library.id for library in libraries]
        last = self.settings.value("lastLibrary", "", type=str)
        select = last if last in ids else (ids[0] if ids else None)

        self.refresh_libraries(select)
        if self.current_library is not None:
            self.open_library(self.current_library, restore=True)
        else:
            self.show_status("Add a library to get started (File → Add Library…)")

    def refresh_libraries(self, select_id: str | None = None) -> None:
        """Re-read the saved libraries and rebuild the drop-down."""
        self.libraries = library_db.list_libraries()
        combo = self.window.libraryCombo
        combo.clear()
        for index, library in enumerate(self.libraries):
            combo.addItem(library.name, library.id)
            combo.setItemData(index, str(library.root), Qt.ItemDataRole.ToolTipRole)

        selected = next(
            (i for i, library in enumerate(self.libraries) if library.id == select_id),
            -1,
        )
        combo.setCurrentIndex(selected)
        self.current_library = self.libraries[selected] if selected >= 0 else None
        self.update_controls()

    def on_library_chosen(self, combo_index: int) -> None:
        """The user picked a library from the drop-down."""
        library_id = self.window.libraryCombo.itemData(combo_index)
        info = next((l for l in self.libraries if l.id == library_id), None)
        if info is not None:
            self.open_library(info)

    def open_library(self, info: LibraryInfo, restore: bool = False) -> None:
        """Show a library's saved tracks (scanning first if it never was).

        With `restore`, also put back the session saved when the app last quit.
        """
        self.player.unload()
        self.apply_library_volume(info)
        self.show_nothing_playing()
        self.current_library = info
        self.loaded_library = None

        try:
            loaded = library_db.load_library(info, self.shown_tag_names())
        except sqlite3.Error as error:
            self.tracks = []
            self.library_model.clear()
            self.reset_play_order()
            self.load_playlists()
            self.update_controls()
            self.update_library_stats()
            self.show_status(f"Cannot read library '{info.name}': {error}")
            return

        self.loaded_library = info
        self.tracks = loaded.paths
        self.library_model.set_library(loaded.paths, loaded.infos, loaded.covers)
        self.reset_play_order()
        self.load_playlists()
        self.settings.setValue("lastLibrary", info.id)
        self.update_controls()
        self.update_library_stats()

        if info.scanned_at is None:
            self.start_scan(info)
        elif not (restore and self.restore_session(info)):
            self.show_status(self.describe(info, len(self.tracks)))

    def reload_current_library(self) -> None:
        """Re-read the current library after a scan, without stopping playback."""
        info = self.current_library
        if info is None:
            return
        try:
            loaded = library_db.load_library(info, self.shown_tag_names())
        except sqlite3.Error as error:
            self.show_status(f"Cannot read library '{info.name}': {error}")
            return

        playing = (
            self.tracks[self.current_index]
            if 0 <= self.current_index < len(self.tracks)
            else None
        )
        self.loaded_library = info
        self.tracks = loaded.paths
        self.library_model.set_library(loaded.paths, loaded.infos, loaded.covers)
        self.play_order.reset(len(self.tracks))
        self.refresh_playlist_list(select=self.current_playlist)

        # If the playing track is still there, rebuild the order from it so
        # Next/Previous keep working; otherwise nothing is "playing" any more.
        if playing in self.tracks:
            row = self.tracks.index(playing)
            self.play_order.start_at(row, self.play_source_rows())
            self.current_index = row
            self.select_in_table(row)
        else:
            self.current_index = -1
        self.refresh_upcoming()
        self.update_library_stats()

    def add_library(self) -> None:
        start_dir = QStandardPaths.writableLocation(QStandardPaths.MusicLocation)
        folder = QFileDialog.getExistingDirectory(
            self.window, "Choose a music folder to add as a library", start_dir
        )
        if not folder:  # the user cancelled the dialog
            return

        try:
            info, created = library_db.add_library(Path(folder))
        except (OSError, sqlite3.Error) as error:
            QMessageBox.warning(
                self.window,
                "Cannot add library",
                f"The library could not be created:\n{error}",
            )
            return

        self.refresh_libraries(select_id=info.id)
        self.open_library(info)  # starts the first scan if it was never scanned
        if not created:
            self.show_status(f"'{info.name}' is already in your libraries")

    def remove_library(self) -> None:
        info = self.current_library
        if info is None:
            return
        answer = QMessageBox.question(
            self.window,
            "Remove library",
            f"Remove the library '{info.name}'?\n\n"
            "This only deletes MusicHive's saved data for it. "
            "Your music files are not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            library_db.remove_library(info)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot remove library", str(error))
            return

        self.player.unload()
        self.show_nothing_playing()
        self.loaded_library = None  # its database is gone; nothing to save at quit
        self.tracks = []
        self.library_model.clear()
        self.reset_play_order()
        self.load_playlists()
        self.settings.remove(self.last_playlist_key(info))
        self.settings.remove("lastLibrary")
        self.refresh_libraries(select_id=None)
        self.show_status(f"Removed library '{info.name}'")

    def update_library_stats(self) -> None:
        """Show the library's file count, size and length at the right of the status bar."""
        label = self.stats_label
        library = self.loaded_library
        if library is None or library.scanned_at is None:
            label.clear()
            label.setToolTip("")
            return
        try:
            stats = library_db.library_stats(library.db_path)
        except sqlite3.Error:
            label.clear()
            label.setToolTip("")
            return

        noun = "file" if stats.file_count == 1 else "files"
        label.setText(
            f"{stats.file_count} {noun}, {format_size(stats.total_bytes)}, "
            f"{format_long_duration(stats.total_ms)}"
        )
        label.setToolTip(
            f"{stats.file_count:,} {noun}\n"
            f"{stats.total_bytes:,} bytes\n"
            f"{format_long_duration(stats.total_ms, seconds=True)}"
        )

    def describe(self, info: LibraryInfo, track_count: int) -> str:
        scanned = info.scanned_at.replace("T", " ") if info.scanned_at else "never"
        return f"{info.name}: {track_count} tracks · last scanned {scanned}"

    def update_controls(self) -> None:
        """Enable or disable the library controls to match the current state."""
        w = self.window
        scanning = self.scan_task is not None
        has_library = self.current_library is not None

        w.libraryCombo.setEnabled(not scanning and bool(self.libraries))
        w.addLibraryButton.setEnabled(not scanning)
        w.actionAddLibrary.setEnabled(not scanning)
        w.rescanButton.setEnabled(has_library and not scanning)
        w.actionRescanLibrary.setEnabled(has_library and not scanning)
        w.actionRemoveLibrary.setEnabled(has_library and not scanning)

    # ----- scanning -----

    def rescan(self) -> None:
        if self.current_library is None:
            self.show_status("Add a library first (File → Add Library…)")
            return
        self.start_scan(self.current_library)

    def start_scan(self, info: LibraryInfo) -> None:
        if self.scan_task is not None:
            return  # one scan at a time
        self.scan_library = info
        self.scan_task = ScanTask(info, self.scan_signals)
        self.update_controls()
        self.show_status(f"Scanning '{info.name}'…")
        self.scan_pool.start(self.scan_task)

    def on_scan_progress(self, library_id: str, done: int, total: int) -> None:
        name = self.scan_library.name if self.scan_library else "library"
        self.show_status(f"Scanning '{name}': {done} of {total} tracks")

    def on_scan_finished(self, library_id: str, count: int) -> None:
        name = self.scan_library.name if self.scan_library else "library"
        self.scan_task = None
        self.scan_library = None

        current_id = self.current_library.id if self.current_library else None
        self.refresh_libraries(select_id=current_id)  # also refreshes the saved info
        if self.current_library is not None and self.current_library.id == library_id:
            self.reload_current_library()

        if count:
            self.show_status(f"Scan complete: {count} tracks in '{name}'")
        else:
            self.show_status(f"Scan complete: no supported audio files in '{name}'")

    def on_scan_failed(self, library_id: str, message: str) -> None:
        self.scan_task = None
        self.scan_library = None
        self.update_controls()
        self.show_status("Scan failed")
        QMessageBox.warning(self.window, "Scan failed", message)

    def on_scan_cancelled(self, library_id: str) -> None:
        self.scan_task = None
        self.scan_library = None
        self.update_controls()

    # ----- choosing what to play -----

    def play_index(self, row: int) -> None:
        """Play library row `row`. The play order must already point at it."""
        if not 0 <= row < len(self.tracks):
            return
        self.current_index = row
        self.position_ms = 0
        self.select_in_table(row)
        self.select_in_playlist(row)
        for page in self.group_pages.values():
            self.highlight_row(page.tracks, page.proxy, page.view, row)
        self.refresh_upcoming()
        self.player.play_file(self.tracks[row])

    def play_item(self, model_index: QModelIndex) -> None:
        """Called when the user double-clicks a song in the library table."""
        row = self.library_proxy.mapToSource(model_index).row()
        if row >= 0:
            self.play_now(row)

    def play_upcoming_item(self, model_index: QModelIndex) -> None:
        """Called when the user double-clicks a song in the Upcoming list."""
        self.play_now(-1, upcoming_offset=model_index.row())

    def on_play_pause(self) -> None:
        """One button for both: pause while playing, resume while paused,
        otherwise start the highlighted track (or the first one shown)."""
        if self.playback_state == "playing":
            self.player.pause()
        elif self.playback_state == "paused":
            self.player.play()
        else:
            selected = self.window.libraryList.currentIndex()
            row = self.library_proxy.mapToSource(selected).row() if selected.isValid() else -1
            shown = self.play_source_rows()
            if row not in shown:
                row = shown[0] if shown else -1
            if row < 0:
                self.show_status("Choose a library with music first")
                return
            self.play_order.start_at(row, shown)
            self.play_index(row)

    def play_next(self) -> None:
        if self.current_index < 0:
            shown = self.play_source_rows()
            if shown:  # nothing playing yet: start from the top of the list
                self.play_order.start_at(shown[0], shown)
                self.play_index(shown[0])
            return
        row = self.play_order.next()
        if row is None:
            self.show_status("This is the last track")
        else:
            self.play_index(row)

    def play_previous(self) -> None:
        """Restart the track if it is well underway, otherwise go back one."""
        if self.current_index < 0:
            return
        if self.position_ms - self.player.sound_start_ms > PREVIOUS_RESTART_THRESHOLD_MS:
            self.player.seek(0)  # keeps the current play/pause state
            self.discord.request_update()
            return
        row = self.play_order.previous()
        if row is None:  # already at the start of the play order
            self.player.seek(0)
            self.discord.request_update()
        else:
            self.play_index(row)

    # ----- seek slider -----

    def on_slider_moved(self, value: int) -> None:
        """While dragging, only update the time label; seek on release."""
        self.window.currentTimeLabel.setText(format_time(value))

    def on_slider_released(self) -> None:
        self.player.seek(self.window.progressSlider.value())
        self.discord.request_update()

    def on_slider_action(self, action: int) -> None:
        """Handle keyboard and wheel changes (arrows, PageUp/Down, Home/End).

        Slider-move actions come from clicks and drags; those are handled
        once, on release, by on_slider_released.
        """
        if action == QAbstractSlider.SliderAction.SliderMove.value:
            return
        slider = self.window.progressSlider
        if not slider.isSliderDown():
            self.player.seek(slider.sliderPosition())
            self.discord.request_update()

    # ----- player -> UI -----

    def on_track_started(self, track: Path) -> None:
        self.update_track_info(track)
        self.duration_ms = 0  # the real length arrives from the player in a moment
        self.begin_listening()
        self.discord.request_update()
        self.update_mpris()

    def on_track_ending(self) -> None:
        """A crossfade is due. If nothing comes next, the track just plays to its end."""
        if not self.play_order.ends_after_current():
            self.on_track_finished()

    def on_track_finished(self) -> None:
        stop_here = self.play_order.take_stop_after()
        row = self.play_order.next(ended_naturally=True)
        if row is None:
            self.show_status("Reached the end of the library")
        elif stop_here:
            # "End playback after track": the next track waits, paused.
            self.load_track_paused(row)
        else:
            self.play_index(row)

    def on_state_changed(self, state: str) -> None:
        self.playback_state = state
        w = self.window

        # Settle what was playing, then start or stop the listening clock.
        self.check_listening()
        self.listening.set_playing(state == "playing")
        if state == "playing":
            self.now_playing_timer.start(NOW_PLAYING_DELAY_MS)
        else:
            self.now_playing_timer.stop()

        # The single transport button shows what clicking it will do.
        if state == "playing":
            w.playButton.setIcon(self.icon_pause)
            w.playButton.setToolTip("Pause")
            self.show_status(f"Playing {w.currentTrackLabel.text()}")
        else:
            w.playButton.setIcon(self.icon_play)
            w.playButton.setToolTip("Play")
            if state == "paused":
                self.show_status("Paused")
            else:  # stopped
                self.position_ms = 0
                w.progressSlider.setValue(0)
                w.currentTimeLabel.setText(format_time(0))

        # Discord only shows a track once playback has really started.
        if state == "playing":
            self.discord_active = True
        elif state == "stopped":
            self.discord_active = False
        self.discord.request_update()

        # Discord only shows a track once playback has really started.
        if state == "playing":
            self.discord_active = True
        elif state == "stopped":
            self.discord_active = False
        self.discord.hold_position(state == "paused")
        self.discord.request_update()
        self.update_mpris()

    def on_position_changed(self, position_ms: int) -> None:
        self.position_ms = position_ms
        self.mpris.set_position(position_ms)
        self.lyrics_view.set_position(position_ms)
        slider = self.window.progressSlider
        if slider.isSliderDown():  # the user is dragging; don't fight them
            return
        slider.setValue(position_ms)
        self.window.currentTimeLabel.setText(format_time(position_ms))

    def on_duration_changed(self, duration_ms: int) -> None:
        self.duration_ms = duration_ms
        self.listening.set_duration(duration_ms)
        if self.listen_entry is not None:
            self.listen_entry["duration"] = duration_ms // 1000
        self.window.progressSlider.setRange(0, duration_ms)
        self.window.durationLabel.setText(format_time(duration_ms))
        self.discord.request_update()
        self.update_mpris()

    def on_player_error(self, message: str) -> None:
        self.show_nothing_playing()
        self.show_status(message)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("MusicHive")
    app.setDesktopFileName("musichive")  # must match musichive.desktop
    music_hive = MusicHive()
    # Menus and dialogs only see the application palette, so give them the
    # selection colours from player.ui instead of the system accent colour.
    palette = app.palette()
    for role in (
        QPalette.ColorRole.Highlight,
        QPalette.ColorRole.HighlightedText,
        QPalette.ColorRole.Accent,
    ):
        palette.setColor(role, music_hive.window.palette().color(role))
    app.setPalette(palette)
    app.aboutToQuit.connect(music_hive.shutdown)
    music_hive.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
