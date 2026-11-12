"""MusicHive: application startup and UI wiring."""

import datetime
import hashlib
import html
import os
import sqlite3
import sys
import time
from pathlib import Path
import random
import json
from dataclasses import dataclass

import shiboken6
from PySide6.QtCore import (
    QDir,
    QEvent,
    QFile,
    QIODevice,
    QItemSelection,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    QObject,
    QRect,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QActionGroup,
    QCursor,
    QDesktopServices,
    QFontDatabase,
    QIcon,
    QImage,
    QPainter,
    QPalette,
    QPixmap,
    QTextDocumentFragment,
)
from PySide6.QtDBus import QDBus, QDBusConnection, QDBusMessage
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSlider,
    QApplication,
    QColorDialog,
    QComboBox,
    QDialogButtonBox,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QProxyStyle,
    QSlider,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolTip,
    QInputDialog,
    QStackedWidget,
    QTabWidget,
    QWidget,
)

from src import library_db, theme
from src.artist_info import ArtistInfoService
from src.audio_player import AudioPlayer
from src.cover_view import SquareCover
from src.drag_label import LabelDragUiLoader
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
    PODCAST,
    PODCASTS_SECTION,
    SECTION,
    SECTION_OF,
    STATION,
    STATIONS_SECTION,
    SectionDragger,
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
    EMBEDDED,
    HAS_LYRICS_TAG,
    LRC,
    SAVE_TO_LRC,
    AutoLyricsSong,
    AutoTagSong,
    OnlineSearch,
    ask_auto_lyrics,
    ask_auto_tag_count,
)
from src.convert_files import ConvertFilesTool, ConvertSong
from src.device_sync import DeviceSyncPanel
from src.devices import DeviceEjector, DevicesTab, connected_devices, open_in_dolphin
from src.equaliser import (
    EqualiserWindow,
    equaliser_preset,
    equaliser_preset_names,
    load_equaliser,
)
from src.level_volume import LevelVolumeTool
from src.settings_transfer import export_settings, import_settings
from src.sound_settings import CROSSFADE_KEY, SoundSettingsTab, load_sound_settings
from src.organise_files import FileMove, OrganiseDialog, OrganiseResult
from src.tag_editor import TagEditorDialog
from src.tag_writer import SaveResult, TagJob, TagWriter
from src.mini_player import MiniPlayer
from src.now_playing import LyricsView, Visualizer, colours_of
from src.podcast_view import PodcastSettingsTab, PodcastsTab, format_date
from src.radio import RadioManager
from src.radio_stream import RadioStream, split_title
from src.radio_view import RadioSettingsTab, RadioTab
from src.sleep_timer import DEFAULT_FADE_SECONDS, FADE_KEY, SleepTimer
from src.podcasts import PodcastManager
from src.shortcuts import Shortcuts, ShortcutsSettingsTab
from src.lyrics_settings import LyricsSettings, LyricsSettingsTab, load_lyrics_settings
from src.number_field import NumberField
from src.type_ahead import (
    DEFAULT_RESET_SECONDS,
    FIND_RESET_KEY,
    add_type_ahead,
    set_reset_seconds,
)
from src.visualiser_settings import (
    VisualiserSettings,
    VisualiserSettingsTab,
    for_mini_player,
    load_visualiser_presets,
    load_visualiser_settings,
    visualiser_preset_names,
)
from src.song_settings import SoundPresets, chosen_presets
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
from src.auto_playlist import PLAYS_TAG, TIME_TAG, apply_limit, song_matches
from src.playlist_dialog import AutoPlaylistEditDialog, PlaylistDetails, PlaylistEditDialog
from src.playlists import Playlist, PlaylistStore


UI_DIR = Path(__file__).parent / "ui"
UI_FILE = UI_DIR / "player.ui"
DEVICE_CHECK_MS = 3000  # how often to look for devices being plugged in or removed
# The Application ID of your Discord application (public, not a secret).
# Leave it empty to disable the Discord menu item.
DISCORD_CLIENT_ID = "1568612126409498656"
DISCORD_PREUPLOAD_COUNT = 2  # how many of the next tracks get their cover uploaded early
# Your Last.fm API account (see the setup steps). The secret is not meant to be public.
SECRETS_FILE_NAME = "secrets"
# What a new secrets file holds: the keys to fill in (Settings, Online).
SECRETS_TEMPLATE = {"lastfm_api_key": "", "lastfm_api_secret": ""}


def load_secrets() -> dict:
    """Read API secrets from ~/Music/.musichive/secrets (kept out of the code)."""
    path = library_db.data_dir() / SECRETS_FILE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
# Before anything below opens a file in the data folder.
DATA_MOVE_RESULT = library_db.finish_data_move()
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
AUTO_LYRICS_UI_FILE = UI_DIR / "auto_lyrics.ui"
ORGANISE_UI_FILE = UI_DIR / "organise_files.ui"
EQUALISER_UI_FILE = UI_DIR / "equaliser.ui"
LEVEL_VOLUME_UI_FILE = UI_DIR / "level_volume.ui"
CONVERT_UI_FILE = UI_DIR / "convert_files.ui"
COVER_WINDOW_UI_FILE = UI_DIR / "cover_window.ui"
ABOUT_UI_FILE = UI_DIR / "about.ui"
MINI_PLAYER_UI_FILE = UI_DIR / "mini_player.ui"

# How long after a track starts before Last.fm is told what is playing.
NOW_PLAYING_DELAY_MS = 3000

# If the track is further in than this, Previous restarts it instead of
# going back to the previous track.
PREVIOUS_RESTART_THRESHOLD_MS = 5000
EPISODE_SAVE_SECONDS = 5  # how often the place in an episode is written down

GROUP_ICON_SIDE = 128  # pixels, the image at the top of the Artists and Albums tabs
# Starts a saved play source that is an artist or album. Playlist names cannot
# contain "/", so this can never be mistaken for one.
GROUP_SOURCE_PREFIX = "/"

SONGS_FILTER_OWNER = "songs"  # the Songs tab, in the saved filters
SONG_SEARCH_DELAY_MS = 200
SEARCH_CLEAR_ICON_SCALE = 0.55  # how much of its button the search bar's clear icon fills
HISTORY_SOURCE = "history"  # the History tab, as a list that can be played from
# Offered in an auto-playlist's rules even before a library has been rescanned.
COMMON_RULE_TAGS = (
    "album", "albumartist", "artist", "date", "genre", "plays", "rating", "time", "title"
)

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
    "all": "Repeat all songs",
    "one": "Repeat current song",
}

# Icon (a file name in assets/, without ".svg") for each context menu entry.
MENU_ICONS = {
    "play_now": "play",
    "play_next": "list-start",
    "add_to_queue": "list-plus",
    "add_to_end": "list-end",
    "add_to_playlist": "list-music",
    "cut": "scissors",
    "copy": "clipboard-copy",
    "paste": "clipboard-paste",
    "select_all": "list-check",
    "save_image": "save",
    "edit": "pencil",
    "delete_from_playlist": "x",
    "delete_file": "trash",
    "go_to": "library",
    "remove_from_upcoming": "minus",
    "end_after": "eject",
    "new_playlist": "plus",
    "edit_playlist": "pencil",
    "play_playlist": "play",
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
    "show_in_files": "folder",
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
SETTINGS_FILTER = "MusicHive settings (*.ini)"
DAYS_FILE = Path(__file__).parent / "days.json"
EMOJI_FONT_FILE = Path(__file__).parent / "assets" / "fonts" / "Twemoji.Mozilla.ttf"
EMOJI_FONTS = ["Twemoji Mozilla", "Segoe UI Emoji", "Noto Emoji"]  # the first with the emoji is used
NOTHING_TO_PASTE = "There are no songs from this library on the clipboard"


def todays_greeting() -> str:
    """ "Happy ...!" if today is listed in days.json, otherwise ""."""
    try:
        days = json.loads(DAYS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    today = datetime.date.today().strftime("%m-%d")
    names = [day["name"] for day in days if day["date"] == today]
    return f"Happy {' and '.join(names)}!" if names else ""


def load_ui(path: Path):
    """Load a Qt Designer .ui file and return the window it describes."""
    ui_file = QFile(str(path))
    if not ui_file.open(QIODevice.ReadOnly):
        raise RuntimeError(f"Cannot open UI file {path}: {ui_file.errorString()}")
    try:
        loader = LabelDragUiLoader()  # its views drag a short label, not the rows
        loader.setWorkingDirectory(QDir(str(theme.ui_dir(path.parent))))
        window = loader.load(ui_file)
    finally:
        ui_file.close()
    if window is None:
        raise RuntimeError(f"Failed to load {path}: {loader.errorString()}")
    theme.apply(window)
    return window


def asset_icon(name: str) -> QIcon:
    """An icon from the assets folder: asset_icon("play") loads assets/play.svg."""
    path = theme.asset_path(f"{name}.svg")
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


class FitColumnsToWidth(QObject):
    """Keeps a table's columns exactly as wide as the table, so it never scrolls sideways.

    Resizing the table scales every column. Dragging a column's edge makes the
    columns to its right give way or grow.
    """

    def __init__(self, view) -> None:
        super().__init__(view)
        self._view = view
        self._header = header = view.horizontalHeader()
        self._adjusting = False  # True while the widths are being changed from here
        self._fit_pending = False
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.viewport().installEventFilter(self)
        header.sectionResized.connect(self._on_section_resized)
        header.sectionCountChanged.connect(lambda _old, _new: self._schedule_fit())

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._schedule_fit()
        return False

    def _schedule_fit(self) -> None:
        # Later, so that several changes in a row (a whole new layout) fit only once.
        if not self._fit_pending:
            self._fit_pending = True
            QTimer.singleShot(0, self._fit)

    def _fit(self) -> None:
        self._fit_pending = False
        if self._view.isVisible():  # a hidden table has no real width yet
            self._scale(self._shown_sections(), self._view.viewport().width())

    def _shown_sections(self) -> list[int]:
        """The visible columns, from left to right."""
        header = self._header
        sections = (header.logicalIndex(position) for position in range(header.count()))
        return [section for section in sections if not header.isSectionHidden(section)]

    def _scale(self, sections: list[int], space: int) -> None:
        """Make these columns fill `space`, keeping their proportions."""
        header = self._header
        total = sum(header.sectionSize(section) for section in sections)
        if not sections or total <= 0 or total == space:
            return
        minimum = header.minimumSectionSize()
        widths = {
            section: max(minimum, round(header.sectionSize(section) * space / total))
            for section in sections[:-1]
        }
        widths[sections[-1]] = max(minimum, space - sum(widths.values()))
        self._adjusting = True
        for section, width in widths.items():
            header.resizeSection(section, width)
        self._adjusting = False

    def _on_section_resized(self, section: int, _old: int, new: int) -> None:
        if self._adjusting:
            return
        dragging = QApplication.mouseButtons() & Qt.MouseButton.LeftButton
        sections = self._shown_sections()
        if not dragging or not self._view.isVisible() or section not in sections:
            self._schedule_fit()
            return
        header = self._header
        position = sections.index(section)
        after = sections[position + 1 :]
        space = self._view.viewport().width() - sum(
            header.sectionSize(other) for other in sections[:position]
        )
        minimum = header.minimumSectionSize()
        width = max(minimum, min(new, space - minimum * len(after))) if after else space
        if width != new:
            self._adjusting = True
            header.resizeSection(section, width)
            self._adjusting = False
        self._scale(after, space - width)


def setup_library_table(view, model) -> None:
    """Give the library table its model, look and column widths."""
    view.setModel(model)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setAlternatingRowColors(True)
    view.setItemDelegate(RoundedRowDelegate(view))
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
    view.fit_columns = FitColumnsToWidth(view)


class RoundedRowDelegate(QStyledItemDelegate):
    """Tells each table cell where it is in its row, for the stylesheet's rounded row ends."""

    Position = QStyleOptionViewItem.ViewItemPosition

    def __init__(self, view) -> None:
        super().__init__(view)
        self._header = view.horizontalHeader()

    def _only_hidden(self, visual_columns) -> bool:
        header = self._header
        return all(header.isSectionHidden(header.logicalIndex(c)) for c in visual_columns)

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        visual = self._header.visualIndex(index.column())
        first = self._only_hidden(range(visual))
        last = self._only_hidden(range(visual + 1, self._header.count()))
        if first and last:
            option.viewItemPosition = self.Position.OnlyOne
        elif first:
            option.viewItemPosition = self.Position.Beginning
        elif last:
            option.viewItemPosition = self.Position.End
        else:
            option.viewItemPosition = self.Position.Middle


class ComboItemDelegate(QStyledItemDelegate):
    """Draws a dropdown's rows with the stylesheet's ::item rules, which Qt's own ignores.

    Qt's delegate also draws the separator lines, so that is done here too.
    """

    SEPARATOR_HEIGHT = 9

    @staticmethod
    def is_separator(index) -> bool:
        return index.data(Qt.ItemDataRole.AccessibleDescriptionRole) == "separator"

    def paint(self, painter, option, index) -> None:
        if not self.is_separator(index):
            # The text colours come from the theme: the dropdown is its own window,
            # and its palette does not reliably have the stylesheet's colours.
            option = QStyleOptionViewItem(option)
            option.palette.setColor(QPalette.ColorRole.Text, QColor(theme.colour(theme.TEXT)))
            option.palette.setColor(QPalette.ColorRole.HighlightedText, QColor(theme.on_highlight()))
            super().paint(painter, option, index)
            return
        y = option.rect.center().y()
        painter.save()
        painter.setPen(QColor(theme.colour("#433d47")))
        painter.drawLine(option.rect.left() + 8, y, option.rect.right() - 8, y)
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        if self.is_separator(index):
            return QSize(0, self.SEPARATOR_HEIGHT)
        return super().sizeHint(option, index)


class AssetIconStyle(QProxyStyle):
    """Replaces the icons Qt takes from the system theme with the app's own.

    These appear on dialog buttons, in message boxes and in the search box.
    """

    ICONS = {
        QStyle.StandardPixmap.SP_DialogOkButton: "check",
        QStyle.StandardPixmap.SP_DialogYesButton: "check",
        QStyle.StandardPixmap.SP_DialogApplyButton: "check",
        QStyle.StandardPixmap.SP_DialogCancelButton: "x",
        QStyle.StandardPixmap.SP_DialogNoButton: "x",
        QStyle.StandardPixmap.SP_DialogCloseButton: "x",
        QStyle.StandardPixmap.SP_DialogDiscardButton: "trash",
        QStyle.StandardPixmap.SP_DialogResetButton: "rotate-ccw",
        QStyle.StandardPixmap.SP_DialogOpenButton: "folder",
        QStyle.StandardPixmap.SP_DialogSaveButton: "save",
        QStyle.StandardPixmap.SP_DialogHelpButton: "circle-question-mark",
        QStyle.StandardPixmap.SP_MessageBoxInformation: "info",
        QStyle.StandardPixmap.SP_MessageBoxWarning: "triangle-alert",
        QStyle.StandardPixmap.SP_MessageBoxCritical: "circle-x",
        QStyle.StandardPixmap.SP_MessageBoxQuestion: "circle-question-mark",
        QStyle.StandardPixmap.SP_LineEditClearButton: "x",
    }

    def polish(self, target):
        if isinstance(target, QMenu) or (isinstance(target, QLabel) and target.inherits("QTipLabel")):
            # The corners outside a rounded edge must show what is behind it.
            target.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        elif isinstance(target, QComboBox):
            # Done here because Qt creates the list's window before polishing it,
            # and a window can only be made see-through before it is created.
            target.view().window().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            if not isinstance(target.itemDelegate(), ComboItemDelegate):
                target.setItemDelegate(ComboItemDelegate(target))
            # The chosen entry's text comes from the palette, which may not have
            # the stylesheet's colour; so do the list's, in its own window.
            if not theme.is_default():
                for widget in (target, target.view()):
                    palette = widget.palette()
                    # Not the disabled colours: a disabled dropdown stays dimmed.
                    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
                        for role in (
                            QPalette.ColorRole.ButtonText,
                            QPalette.ColorRole.Text,
                            QPalette.ColorRole.WindowText,
                        ):
                            palette.setColor(group, role, QColor(theme.colour(theme.TEXT)))
                        palette.setColor(
                            group, QPalette.ColorRole.HighlightedText, QColor(theme.on_highlight())
                        )
                    widget.setPalette(palette)
        return super().polish(target)

    def standardIcon(self, standardIcon, option=None, widget=None):
        name = self.ICONS.get(standardIcon)
        if name is None:
            return super().standardIcon(standardIcon, option, widget)
        return QIcon(str(theme.asset_path(f"{name}.svg")))


class JumpToClickStyle(AssetIconStyle):
    """Makes a left click on a slider's groove jump straight to that position."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_Slider_AbsoluteSetButtons:
            return Qt.MouseButton.LeftButton.value
        return super().styleHint(hint, option, widget, returnData)


class CenteredTabIconStyle(AssetIconStyle):
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


class DragSelectFilter(QObject):
    """Lets dragging across a song list select songs.

    A drag that starts on a selected song still moves the selection, as before.
    A drag that starts anywhere else selects the rows it passes over.
    """

    def __init__(self, view: QAbstractItemView) -> None:
        super().__init__(view)
        self._view = view
        view.viewport().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            view = self._view
            index = view.indexAt(event.position().toPoint())
            view.setDragEnabled(index.isValid() and view.selectionModel().isSelected(index))
        elif kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            self._view.setDragEnabled(True)
        return False


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
        # Podcast mode: an episode plays instead of the music, which waits.
        self.episode_id: int | None = None  # the episode playing (or last played), if any
        self.music_row_before = -1  # the song put aside for it, and where it was
        self.music_position_before = 0
        self._episode_saved_at = 0.0  # when its position was last written down
        # Radio mode: a station plays instead of the music.
        self.station_id: int | None = None
        self.radio_stream: RadioStream | None = None  # None while a station is played directly
        self.radio_title = ""  # "Artist - Title" as heard now
        self._radio_song_position = 0  # the player's position when that title was first heard
        self._radio_song_started_at = 0  # and the time, for scrobbling
        self.sleep_at_end = False  # the sleep timer waits for the song or episode to end

        # Which track plays next (play order, queue, shuffle, repeat)
        self.play_order = PlayOrder()

        # Playback
        self.playback_state = "stopped"  # "playing", "paused" or "stopped"
        self.restoring_session = False  # True while the last session's song is put back
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

        self.mini_player = MiniPlayer(load_ui(MINI_PLAYER_UI_FILE), w.styleSheet(), self.settings)

        # Left-click on a slider bar jumps to that position (instead of nudging).
        self._slider_style = JumpToClickStyle()
        w.progressSlider.setStyle(self._slider_style)
        w.volumeSlider.setStyle(self._slider_style)
        self.mini_player.slider.setStyle(self._slider_style)

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
        # Songs dragged from the other lists can be dropped between its rows,
        # and songs dragged within it are moved.
        w.upcomingList.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        w.upcomingList.setDragDropOverwriteMode(False)
        w.upcomingList.setDropIndicatorShown(True)
        w.upcomingList.doubleClicked.connect(self.play_upcoming_item)
        # Delete removes the selected songs (never the playing one) from the list.
        # The action only works while the list has focus (shortcutContext in player.ui).
        w.upcomingList.addAction(w.actionRemoveFromUpcoming)
        w.actionRemoveFromUpcoming.triggered.connect(
            lambda _checked=False: self.remove_from_upcoming(
                [o for o in self.selected_upcoming_offsets() if o > 0]
            )
        )
        w.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, w.upcomingDock)
        self.setup_dock_toggles()

        # Right-click menus and drag-selecting on the song lists
        self._drag_select_filters = []
        for view, source in (
            (w.libraryList, "library"),
            (w.upcomingList, "upcoming"),
            (w.playlistSongList, "playlist"),
            (w.artistSongList, "artist"),
            (w.albumSongList, "album"),
            (w.historyList, HISTORY_SOURCE),
        ):
            self._drag_select_filters.append(DragSelectFilter(view))
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
        # Podcasts and stations pinned to the sidebar, as "podcast:12" or "station:5".
        self.pinned_media: list[str] = self.load_json_setting("pinnedMedia")
        saved_order = self.load_json_setting("sideSectionOrder")
        if saved_order:
            self.side_model.section_order = saved_order
        w.sideList.setModel(self.side_model)
        self.playlist_selection = w.sideList.selectionModel()
        self.playlist_selection.currentChanged.connect(self.on_playlist_selected)
        w.sideList.clicked.connect(self.on_side_list_clicked)
        w.sideList.doubleClicked.connect(self.on_side_list_double_clicked)
        w.sideList.expanded.connect(lambda index: self.on_side_list_toggled(index, True))
        w.sideList.collapsed.connect(lambda index: self.on_side_list_toggled(index, False))
        w.sideList.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        w.sideList.customContextMenuRequested.connect(self.show_side_menu)
        # Dropping on a row always means "on this row", never "insert between": songs
        # are added to the playlist, and a section or pinned row takes the row's place.
        w.sideList.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        w.sideList.setDefaultDropAction(Qt.DropAction.MoveAction)
        w.sideList.setDragDropOverwriteMode(True)
        self.section_dragger = SectionDragger(w.sideList)  # sections cannot be selected

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
        self.queue_model.songs_dropped.connect(self.drop_into_upcoming, queued)
        self.queue_model.songs_moved.connect(self.move_in_upcoming, queued)
        self.side_model.drop_refused.connect(self.show_status)
        # Queued: the list is built again, which must not happen during the drop.
        self.side_model.section_moved.connect(self.on_side_section_moved, queued)
        self.playlist_proxy.drop_refused.connect(self.show_status)

        # Holding a dragged song over a tab opens that tab.
        tab_bar = w.podcastsTab.tabBar()
        self._tab_style = CenteredTabIconStyle()
        tab_bar.setStyle(self._tab_style)
        tab_bar.setChangeCurrentOnDrag(True)
        tab_bar.setAcceptDrops(True)

        w.actionSelectAll.triggered.connect(lambda _checked=False: self.select_all_in_table())
        w.playlistLockButton.clicked.connect(self.on_playlist_lock_clicked)
        w.playlistEditButton.setToolTip("Edit this playlist's title, image and description")
        w.playlistEditButton.clicked.connect(lambda _checked=False: self.edit_playlist())
        self.playlist_description = ElidedLabel(w.playlistsTabDescription)
        self.tab_icon_animation: IconAnimation | None = None

        # Track info panel (filled whenever a track starts playing)
        self.cover_view = SquareCover(w.trackInfoCoverImg, asset_icon("eighth-notes"))
        self.full_cover: QImage | None = None  # the playing track's cover, at full size
        self.full_cover_data: bytes | None = None  # the same cover, as stored in the file
        self.track_audio_format = ""  # for example "FLAC 44.1 kHz @ 906k Stereo"
        self.cover_window = None  # created the first time a cover is opened
        self._cover_click = ClickFilter(w.trackInfoCoverImg)
        self._cover_click.clicked.connect(self.show_full_cover)
        self.visualizer = Visualizer(w.nowPlayingVisualizer)
        self.player.audio_buffer_ready.connect(self.visualizer.add_buffer)
        self.lyrics_view = LyricsView(w.nowPlayingLyrics)
        self.lyrics_view.set_style(load_lyrics_settings(self.settings))
        w.nowPlayingLyrics.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        w.nowPlayingLyrics.customContextMenuRequested.connect(self.show_lyrics_menu)
        for widget in (
            w.trackInfoTitle,
            w.trackInfoArtistAlbumArtist,
            w.trackInfoAlbumYear,
            w.trackInfoFile,
            w.trackInfoCoverImg,
        ):
            widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            widget.customContextMenuRequested.connect(
                lambda pos, widget=widget: self.show_playing_track_menu(widget, pos)
            )
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
        self.online_search.auto_lyrics_progress.connect(self.on_auto_lyrics_progress)
        self.online_search.auto_lyrics_finished.connect(self.on_auto_lyrics_finished)
        self.online_search.auto_lyrics_checking.connect(
            lambda done, total: self.show_status(
                f"Checking which songs need lyrics: {done} of {total}…"
            )
        )
        self.online_search.auto_lyrics_waiting.connect(
            lambda seconds: self.show_status(
                f"LRCLIB asks to slow down: waiting {seconds} seconds, then carrying on…"
            )
        )
        self.auto_lyrics_library: LibraryInfo | None = None  # set while lyrics are looked up
        self.auto_lyrics_save_to = SAVE_TO_LRC
        self.auto_lyrics_text = w.actionAutoLyrics.text()
        w.actionAutoLyrics.triggered.connect(lambda _checked=False: self.on_auto_lyrics_clicked())
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

        self.settings_window = None  # created the first time it is opened
        self.restart_requested = False  # start again after quitting (a new look)

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
        # The presets of the playing song or playlist in use; empty means the
        # equaliser and visualiser are as the user set them.
        self.active_presets = SoundPresets()
        self.player.set_sound_settings(load_sound_settings(self.settings))
        self.equaliser_window: EqualiserWindow | None = None  # made when first opened
        w.equalizerButton.clicked.connect(lambda _checked=False: self.show_equaliser())
        self.setup_menu_actions()
        self.update_volume_ui()
        w.volumeSlider.valueChanged.connect(self.on_volume_changed)
        w.muteButton.toggled.connect(self.on_mute_toggled)
        self._volume_wheel = VolumeWheelFilter(w.volumeSlider)
        w.volumeSlider.installEventFilter(self._volume_wheel)
        w.muteButton.installEventFilter(self._volume_wheel)
        self.setup_mini_player()
        # After every action exists: the user's own shortcuts replace player.ui's.
        self.shortcuts = Shortcuts(w, self.settings)
        self.setup_podcasts()
        self.setup_radio()
        self.setup_pinned_media()
        self.setup_sleep_timer()
        self.type_ahead = add_type_ahead(w)  # typing in a list or table finds a song
        set_reset_seconds(self.settings.value(FIND_RESET_KEY, DEFAULT_RESET_SECONDS, type=float))

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
        # Only hidden if there is a tray icon to bring the window back with.
        stays_hidden = (
            self.settings.value("startMinimized", False, type=bool)
            and self.window.actionMinimizeToTray.isChecked()
            and TrayMinimizer.is_available()
        )
        if not stays_hidden:
            self.window.show()
        self.window.actionMiniPlayer.setChecked(self.settings.value("miniPlayer", False, type=bool))
        greeting = todays_greeting()
        if greeting:
            self.show_status(greeting)
        if DATA_MOVE_RESULT is not None:
            moved, message = DATA_MOVE_RESULT
            if moved:
                self.show_status(message)
            else:
                QMessageBox.warning(self.window, "Data folder", message)

    @staticmethod
    def quit_app() -> None:
        """End the program, also if the window would rather hide itself to the tray."""
        # Not quit(), which gives up when a window refuses to close.
        QApplication.exit()

    def show_settings(self) -> None:
        if self.settings_window is None:
            self.settings_window = d = load_dialog(SETTINGS_UI_FILE, self.window)
            self.sound_settings_tab = SoundSettingsTab(
                d, self.settings, self.on_sound_settings_changed
            )
            self.visualiser_settings_tab = VisualiserSettingsTab(
                d, self.settings, self.set_visualiser_style
            )
            self.shortcuts_settings_tab = ShortcutsSettingsTab(d, self.shortcuts)
            self.podcast_settings_tab = PodcastSettingsTab(d, self.podcasts)
            self.radio_settings_tab = RadioSettingsTab(d, self.radio)
            self._sleep_fade = NumberField(d.playbackSleepFade)
            self._sleep_fade.setValue(self.settings.value(FADE_KEY, DEFAULT_FADE_SECONDS, type=int))
            self._sleep_fade.valueChanged.connect(lambda value: self.settings.setValue(FADE_KEY, value))
            self.lyrics_settings_tab = LyricsSettingsTab(
                d, self.settings, self.set_lyrics_style
            )
            d.visualiserMiniPlayer.setChecked(self.mini_visualizer.enabled)
            d.visualiserMiniPlayer.toggled.connect(self.set_mini_visualizer)
            self._find_reset = NumberField(d.generalFindReset)
            self._find_reset.setValue(
                self.settings.value(FIND_RESET_KEY, DEFAULT_RESET_SECONDS, type=float)
            )
            self._find_reset.valueChanged.connect(self.set_find_reset)
            self.connect_settings_tabs(d)
            d.settingsButtonBox.rejected.connect(d.reject)
            # Enter confirms a typed number; it must not press Close as well.
            d.settingsButtonBox.button(QDialogButtonBox.StandardButton.Close).setAutoDefault(False)
        else:
            self.visualiser_settings_tab.reload_flags()  # flag files may have been added
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def connect_settings_tabs(self, d) -> None:
        """Connect the settings window's controls that mirror menu items."""
        w = self.window
        for checkbox, action in (
            (d.playbackShuffle, w.actionShuffle),
            (d.playbackMiniPlayer, w.actionMiniPlayer),
            (d.soundVolumeAdjustment, w.actionVolumeAdjustment),
            (d.lastFmScrobble, w.actionScrobble),
            (d.onlineDiscordPresence, w.actionDiscordPresence),
            (d.generalMinimizeToTray, w.actionMinimizeToTray),
            (d.generalShowUpcoming, w.actionShowUpcoming),
            (d.generalShowLibrary, w.actionShowLibrary),
            (d.generalShowPlayer, w.actionShowPlayer),
        ):
            if not action.isEnabled():  # no Discord application id, or no system tray
                checkbox.setEnabled(False)
                checkbox.setToolTip(action.toolTip())
            checkbox.setChecked(action.isChecked())
            checkbox.toggled.connect(action.setChecked)
            action.toggled.connect(checkbox.setChecked)

        d.playbackRepeat.setCurrentIndex(PlayOrder.REPEAT_MODES.index(self.play_order.repeat))
        d.playbackRepeat.activated.connect(
            lambda index: self.set_repeat_mode(PlayOrder.REPEAT_MODES[index])
        )
        d.generalStartMinimized.setChecked(self.settings.value("startMinimized", False, type=bool))
        d.generalStartMinimized.toggled.connect(
            lambda enabled: self.settings.setValue("startMinimized", enabled)
        )
        d.generalStartMinimized.setEnabled(w.actionMinimizeToTray.isChecked())
        w.actionMinimizeToTray.toggled.connect(d.generalStartMinimized.setEnabled)
        for button, action in (
            (d.librariesAddButton, w.actionAddLibrary),
            (d.librariesRescanButton, w.actionRescanLibrary),
            (d.librariesRemoveButton, w.actionRemoveLibrary),
            (d.onlineLastFmButton, w.actionAudioScrobbleLastFm),
        ):
            button.clicked.connect(lambda _checked=False, a=action: a.trigger())

        self.connect_interface_tab(d)
        self.show_data_folder()
        d.generalDataFolderButton.clicked.connect(lambda _checked=False: self.choose_data_folder())
        d.soundEqualiserButton.clicked.connect(lambda _checked=False: self.show_equaliser())
        d.backupImportButton.clicked.connect(lambda _checked=False: self.import_settings_file())
        d.backupExportButton.clicked.connect(lambda _checked=False: self.export_settings_file())
        d.onlineSecretsButton.clicked.connect(lambda _checked=False: self.open_secrets_file())

    def open_secrets_file(self) -> None:
        """Settings, Online: open the secrets file, made with empty keys if it is missing."""
        path = library_db.data_dir() / SECRETS_FILE_NAME
        try:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(SECRETS_TEMPLATE, indent=2) + "\n", encoding="utf-8")
                os.chmod(path, 0o600)  # only you can read it
        except OSError as error:
            QMessageBox.warning(self.window, "Secrets file", f"Cannot make the file: {error}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.information(
                self.window, "Secrets file", f"Open this file in a text editor:\n{path}"
            )

    def connect_interface_tab(self, d) -> None:
        """Settings, Interface: dark or light mode and the highlight colour.
        Both are used from the next start, so the tab says so once they change."""
        d.interfaceMode.setCurrentIndex(theme.MODES.index(theme.saved_mode(self.settings)))
        d.interfaceMode.activated.connect(self.set_interface_mode)
        d.interfaceHighlight.editingFinished.connect(
            lambda: self.set_highlight_colour(d.interfaceHighlight.text())
        )
        d.interfaceHighlightChoose.clicked.connect(lambda _checked=False: self.choose_highlight_colour())
        d.interfaceHighlightReset.clicked.connect(
            lambda _checked=False: self.set_highlight_colour(
                theme.default_highlight(theme.saved_mode(self.settings))
            )
        )
        self.show_interface_settings()

    def show_interface_settings(self) -> None:
        d = self.settings_window
        highlight = theme.saved_highlight(self.settings)
        d.interfaceHighlight.setText(highlight)
        d.interfaceHighlightSwatch.setStyleSheet(
            f"background-color: {highlight}; border-radius: 12px;"
        )
        d.interfaceHighlightReset.setEnabled(
            highlight != theme.default_highlight(theme.saved_mode(self.settings))
        )
        changed = (
            theme.saved_mode(self.settings) != theme.active_mode()
            or highlight != theme.active_highlight()
        )
        d.interfaceRestartNote.setVisible(changed)

    def set_interface_mode(self, index: int) -> None:
        mode = theme.MODES[index]
        if mode == theme.saved_mode(self.settings):
            return
        self.settings.setValue(theme.MODE_KEY, mode)
        self.show_interface_settings()
        self.offer_restart()

    def set_highlight_colour(self, text: str) -> None:
        """Save a typed or chosen highlight colour; anything else is put back."""
        colour = theme.valid_highlight(text)
        if colour is None:
            self.show_status(f"'{text}' is not a colour: type it as #rrggbb")
        elif colour != theme.saved_highlight(self.settings):
            theme.save_highlight(self.settings, colour)
            self.show_interface_settings()
            self.offer_restart()
            return
        self.show_interface_settings()

    def offer_restart(self) -> None:
        """A new look was chosen: ask whether to restart now to see it."""
        if not self.settings_window.interfaceRestartNote.isVisible():
            return  # back to the look in use, so nothing to restart for
        answer = QMessageBox.question(
            self.settings_window,
            "Restart MusicHive",
            "The new look is used once MusicHive restarts. Restart now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.restart_requested = True
            self.quit_app()

    def choose_highlight_colour(self) -> None:
        d = self.settings_window
        current = QColor(theme.saved_highlight(self.settings))
        chosen = QColorDialog.getColor(current, d, "Highlight colour")
        if chosen.isValid():
            self.set_highlight_colour(chosen.name())

    def show_data_folder(self) -> None:
        d = self.settings_window
        d.generalDataFolder.setText(str(library_db.data_dir()))
        pending = library_db.pending_data_move()
        d.generalDataFolderPending.setText(
            f"It will move to {pending} the next time MusicHive starts." if pending else ""
        )

    def choose_data_folder(self) -> None:
        """Settings: pick a new data folder. The move happens at the next start."""
        d = self.settings_window
        chosen = QFileDialog.getExistingDirectory(d, "Choose an empty folder for MusicHive's data")
        if not chosen:
            return
        target = Path(chosen)
        if target == library_db.data_dir():
            library_db.request_data_move(target)  # cancels a move that was waiting
            self.show_data_folder()
            return
        problem = library_db.check_new_data_folder(target)
        if problem is not None:
            QMessageBox.warning(d, "Data folder", problem)
            return
        library_db.request_data_move(target)
        self.show_data_folder()
        QMessageBox.information(
            d,
            "Data folder",
            f"Everything in the data folder will be moved to:\n{target}\n\n"
            "This happens the next time MusicHive starts, so please restart it.",
        )

    def setup_menu_actions(self) -> None:
        """Connect the Edit and Playback menu items that mirror other controls."""
        w = self.window
        w.actionCut.triggered.connect(lambda _checked=False: self.on_clipboard_action("cut"))
        w.actionCopy.triggered.connect(lambda _checked=False: self.on_clipboard_action("copy"))
        w.actionPaste.triggered.connect(lambda _checked=False: self.on_clipboard_action("paste"))
        w.actionExportSettings.triggered.connect(lambda _checked=False: self.export_settings_file())
        w.actionImportSettings.triggered.connect(lambda _checked=False: self.import_settings_file())
        w.actionEqualiser.triggered.connect(lambda _checked=False: self.show_equaliser())
        w.addAction(w.actionMute)  # in no menu, so it needs the window to hear its keys
        w.actionMute.triggered.connect(lambda _checked=False: w.muteButton.toggle())

        w.actionShuffle.setChecked(w.shuffleButton.isChecked())
        w.actionShuffle.toggled.connect(w.shuffleButton.setChecked)
        w.shuffleButton.toggled.connect(w.actionShuffle.setChecked)
        w.actionRepeat.triggered.connect(lambda _checked=False: self.cycle_repeat())
        w.actionStopAfterCurrentSong.setEnabled(False)  # until something is playing
        w.actionStopAfterCurrentSong.triggered.connect(
            lambda _checked=False: self.toggle_stop_after(0)
        )

        w.actionCrossfade.setChecked(load_sound_settings(self.settings).crossfade)
        w.actionCrossfade.toggled.connect(self.on_crossfade_toggled)

        w.actionScrobble.setChecked(self.settings.value("lastfm/scrobbling", True, type=bool))
        w.actionScrobble.toggled.connect(
            lambda enabled: self.settings.setValue("lastfm/scrobbling", enabled)
        )

        adjust_volume = self.settings.value("volumeAdjustment", True, type=bool)
        w.actionVolumeAdjustment.setChecked(adjust_volume)
        self.player.set_level_enabled(adjust_volume)
        w.actionVolumeAdjustment.toggled.connect(self.on_volume_adjustment_toggled)

    def on_sound_settings_changed(self, sound) -> None:
        self.player.set_sound_settings(sound)
        self.window.actionCrossfade.setChecked(sound.crossfade)

    def on_crossfade_toggled(self, enabled: bool) -> None:
        if self.settings_window is not None:
            self.settings_window.soundCrossfade.setChecked(enabled)  # this saves and applies it
            return
        self.settings.setValue(CROSSFADE_KEY, enabled)
        self.player.set_sound_settings(load_sound_settings(self.settings))

    def on_volume_adjustment_toggled(self, enabled: bool) -> None:
        self.settings.setValue("volumeAdjustment", enabled)
        self.player.set_level_enabled(enabled)

    def on_clipboard_action(self, name: str) -> None:
        """Cut, Copy or Paste: on the text being used, otherwise on songs."""
        focus = QApplication.focusWidget()
        text_action = getattr(focus, name, None)
        if name == "copy" and isinstance(focus, QLabel) and focus.hasSelectedText():
            self.copy_text(focus.selectedText(), "Text copied")
        elif callable(text_action):
            text_action()
        elif name == "paste":
            self.paste_songs()
        else:
            self.copy_songs(cut=name == "cut")

    def copy_songs(self, cut: bool) -> None:
        """Edit menu: copy or cut the songs selected in the list being used."""
        w = self.window
        if w.upcomingList.hasFocus():
            offsets = self.selected_upcoming_offsets()
            if not offsets:
                self.show_status("Select some songs first")
            elif cut:
                self.cut_from_upcoming([offset for offset in offsets if offset > 0])
            else:
                self.copy_rows(self.selected_library_rows("upcoming"))
            return
        rows = self.selected_song_rows()
        if not rows:
            self.show_status("Select some songs first")
        elif not cut:
            self.copy_rows(rows)
        elif (
            w.playlistSongList.isVisible()
            and self.shows_normal_playlist()
            and not self.side_model.is_locked(self.current_playlist)
        ):
            self.cut_songs(rows)
        else:
            self.show_status("Songs can only be cut from an unlocked normal playlist or Upcoming")

    def paste_songs(self) -> None:
        """Edit menu: paste into the list being used."""
        if self.window.upcomingList.hasFocus():
            self.paste_into("upcoming")
        elif not self.window.playlistSongList.isVisible() or self.current_playlist is None:
            self.show_status("Open a playlist to paste songs into")
        elif not self.shows_normal_playlist():
            self.show_status("Songs cannot be pasted into an auto playlist")
        else:
            self.paste_into("playlist")

    def copy_text(self, text: str, message: str) -> None:
        QApplication.clipboard().setText(text)
        self.show_status(message)

    def put_songs_on_clipboard(self, rows: list[int]) -> None:
        """Put songs on the clipboard as files, so other programs can paste them too."""
        data = QMimeData()
        data.setUrls([QUrl.fromLocalFile(str(self.tracks[row])) for row in rows])
        QApplication.clipboard().setMimeData(data)

    def clipboard_rows(self) -> list[int]:
        """The library rows of the songs on the clipboard."""
        data = QApplication.clipboard().mimeData()
        urls = data.urls() if data is not None and data.hasUrls() else []
        if not urls:
            return []
        row_of = {path: row for row, path in enumerate(self.tracks)}
        paths = (Path(url.toLocalFile()) for url in urls if url.isLocalFile())
        return [row_of[path] for path in paths if path in row_of]

    def copy_rows(self, rows: list[int]) -> None:
        self.put_songs_on_clipboard(rows)
        self.show_status(f"Copied {self.describe_songs(rows)}")

    def cut_songs(self, rows: list[int]) -> None:
        """Copy songs and remove them from the playlist on screen."""
        self.put_songs_on_clipboard(rows)
        self.remove_from_playlist(rows)
        self.show_status(f"Cut {self.describe_songs(rows)}")

    def cut_from_upcoming(self, offsets: list[int]) -> None:
        """Copy these entries of the Upcoming list, and take them out of it."""
        if not offsets:
            self.show_status("The playing song cannot be removed")
            return
        model = self.queue_model
        rows = [model.library_row(model.index(offset, 0)) for offset in sorted(offsets)]
        self.put_songs_on_clipboard(rows)
        self.remove_from_upcoming(offsets)
        self.show_status(f"Cut {self.describe_songs(rows)}")

    def paste_into(self, source: str, before: int = -1) -> None:
        """Add the songs on the clipboard to the Upcoming list or the playlist on screen."""
        rows = self.clipboard_rows()
        if not rows:
            self.show_status(NOTHING_TO_PASTE)
        elif source == "upcoming":
            self.queue_tracks(rows, play_next=False)
        else:
            self.add_to_playlist(self.current_playlist, rows, before)

    def export_settings_file(self) -> None:
        start_dir = Path(QStandardPaths.writableLocation(QStandardPaths.HomeLocation))
        chosen, _filter = QFileDialog.getSaveFileName(
            self.window, "Export settings", str(start_dir / "musichive-settings.ini"), SETTINGS_FILTER
        )
        if not chosen:
            return
        file = Path(chosen) if chosen.lower().endswith(".ini") else Path(f"{chosen}.ini")
        try:
            export_settings(self.settings, file)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot export settings", str(error))
            return
        self.show_status(f"Settings exported to {file.name}")

    def import_settings_file(self) -> None:
        start_dir = QStandardPaths.writableLocation(QStandardPaths.HomeLocation)
        chosen, _filter = QFileDialog.getOpenFileName(
            self.window, "Import settings", start_dir, SETTINGS_FILTER
        )
        if not chosen:
            return
        try:
            import_settings(self.settings, Path(chosen))
        except ValueError as error:
            QMessageBox.warning(self.window, "Cannot import settings", str(error))
            return
        QMessageBox.information(
            self.window, "Settings imported", "Restart MusicHive to use the imported settings."
        )

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
        # A plain item, not the checkable action: the desktop draws this menu and
        # would leave an empty space for the checkbox.
        mini_item = QAction(asset_icon("picture-in-picture-2"), "Open Mini Player", menu)
        mini_item.triggered.connect(lambda _checked=False: w.actionMiniPlayer.toggle())
        w.actionMiniPlayer.toggled.connect(
            lambda shown: mini_item.setText("Close Mini Player" if shown else "Open Mini Player")
        )
        menu.insertAction(action, mini_item)  # above Quit, the last item
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

    def setup_mini_player(self) -> None:
        """Connect the mini player's controls to the same actions as the main window's."""
        w = self.window
        mini = self.mini_player
        controls = mini.window
        controls.miniPreviousButton.clicked.connect(lambda _checked=False: self.play_previous())
        controls.miniPlayButton.clicked.connect(lambda _checked=False: self.on_play_pause())
        controls.miniNextButton.clicked.connect(lambda _checked=False: self.play_next())
        controls.miniMuteButton.clicked.connect(lambda _checked=False: w.muteButton.toggle())
        controls.miniMuteButton.installEventFilter(self._volume_wheel)
        mini.follow(w.progressSlider)
        mini.slider.sliderReleased.connect(lambda: self.on_slider_released(mini.slider))
        mini.slider.actionTriggered.connect(
            lambda action: self.on_slider_action(action, mini.slider)
        )
        controls.customContextMenuRequested.connect(self.show_mini_player_menu)
        # 13: the panel's 14px corners, less its outline.
        self.mini_visualizer = Visualizer(
            controls.miniPlayerContents, corner_radius=13
        )
        self.player.audio_buffer_ready.connect(self.mini_visualizer.add_buffer)
        self.mini_visualizer.set_enabled(
            self.settings.value("miniPlayerVisualizer", True, type=bool)
        )
        self.set_visualiser_style(load_visualiser_settings(self.settings))
        mini.set_text_shadow(load_lyrics_settings(self.settings))
        mini.connect_action(w.actionMiniPlayer)
        w.actionMiniPlayer.toggled.connect(lambda on: self.settings.setValue("miniPlayer", on))
        self.update_volume_ui()

    def show_mini_player_menu(self, position) -> None:
        """Right-click menu of the mini player."""
        w = self.window
        controls = self.mini_player.window
        menu = QMenu(controls)
        close = menu.addAction(asset_icon("x"), "Close Mini Player")
        close.triggered.connect(lambda _checked=False: w.actionMiniPlayer.setChecked(False))
        big_player = menu.addAction(asset_icon("library"), "Open Big Player")
        big_player.triggered.connect(lambda _checked=False: self.raise_window())
        menu.addSeparator()
        # With the same icons as the main window's buttons, which show the current state.
        shuffle = menu.addAction(w.shuffleButton.icon(), "Shuffle")
        shuffle.triggered.connect(lambda _checked=False: w.shuffleButton.toggle())
        repeat = menu.addAction(w.repeatButton.icon(), "Repeat")
        repeat.triggered.connect(lambda _checked=False: self.cycle_repeat())
        shown = self.mini_visualizer.enabled
        visualiser = menu.addAction(
            asset_icon("audio-lines" if shown else "audio-lines-off"), "Visualiser"
        )
        visualiser.triggered.connect(lambda _checked=False: self.set_mini_visualizer(not shown))
        menu.exec(controls.mapToGlobal(position))

    def set_mini_visualizer(self, enabled: bool) -> None:
        self.mini_visualizer.set_enabled(enabled)
        self.settings.setValue("miniPlayerVisualizer", enabled)
        if self.settings_window is not None:
            self.settings_window.visualiserMiniPlayer.setChecked(enabled)

    def set_lyrics_style(self, style: LyricsSettings) -> None:
        """The text shadow (lyrics and mini player) and the "No lyrics" fade."""
        self.lyrics_view.set_style(style)
        self.mini_player.set_text_shadow(style)

    def set_find_reset(self, seconds: float) -> None:
        set_reset_seconds(seconds)
        self.settings.setValue(FIND_RESET_KEY, seconds)

    def set_visualiser_cover(self, cover: QImage | None) -> None:
        """The playing song's cover, for the visualisers' album art background and
        the colours taken from it (bars, background and lyrics)."""
        colours = colours_of(cover)  # worked out once, then kept (cover_colours.py)
        self.visualizer.set_cover(cover, colours)
        self.mini_visualizer.set_cover(cover, colours)
        self.lyrics_view.set_cover_colour(colours.lyrics if colours is not None else None)

    def set_visualiser_style(self, style: VisualiserSettings) -> None:
        """How the bars of both visualisers look (Settings, Visualiser)."""
        self.visualizer.set_style(style)
        self.mini_visualizer.set_style(for_mini_player(style))  # its own bars and background

    def on_minimize_to_tray_toggled(self, enabled: bool) -> None:
        self.settings.setValue("minimizeToTray", enabled)
        self.tray.set_enabled(enabled)

    def setup_dock_toggles(self) -> None:
        """Connect the View menu's dock items, and remember which docks are shown."""
        w = self.window
        for dock, action in (
            (w.upcomingDock, w.actionShowUpcoming),
            (w.libraryDock, w.actionShowLibrary),
            (w.controlsWidget, w.actionShowPlayer),
        ):
            dock.setTitleBarWidget(QWidget(dock))  # an empty one: no title bar
            key = f"dockVisible/{dock.objectName()}"
            visible = self.settings.value(key, True, type=bool)
            action.setChecked(visible)
            dock.setVisible(visible)
            action.toggled.connect(dock.setVisible)
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
        self.save_episode_position()
        self.stop_station()  # finishes a tape and a scrobble
        self.podcasts.shutdown()
        self.radio.shutdown()
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
        dialog = load_dialog(ABOUT_UI_FILE, self.window)
        dialog.aboutButtonBox.rejected.connect(dialog.reject)
        dialog.exec()

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
        self.mini_player.show_volume(w.muteButton.icon(), w.muteButton.toolTip())

    # ----- MPRIS (desktop widgets) -----

    def mpris_metadata(self) -> dict:
        """What MPRIS shows about the current track."""
        if self.station_id is not None:
            return self.radio_mpris_metadata()
        if self.episode_id is not None:
            return self.episode_mpris_metadata()
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
            CanSeek=metadata["mpris:trackid"][1] != NO_TRACK and self.station_id is None,
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
        self.mini_player.show_track(title, artist)

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
                self.update_playlists_using(PLAYS_TAG)

        scrobbling = self.window.actionScrobble.isChecked()
        if scrobbling and entry["artist"] and entry["title"]:  # Last.fm needs both
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
            scrobbling = self.window.actionScrobble.isChecked()
            if scrobbling and self.listen_entry["artist"] and self.listen_entry["title"]:
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
        self.update_playlists_using(PLAYS_TAG)


    # ----- Discord -----

    def build_discord_activity(self) -> dict | None:
        """What Discord should show right now, or None to show nothing."""
        if self.discord_active and self.station_id is not None:
            station = self.radio.store.station(self.station_id)
            if station is None:
                return None
            artist, song = split_title(self.radio_title)
            cover_url = ""
            if self.discord.is_ready and self.station_cover_key():
                cover_url = self.discord_covers.link_for(
                    self.radio.logo_file(station.id), self.station_cover_key(), is_image=True
                )
            return make_activity(
                title=song or self.radio_title or station.name,
                artist=artist or station.name,
                album=f"Radio: {station.name}",
                audio_format="",
                position_ms=self.position_ms,
                duration_ms=0,
                paused=self.playback_state == "paused",
                cover_url=cover_url,
            )
        if self.discord_active and self.episode_id is not None:
            episode = self.podcasts.store.episode(self.episode_id)
            podcast = self.podcasts.store.podcast(episode.podcast_id) if episode else None
            if episode is None or podcast is None:
                return None
            cover_url = ""
            if self.discord.is_ready:  # nothing is uploaded while Discord isn't running
                cover_url = self.discord_covers.link_for(
                    self.podcasts.cover_file(podcast.id), self.episode_cover_key(), is_image=True
                )
            return make_activity(
                title=episode.title,
                artist=podcast.title,
                album="Podcast",
                audio_format="",
                position_ms=self.position_ms,
                duration_ms=self.duration_ms or episode.duration_ms,
                paused=self.playback_state == "paused",
                cover_url=cover_url,
            )
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
            audio_format=self.track_audio_format,
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
        if self.station_id is not None:
            if cover_key == self.station_cover_key():
                self.discord.request_update()
            return
        if self.episode_id is not None:
            if cover_key == self.episode_cover_key():
                self.discord.request_update()
            return
        info = self.library_model.info(self.current_index)
        if info is not None and info.cover_key == cover_key:
            self.discord.request_update()

    def episode_cover_key(self) -> str:
        """Names the playing episode's podcast cover for the Discord uploads ("" for none).
        It changes when the podcast gets a new cover, so the new one is uploaded."""
        episode = self.podcasts.store.episode(self.episode_id) if self.episode_id else None
        if episode is None or not self.podcasts.cover_file(episode.podcast_id).is_file():
            return ""
        return f"podcast:{episode.podcast_id}:{self.podcasts.store.cover_url(episode.podcast_id)}"

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
        self.track_audio_format = details.audio_format
        # Rich text, like the other labels here: a selectable plain label loses
        # its stylesheet colour and size once it is clicked.
        w.trackInfoFile.setText(
            f'<span style="color: {theme.dimmed_text_on_panel()}; font-size: 8pt">'
            f"{html.escape(details.audio_format)}</span>"
            if details.audio_format
            else ""
        )
        self.update_window_title(details.title, details.artist)
        cover = QImage.fromData(details.cover) if details.cover else None
        self.full_cover = cover if cover is not None and not cover.isNull() else None
        self.set_visualiser_cover(self.full_cover)
        self.full_cover_data = details.cover if self.full_cover is not None else None
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
        self.update_playlists_using("rating")

    def update_playlists_using(self, tag: str) -> None:
        """A rating or play count changed: auto-playlists with a rule on it must look again."""
        for key in [key for key in self.auto_playlist_cache if f'"tag": "{tag}"' in key]:
            del self.auto_playlist_cache[key]
        store = self.playlist_store
        if store is None:
            return
        for name in {self.current_playlist, self.play_source} - {None}:
            playlist = store.load(name)
            if playlist.is_auto and any(rule["tag"] == tag for rule in playlist.rules):
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
        self.update_playlists_using("rating")
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
            label = self.cover_window.coverWindowImage
            label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            label.customContextMenuRequested.connect(self.show_cover_window_menu)
        window = self.cover_window
        # What the window keeps showing, even after the song changes.
        self.shown_cover = (image, self.full_cover_data, self.cover_file_name())
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

    def cover_file_name(self) -> str:
        """A file name, without extension, for the playing track's cover."""
        info = self.library_model.info(self.current_index)
        name = (info.album or info.title) if info is not None else ""
        return name.replace("/", "-") or "cover"

    def show_cover_window_menu(self, pos) -> None:
        """Right-click menu of the large cover window."""
        image, data, name = self.shown_cover
        label = self.cover_window.coverWindowImage
        menu = QMenu(label)
        add = self.add_menu_item
        add(menu, "Save image…", "save_image", lambda: self.save_cover(data, name))
        add(menu, "Copy image", "copy", lambda: self.copy_image(image))
        menu.exec(label.mapToGlobal(pos))

    def save_cover(self, data: bytes, name: str) -> None:
        """Save a cover exactly as it is stored, so it loses no quality."""
        if data.startswith(b"\x89PNG"):
            extension, kind = "png", "PNG image"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            extension, kind = "webp", "WebP image"
        else:
            extension, kind = "jpg", "JPEG image"
        start_dir = Path(QStandardPaths.writableLocation(QStandardPaths.PicturesLocation))
        chosen, _filter = QFileDialog.getSaveFileName(
            self.window, "Save image", str(start_dir / f"{name}.{extension}"), f"{kind} (*.{extension})"
        )
        if not chosen:
            return
        file = Path(chosen)
        if file.suffix.lower().lstrip(".") != extension:
            file = file.with_name(f"{file.name}.{extension}")
        try:
            file.write_bytes(data)
        except OSError as error:
            QMessageBox.warning(self.window, "Cannot save image", str(error))
            return
        self.show_status(f"Image saved as {file.name}")

    def track_info_link(self, text: str, field: str) -> str:
        """A link that looks like the label's normal text ("" if there is no text)."""
        if not text:
            return ""
        color = theme.colour(theme.TEXT)  # the label's palette may not have the stylesheet's colour
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
        self.set_visualiser_cover(None)
        self.full_cover_data = None
        self.track_audio_format = ""
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
        self.update_stop_after_action()
        if self.queue_model.rowCount() > 0:
            # The playing track is always the first row of the Upcoming list.
            view.setCurrentIndex(self.queue_model.index(0, 0))

        if keep_scroll:
            QTimer.singleShot(0, lambda: scroll_bar.setValue(scroll))
        else:
            view.scrollToTop()

    def update_stop_after_action(self) -> None:
        """Make the Playback menu's item match the playing track's stop marker."""
        action = self.window.actionStopAfterCurrentSong
        action.setEnabled(bool(self.play_order.upcoming_rows()))
        action.setChecked(self.play_order.stop_after_offset() == 0)

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
        self.window.actionRepeat.setChecked(mode != "off")
        if self.settings_window is not None:
            self.settings_window.playbackRepeat.setCurrentIndex(PlayOrder.REPEAT_MODES.index(mode))

    # ----- right-click menu -----

    def add_menu_item(self, target, label, icon_key, callback, enabled=True, tip=""):
        """Add one entry to a right-click menu."""
        action = target.addAction(self.menu_icons.get(icon_key, QIcon()), label)
        action.setEnabled(enabled)
        if tip:
            action.setToolTip(tip)
        action.triggered.connect(lambda _checked=False: callback())
        return action

    def add_playlist_submenu(self, menu: QMenu, rows: list[int], several: str = "") -> None:
        """The "Add to playlist" entry of a right-click menu."""
        names = self.playlist_store.names() if self.playlist_store else []
        submenu = menu.addMenu(self.menu_icons["add_to_playlist"], f"Add to playlist{several}")
        submenu.setToolTipsVisible(True)
        for name in names:
            locked = self.side_model.is_locked(name)
            self.add_menu_item(
                submenu, name.replace("&", "&&"), None,
                lambda n=name: self.add_to_playlist(n, rows),
                enabled=not locked,
                tip="This playlist is locked" if locked else "",
            )
        if names:
            submenu.addSeparator()
        self.add_menu_item(
            submenu, "New playlist…", None,
            lambda: self.new_playlist(add_rows=rows),
            enabled=self.playlist_store is not None,
        )

    def add_go_to_submenu(self, menu: QMenu, row: int, info) -> None:
        """The "Go to" entry of a right-click menu."""
        go_menu = menu.addMenu(self.menu_icons["go_to"], "Go to")
        go_menu.setToolTipsVisible(True)
        for label, field in GO_TO_FIELDS:
            has_value = field == "title" or bool(getattr(info, field))
            self.add_menu_item(
                go_menu, label, None, lambda f=field: self.go_to(row, f),
                enabled=has_value,
                tip="" if has_value else "This song has no such tag (rescan to read new ones)",
            )

    def show_playing_track_menu(self, widget, pos) -> None:
        """Right-click menu of the track info panel: about the song playing now."""
        row = self.current_index
        info = self.library_model.info(row)
        if info is None or not 0 <= row < len(self.tracks):
            return
        window = self.window
        menu = QMenu(window)  # not the label: its stylesheet would make the menu see-through
        menu.setToolTipsVisible(True)
        add = self.add_menu_item

        if widget is window.trackInfoCoverImg:
            image = self.full_cover
            add(
                menu, "Copy image", "copy", lambda: self.copy_image(image),
                enabled=image is not None,
                tip="" if image is not None else "This song has no cover",
            )
            data = self.full_cover_data
            name = self.cover_file_name()
            add(
                menu, "Save image…", "save_image", lambda: self.save_cover(data, name),
                enabled=data is not None,
                tip="" if data is not None else "This song has no cover",
            )
        else:
            plain = QTextDocumentFragment.fromHtml(widget.text()).toPlainText()
            text = widget.selectedText() if widget.hasSelectedText() else plain
            add(menu, "Copy text", "copy", lambda: self.copy_text(text, "Text copied"), enabled=bool(text))
        add(menu, "Copy song", "copy", lambda: self.copy_rows([row]))
        menu.addSeparator()

        add(menu, "Play next", "play_next", lambda: self.queue_tracks([row], play_next=True))
        add(menu, "Add to queue", "add_to_queue", lambda: self.queue_tracks([row], play_next=False))
        add(menu, "Add to end of queue", "add_to_end", lambda: self.queue_tracks([row], at_end=True))
        self.add_playlist_submenu(menu, [row])
        menu.addSeparator()

        scanning = self.scan_task is not None
        add(
            menu, "Edit…", "edit", lambda: self.edit_tags([row]),
            enabled=not scanning,
            tip="Wait for the scan to finish" if scanning else "",
        )
        self.add_go_to_submenu(menu, row, info)
        add(menu, "Show in File Manager", "show_in_files",
            lambda: self.show_in_file_manager([self.tracks[row]]))
        menu.addSeparator()

        marked = self.play_order.stop_after_offset() == 0
        add(
            menu,
            "Don't end playback after song" if marked else "End playback after song",
            "end_after",
            lambda: self.toggle_stop_after(0),
        )
        menu.exec(widget.mapToGlobal(pos))

    def copy_image(self, image: QImage) -> None:
        QApplication.clipboard().setImage(image)
        self.show_status("Image copied")

    def show_lyrics_menu(self, pos) -> None:
        """Right-click menu of the lyrics."""
        view = self.window.nowPlayingLyrics
        lyrics = self.lyrics_view.text
        if not lyrics:
            return
        item = view.itemAt(pos)
        line = item.text().strip() if item is not None else ""
        menu = QMenu(view)
        add = self.add_menu_item
        add(menu, "Copy line", "copy", lambda: self.copy_text(line, "Line copied"), enabled=bool(line))
        add(menu, "Copy all lyrics", "copy", lambda: self.copy_text(lyrics, "Lyrics copied"))
        menu.exec(view.viewport().mapToGlobal(pos))

    def shows_normal_playlist(self) -> bool:
        """True if the playlist on screen is one songs can be added to and removed from."""
        name = self.current_playlist
        return name is not None and not self.is_auto_playlist(name)

    def show_empty_list_menu(self, view, pos, source: str) -> None:
        """Right-click on the empty part of a song list."""
        menu = QMenu(view)
        menu.setToolTipsVisible(True)
        add = self.add_menu_item
        pasted = bool(self.clipboard_rows())
        paste_tip = "" if pasted else NOTHING_TO_PASTE
        if source == "upcoming":
            add(menu, "Paste", "paste", lambda: self.paste_into("upcoming"), enabled=pasted, tip=paste_tip)
        elif source == "playlist" and self.shows_normal_playlist():
            locked = self.side_model.is_locked(self.current_playlist)
            add(
                menu, "Paste", "paste", lambda: self.paste_into("playlist"),
                enabled=pasted and not locked,
                tip="This playlist is locked" if locked else paste_tip,
            )
        add(menu, "Select all", "select_all", view.selectAll, enabled=view.model().rowCount() > 0)
        menu.exec(view.viewport().mapToGlobal(pos))

    def show_track_menu(self, view, pos, source: str) -> None:
        """Right-click menu for a song. `source` is "library", "upcoming", "playlist",
        "artist", "album" or "history"."""
        index = view.indexAt(pos)
        if not index.isValid():
            self.show_empty_list_menu(view, pos, source)
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
        menu = QMenu(view)
        menu.setToolTipsVisible(True)

        add = self.add_menu_item
        add(menu, "Play now", "play_now", lambda: self.play_now(row, offset, context, group))
        add(
            menu, f"Play next{several}", "play_next",
            lambda: self.queue_tracks(rows, play_next=True),
        )
        add(
            menu, f"Add to queue{several}", "add_to_queue",
            lambda: self.queue_tracks(rows, play_next=False),
        )
        add(
            menu, f"Add to end of queue{several}", "add_to_end",
            lambda: self.queue_tracks(rows, at_end=True),
        )
        self.add_playlist_submenu(menu, rows, several)
        menu.addSeparator()

        # Hidden where it can never apply; greyed out while it temporarily cannot.
        in_playlist = source == "playlist" and self.shows_normal_playlist()
        locked = in_playlist and self.side_model.is_locked(self.current_playlist)
        lock_tip = "This playlist is locked" if locked else ""
        offsets = self.selected_upcoming_offsets() if source == "upcoming" else []
        removable = [o for o in (offsets if offset in offsets else [offset]) if o > 0]
        removable_count = f" ({len(removable)} songs)" if len(removable) > 1 else ""
        pasted = bool(self.clipboard_rows())
        paste_tip = "" if pasted else NOTHING_TO_PASTE

        if in_playlist:
            add(menu, f"Cut{several}", "cut", lambda: self.cut_songs(rows), enabled=not locked, tip=lock_tip)
        elif source == "upcoming":
            add(
                menu, f"Cut{removable_count}", "cut",
                lambda: self.cut_from_upcoming(removable),
                enabled=bool(removable),
                tip="" if removable else "The playing song cannot be removed",
            )
        add(menu, f"Copy{several}", "copy", lambda: self.copy_rows(rows))
        if in_playlist:
            add(
                menu, "Paste", "paste", lambda: self.paste_into("playlist", before=row),
                enabled=pasted and not locked, tip=lock_tip or paste_tip,
            )
        elif source == "upcoming":
            add(menu, "Paste", "paste", lambda: self.paste_into("upcoming"), enabled=pasted, tip=paste_tip)
        select_all = add(menu, "Select all", "select_all", view.selectAll)
        menu.addSeparator()

        add(
            menu, f"Edit…{several}", "edit",
            lambda: self.edit_tags(rows, self.track_menu_neighbours(source, context)),
            enabled=not scanning,
            tip="Wait for the scan to finish" if scanning else "",
        )
        self.add_go_to_submenu(menu, row, info)
        add(
            menu, f"Show in File Manager{several}", "show_in_files",
            lambda: self.show_in_file_manager([self.tracks[r] for r in rows]),
        )
        menu.addSeparator()

        if source == "upcoming":
            marked = self.play_order.stop_after_offset() == offset
            add(
                menu,
                "Don't end playback after song" if marked else "End playback after song",
                "end_after",
                lambda: self.toggle_stop_after(offset),
            )
            add(
                menu, f"Remove from upcoming{removable_count}", "remove_from_upcoming",
                lambda: self.remove_from_upcoming(removable),
                enabled=bool(removable),
                tip="" if removable else "The playing song cannot be removed",
            )
        if in_playlist:
            add(
                menu, f"Remove from playlist{several}", "delete_from_playlist",
                lambda: self.remove_from_playlist(rows),
                enabled=not locked, tip=lock_tip,
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

        chosen = menu.exec(view.viewport().mapToGlobal(pos))

        if source == "upcoming" and self.queue_model.rowCount() > 0 and chosen != select_all:
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

    def queue_tracks(self, rows: list[int], play_next: bool = False, at_end: bool = False) -> None:
        """Play next / Add to queue / Add to end of queue, keeping the songs in
        the order given."""
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
        elif at_end:
            for row in rows:
                self.play_order.add_to_end(row)
            self.show_status(f"Added to end of queue: {what}")
        else:
            for row in rows:
                self.play_order.add_to_queue(row)
            self.show_status(f"Added to queue: {what}")
        self.refresh_upcoming(keep_scroll=True)

    def drop_into_upcoming(self, rows: list[int], offset: int) -> None:
        """Songs were dropped on the Upcoming list, `offset` places down it."""
        rows = [row for row in rows if 0 <= row < len(self.tracks)]
        if not rows:
            return
        if self.current_index < 0:
            self.queue_tracks(rows)  # nothing is playing: the first song starts
            return
        self.play_order.insert_upcoming(offset, rows)
        what = self.library_model.info(rows[0]).title if len(rows) == 1 else f"{len(rows)} songs"
        self.show_status(f"Added to queue: {what}")
        self.refresh_upcoming(keep_scroll=True)

    def move_in_upcoming(self, offsets: list[int], offset: int) -> None:
        """Songs dragged within the Upcoming list were dropped `offset` places down it."""
        if self.play_order.move_upcoming(offsets, offset):
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
            self.show_status("Playback will end after that song")
        else:
            self.show_status("Playback will no longer end after that song")

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
        row, position_ms = self.current_index, self.position_ms
        if self.away_from_music:  # the music put aside for the episode or station
            row, position_ms = self.music_row_before, self.music_position_before
        try:
            if 0 <= row < len(self.tracks):
                rows, queued, order_position, stop_index = self.play_order.snapshot()
                library_db.save_session(
                    library.db_path,
                    SavedSession(
                        rel_path=self.relative_path(self.tracks[row]),
                        position_ms=position_ms,
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
        # Coming back paused at startup is not news, so it gets no status message.
        self.restoring_session = True
        try:
            return self.load_track_paused(row, session.position_ms)
        finally:
            self.restoring_session = False

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
        bar.group_menu_requested.connect(self.show_group_menu)
        return bar

    # ----- the tag editor -----

    def show_group_menu(self, rows: list[int], position) -> None:
        """Right-click menu of an entry in a filter list (an artist, an album, a genre...)."""
        menu = QMenu(self.window)
        menu.setToolTipsVisible(True)
        add = self.add_menu_item
        add(menu, "Play now", "play_now", lambda: self.play_rows(rows))
        add(menu, "Play next", "play_next", lambda: self.queue_tracks(rows, play_next=True))
        add(menu, "Add to queue", "add_to_queue", lambda: self.queue_tracks(rows, play_next=False))
        self.add_playlist_submenu(menu, rows)
        menu.addSeparator()
        add(menu, "Copy", "copy", lambda: self.copy_rows(rows))
        menu.addSeparator()
        scanning = self.scan_task is not None
        add(
            menu, "Edit…", "edit", lambda: self.edit_tags(rows),
            enabled=not scanning,
            tip="Wait for the scan to finish" if scanning else "",
        )
        menu.exec(position)

    def play_rows(self, rows: list[int]) -> None:
        """Start playing these songs, in this order."""
        self.set_play_source(None)
        self.source_group = None
        self.play_order.start_at(rows[0], rows)
        self.play_index(rows[0])

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
        editor = TagEditorDialog(
            TAG_EDIT_UI_FILE,
            self.window,
            paths,
            [read_editable_file(path) for path in paths],
            asset_icon("image"),
            asset_icon("star"),
            asset_icon("star_full"),
            self.online_search,
            [self.tracks[row] for row in dict.fromkeys(neighbours or [])],
            self.load_song_presets,
            equaliser_preset_names(self.settings),
            visualiser_preset_names(self.settings),
        )
        jobs = editor.run()
        if editor.preset_changes:
            self.save_song_presets(editor.preset_changes)
        if jobs:
            self.show_status(f"Saving tags of {len(jobs)} song{'' if len(jobs) == 1 else 's'}…")
            self.tag_writer.save_tags(jobs, library.root, library.db_path)

    def load_song_presets(self, paths: list[Path]) -> dict[Path, SoundPresets]:
        """The equaliser and visualiser presets of these songs (Edit, Settings)."""
        library = self.loaded_library
        if library is None:
            return {}
        by_rel_path = {self.relative_path(path): path for path in paths}
        try:
            stored = library_db.load_track_settings(library.db_path, list(by_rel_path))
        except sqlite3.Error:
            return {}
        return {by_rel_path[rel]: SoundPresets(*values) for rel, values in stored.items()}

    def save_song_presets(self, presets: dict[Path, SoundPresets]) -> None:
        library = self.loaded_library
        if library is None:
            return
        rows = {
            self.relative_path(path): (p.equaliser, p.visualiser, p.over_playlist)
            for path, p in presets.items()
        }
        try:
            library_db.save_track_settings(library.db_path, rows)
        except sqlite3.Error as error:
            self.show_status(f"Cannot save the song settings: {error}")
            return
        if 0 <= self.current_index < len(self.tracks) and self.tracks[self.current_index] in presets:
            self.apply_sound_presets()  # the playing song changed

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

    # ----- finding lyrics for many songs -----

    def on_auto_lyrics_clicked(self) -> None:
        """Tools menu: look up the lyrics of the selected songs, or stop if it is running."""
        if self.auto_lyrics_library is not None:
            self.online_search.stop_auto_lyrics()
            return
        library = self.loaded_library
        rows = self.selected_song_rows()
        if library is None or not rows:
            self.show_status("Select the songs to find lyrics for in a table first")
            return
        if self.scan_task is not None:
            self.show_status("Wait for the scan to finish")
            return
        answer = ask_auto_lyrics(AUTO_LYRICS_UI_FILE, self.window, len(rows), self.settings)
        if answer is None:
            return
        preference, self.auto_lyrics_save_to, skip_existing, upgrade_plain = answer
        songs = []
        for row in rows:
            info = self.library_model.info(row)
            songs.append(AutoLyricsSong(
                self.tracks[row],
                info.artist or info.album_artist,
                info.title,
                info.album,
                (info.duration_ms or 0) / 1000,
            ))
        self.auto_lyrics_library = library
        self.window.actionAutoLyrics.setText("Stop Finding Lyrics")
        self.online_search.auto_lyrics(songs, preference, skip_existing, upgrade_plain)

    def on_auto_lyrics_progress(self, done: int, total: int) -> None:
        self.show_status(f"Looking up lyrics: {done} of {total}…")

    def on_auto_lyrics_finished(
        self, results: list, not_found: int, skipped: int, upgraded: int, error: str, stopped: bool
    ) -> None:
        library = self.auto_lyrics_library
        self.auto_lyrics_library = None
        self.window.actionAutoLyrics.setText(self.auto_lyrics_text)
        if stopped:
            self.show_status("Stopped finding lyrics. No files were changed.")
            return
        if error:
            QMessageBox.warning(self.window, "Find lyrics", f"The lookup stopped early: {error}")
        instrumentals = sum(1 for _path, text, _where in results if text is None)
        notes = []
        if instrumentals:
            notes.append(f"{instrumentals} marked as instrumental")
        if upgraded:
            notes.append(f"{upgraded} upgraded to timed lyrics")
        if not_found:
            notes.append(f"none found for {not_found}")
        if skipped:
            notes.append(f"{skipped} already had lyrics")
        if not results:
            self.show_status("No lyrics were saved" + (f" ({', '.join(notes)})" if notes else ""))
            return
        self.tags_saved_note = f" ({', '.join(notes)})" if notes else ""
        default = LRC if self.auto_lyrics_save_to == SAVE_TO_LRC else EMBEDDED
        jobs = []
        for path, text, where in results:  # upgrades go where the plain lyrics were
            if text is None:  # an instrumental: no lyrics to save
                jobs.append(TagJob(path, TagChanges({HAS_LYRICS_TAG: "false"})))
            elif (where or default) == LRC:
                jobs.append(TagJob(path, TagChanges({HAS_LYRICS_TAG: "true"}), lrc_text=text))
            else:
                jobs.append(TagJob(path, TagChanges({"lyrics": text, HAS_LYRICS_TAG: "true"})))
        count = len(jobs)
        self.show_status(f"Saving the lyrics of {count} song{'' if count == 1 else 's'}…")
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
        if self.auto_tag_library is not None or self.auto_lyrics_library is not None or (
            self.tag_writer.is_busy()
        ):
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
        if self.auto_lyrics_library is not None:
            return "Wait for the lyrics to be found."
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
        stats = self.describe_playlist(shown)
        if page.source == "artist":
            self.window.artistsTabStats.setText(stats)
            self.show_artist_header(page)
        else:
            self.window.albumsTabStats.setText(stats)
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
        if kind in (PODCAST, STATION):
            self.show_pinned(kind, model.value(index))
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
                add("Copy path", "copy", lambda: self.copy_text(value, "Path copied"))
                add("Unpin", "unpin_folder", lambda: self.unpin_folder(value))
        elif kind == DEVICE:
            add("Synchronise…", "sync_device", lambda: self.show_device_tab(value))
            add("Open in Dolphin", "open_device", lambda: self.open_device(value))
            add("Safely Remove", "remove_device", lambda: self.safely_remove_device(value))
        elif kind == SECTION and value == DEVICES_SECTION:
            pass  # nothing to offer yet
        elif kind in (PODCAST, STATION):
            menu.addAction(asset_icon("play"), "Play").triggered.connect(
                lambda _checked=False: self.play_pinned(kind, value)
            )
            menu.addAction(asset_icon("mic" if kind == PODCAST else "radio"), "Show").triggered.connect(
                lambda _checked=False: self.show_pinned(kind, value)
            )
            menu.addSeparator()
            menu.addAction(asset_icon("pin-off"), "Unpin from Sidebar").triggered.connect(
                lambda _checked=False: self.toggle_pin(kind, int(value))
            )
        elif kind == SECTION and value in (PODCASTS_SECTION, STATIONS_SECTION):
            what = "podcasts" if value == PODCASTS_SECTION else "stations"
            hint = menu.addAction(f"Pin {what} from their right-click menus")
            hint.setEnabled(False)
            menu.addAction("Drag a section's title onto another to move it there").setEnabled(False)
        elif self.playlist_store is not None:
            self.fill_playlist_menu(menu, add, kind, value)
        if not menu.isEmpty():
            menu.exec(view.viewport().mapToGlobal(pos))

    def fill_playlist_menu(self, menu: QMenu, add, kind: str | None, value: str | None) -> None:
        """The menu entries for the Playlists section, a folder or a playlist."""
        name = value if kind == PLAYLIST else None
        if name is not None and name != self.current_playlist:
            self.select_playlist(name)

        if name is not None:
            self.fill_single_playlist_menu(menu, add, name)
            menu.addSeparator()

        folder = value if kind == FOLDER else ""
        add("New playlist…", "new_playlist", lambda: self.new_playlist(folder=folder))
        add("New auto-playlist…", "new_playlist", lambda: self.new_auto_playlist(folder=folder))
        add("New folder…", "new_folder", self.new_playlist_folder)
        add("Import playlist…", "import_playlist", self.import_playlist)
        if kind == FOLDER:
            menu.addSeparator()
            add("Rename folder…", "rename_folder", lambda: self.rename_playlist_folder(value))
            add("Delete folder", "delete_folder", lambda: self.delete_playlist_folder(value))

    def fill_single_playlist_menu(self, menu: QMenu, add, name: str) -> None:
        """The entries about one playlist: playing it, its songs, and managing it."""
        store = self.playlist_store
        playlist = store.load(name)
        rows = self.playlist_rows(name)
        has_songs = bool(rows)
        locked = self.side_model.is_locked(name)

        add("Play", "play_playlist", lambda: self.play_playlist(name), enabled=has_songs)
        add(
            "Play next", "play_next",
            lambda: self.queue_tracks(rows, play_next=True), enabled=has_songs,
        )
        add(
            "Add to queue", "add_to_queue",
            lambda: self.queue_tracks(rows, play_next=False), enabled=has_songs,
        )
        menu.addSeparator()

        add("Copy songs", "copy", lambda: self.copy_rows(rows), enabled=has_songs)
        if not playlist.is_auto:
            add(
                "Paste", "paste",
                lambda: self.add_to_playlist(name, self.clipboard_rows()),
                enabled=bool(self.clipboard_rows()) and not locked,
            )
        menu.addSeparator()

        add("Edit…", "edit_playlist", lambda: self.edit_playlist(name))
        add("Duplicate", "duplicate_playlist", lambda: self.duplicate_playlist(name))
        add("Change icon…", "playlist_icon", self.choose_playlist_icon)
        add(
            "Remove icon", "remove_playlist_icon",
            lambda: self.remove_playlist_icon(name),
            enabled=store.icon_file(name).exists(),
        )
        if not playlist.is_auto:
            add(
                "Unlock playlist" if locked else "Lock playlist", "lock_playlist",
                lambda: self.set_playlist_locked(name, not locked),
            )
        add("Export playlist…", "export_playlist", lambda: self.export_playlist(name))

        move_menu = menu.addMenu(self.menu_icons["move_to_folder"], "Move to folder")
        add(
            "No folder", None, lambda: self.move_to_folder(name, ""),
            enabled=bool(playlist.folder), target=move_menu,
        )
        for target in store.folders():
            add(
                target.replace("&", "&&"), None,
                lambda t=target: self.move_to_folder(name, t),
                enabled=target != playlist.folder, target=move_menu,
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

    def show_in_file_manager(self, paths: list[Path]) -> None:
        """Open the songs' folder in the file manager with the songs selected.

        Uses the desktop's standard request (org.freedesktop.FileManager1), which
        Dolphin, Files (Nautilus) and others understand; otherwise just the folder.
        """
        existing = [path for path in paths if path.exists()]
        if not existing:
            self.show_status("The file no longer exists")
            return
        call = QDBusMessage.createMethodCall(
            "org.freedesktop.FileManager1",
            "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1",
            "ShowItems",
        )
        call.setArguments([[QUrl.fromLocalFile(str(p)).toString() for p in existing], ""])
        reply = QDBusConnection.sessionBus().call(call, QDBus.CallMode.Block, 3000)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(existing[0].parent))):
                self.show_status(f"Could not open {existing[0].parent}")

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
            names = sorted({rule["tag"] for rule in rules} - {"rating", TIME_TAG, PLAYS_TAG})

            def tags_of(row: int, path: Path) -> dict[str, str]:
                """The song's tags, plus what MusicHive itself knows about it."""
                info = self.library_model.info(row)
                return {
                    **values.get(self.relative_path(path), {}),
                    "rating": str(info.rating),  # 0 for an unrated song
                    TIME_TAG: str((info.duration_ms or 0) // 1000),
                    PLAYS_TAG: str(info.play_count),  # 0 for a song never played
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
        equaliser: str = "",
        visualiser: str = "",
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
            equaliser,
            visualiser,
            equaliser_preset_names(self.settings),
            visualiser_preset_names(self.settings),
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
        store.set_presets(name, details.equaliser, details.visualiser)
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
            playlist.equaliser,
            playlist.visualiser,
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
        if current == self.play_source:
            self.apply_sound_presets()  # its presets may have changed

    def move_playlist(self, old: str, new: str) -> None:
        """Rename a playlist and everything that refers to it. Raises OSError."""
        self.playlist_store.rename(old, new)
        if self.play_source == old:
            self.play_source = new
        if self.saved_last_playlist() == old:
            self.settings.setValue(self.last_playlist_key(self.loaded_library), new)

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
        elif kind in (PODCAST, STATION):
            self.play_pinned(kind, self.side_model.value(model_index))

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
        return f"{info.name}: {track_count} songs · last scanned {scanned}"

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
        self.show_status(f"Scanning '{name}': {done} of {total} songs")

    def on_scan_finished(self, library_id: str, count: int) -> None:
        name = self.scan_library.name if self.scan_library else "library"
        self.scan_task = None
        self.scan_library = None

        current_id = self.current_library.id if self.current_library else None
        self.refresh_libraries(select_id=current_id)  # also refreshes the saved info
        if self.current_library is not None and self.current_library.id == library_id:
            self.reload_current_library()

        if count:
            self.show_status(f"Scan complete: {count} songs in '{name}'")
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
        # Back from a podcast to the song that was put aside: carry on where it was.
        resume_at = 0
        if self.away_from_music:
            if row == self.music_row_before:
                resume_at = self.music_position_before
            self.leave_side_modes()
        self.current_index = row
        self.position_ms = resume_at
        self.select_in_table(row)
        self.select_in_playlist(row)
        for page in self.group_pages.values():
            self.highlight_row(page.tracks, page.proxy, page.view, row)
        self.refresh_upcoming()
        if resume_at > 0:
            info = self.library_model.info(row)
            duration_ms = (info.duration_ms or 0) if info is not None else 0
            if self.player.load_file(self.tracks[row], resume_at, duration_ms):
                self.player.play()
            return
        self.player.play_file(self.tracks[row])

    # ----- podcasts -----

    def setup_podcasts(self) -> None:
        w = self.window
        self.podcasts = PodcastManager(self.settings, w)
        self.podcast_tab = PodcastsTab(
            w,
            self.podcasts,
            RoundedRowDelegate,
            asset_icon("mic"),
            self.show_status,
            asset_icon,
            self.settings,
        )
        self.podcast_tab.play_requested.connect(self.play_episode)
        self.podcasts.status.connect(lambda text: self.show_status(text) if text else None)
        self.podcasts.failed.connect(self.show_status)

    def play_episode(self, episode_id: int) -> None:
        """Play a podcast episode where it was left off. The music waits meanwhile."""
        episode = self.podcasts.store.episode(episode_id)
        if episode is None:
            return
        self.save_episode_position()
        self.enter_side_mode()
        self.stop_station()
        self.episode_id = episode_id
        self.podcasts.playing_episode = episode_id
        self.podcast_tab.set_playing(episode_id)
        source = self.podcasts.source_for(episode)
        if isinstance(source, str):
            # Streamed; the download runs alongside in case the connection drops.
            self.podcasts.download(episode_id, first=True)
        position = 0 if episode.played else episode.position_ms
        self.position_ms = position
        self.player.play_episode(source, position)

    @property
    def away_from_music(self) -> bool:
        """A podcast episode or a radio station plays (or was the last thing played)."""
        return self.episode_id is not None or self.station_id is not None

    def enter_side_mode(self) -> None:
        """An episode or a station takes over: coming from the music, remember where it was."""
        if not self.away_from_music:
            self.check_listening()
            self.music_row_before = self.current_index
            self.music_position_before = self.position_ms
        self.current_index = -1

    def stop_episode(self) -> None:
        """The episode stops being the one playing; it keeps its place for next time."""
        if self.episode_id is None:
            return
        self.save_episode_position()
        self.episode_id = None
        self.podcasts.playing_episode = None
        self.podcast_tab.set_playing(None)
        self.podcasts.tidy_up()  # the episode may wait to be deleted

    def leave_side_modes(self) -> None:
        """Music is played again."""
        self.stop_episode()
        self.stop_station()
        self.music_row_before = -1
        self.music_position_before = 0

    def save_episode_position(self) -> None:
        if self.episode_id is None:
            return
        self._episode_saved_at = time.monotonic()
        if self.playback_state == "stopped" and self.position_ms == 0:
            return  # stopping resets the position; that is no reason to forget it
        self.podcasts.set_position(self.episode_id, self.position_ms, self.duration_ms)
        self.podcast_tab.update_episode(self.episode_id)

    def episode_beside(self, newer: bool) -> int | None:
        """The episode released just after (or before) the current one."""
        episode = self.podcasts.store.episode(self.episode_id) if self.episode_id else None
        if episode is None:
            return None
        episodes = self.podcasts.store.episodes(episode.podcast_id)  # newest first
        ids = [e.id for e in episodes]
        index = ids.index(episode.id) + (-1 if newer else 1)
        return ids[index] if 0 <= index < len(ids) else None

    def finish_episode(self) -> None:
        """The episode played to its end: it counts as played, and the next may follow."""
        finished = self.podcasts.store.episode(self.episode_id)
        self.podcasts.set_played([self.episode_id], True)
        self.podcast_tab.update_episode(self.episode_id)
        if self.sleep_at_end:
            self.sleep_now()
            return
        following = self.episode_beside(newer=True)
        if following is not None and self.podcasts.settings.continue_with_next:
            upcoming = self.podcasts.store.episode(following)
            if upcoming is not None and not upcoming.played:
                self.play_episode(following)
                return
        if finished is not None:
            self.show_status(f"Finished “{finished.title}”")

    def show_episode_info(self) -> None:
        """Fill the track info panel for the episode that just started."""
        episode = self.podcasts.store.episode(self.episode_id)
        podcast = self.podcasts.store.podcast(episode.podcast_id) if episode else None
        if episode is None or podcast is None:
            return
        w = self.window
        w.currentTrackLabel.setText(episode.title)
        w.trackInfoTitle.setText(html.escape(episode.title))
        w.trackInfoArtistAlbumArtist.setText(html.escape(podcast.title))
        released = format_date(episode.published)
        w.trackInfoAlbumYear.setText(
            html.escape(f"Podcast episode, {released}" if released else "Podcast episode")
        )
        streaming = self.podcasts.file_path(episode) is None
        self.track_audio_format = ""
        w.trackInfoFile.setText(
            f'<span style="color: {theme.dimmed_text_on_panel()}; font-size: 8pt">'
            f"{'Streaming' if streaming else 'Downloaded'}</span>"
        )
        self.update_window_title(episode.title, podcast.title)
        cover = self.podcast_tab.cover_image(podcast.id)
        self.full_cover = cover
        self.set_visualiser_cover(cover)
        try:
            data = self.podcasts.cover_file(podcast.id).read_bytes() if cover else None
        except OSError:
            data = None
        self.full_cover_data = data
        self.mpris_art_url = cover_file_url(data) if data else ""
        self.lyrics_view.clear()
        self.rating_stars.set_rating(0, enabled=False)
        self.cover_view.set_image(cover)

    # ----- radio -----

    def setup_radio(self) -> None:
        w = self.window
        self.radio = RadioManager(self.settings, w)
        self.radio_tab = RadioTab(
            w,
            self.radio,
            RoundedRowDelegate,
            asset_icon("radio"),
            asset_icon,
            self.show_status,
            self.settings,
            self.find_in_library,
        )
        self.radio_tab.play_requested.connect(self.play_station)
        self.radio_tab.go_live_requested.connect(self.radio_go_live)
        self.radio_tab.tape_toggled.connect(self.set_taping)
        self.radio_tab.remember_requested.connect(self.remember_radio_song)
        self.radio.status.connect(lambda text: self.show_status(text) if text else None)
        self.radio.failed.connect(self.show_status)
        self.player.stream_title.connect(self.on_player_stream_title)
        self.radio_timer = QTimer(w)  # how far behind live, once a second
        self.radio_timer.setInterval(1000)
        self.radio_timer.timeout.connect(self.show_radio_delay)

    # ----- podcasts and stations pinned to the sidebar -----

    def setup_pinned_media(self) -> None:
        self.podcast_tab.is_pinned = lambda podcast_id: self.is_pinned(PODCAST, podcast_id)
        self.radio_tab.is_pinned = lambda station_id: self.is_pinned(STATION, station_id)
        self.podcast_tab.pin_toggled.connect(lambda podcast_id: self.toggle_pin(PODCAST, podcast_id))
        self.radio_tab.pin_toggled.connect(lambda station_id: self.toggle_pin(STATION, station_id))
        # Names and pictures change: show them as they are now.
        for signal in (
            self.podcasts.podcast_changed,
            self.podcasts.cover_ready,
            self.radio.station_changed,
            self.radio.logo_ready,
        ):
            signal.connect(lambda _id: self.refresh_pinned_media())
        self.refresh_pinned_media()

    def is_pinned(self, kind: str, item_id: int) -> bool:
        return f"{kind}:{item_id}" in self.pinned_media

    def toggle_pin(self, kind: str, item_id: int) -> None:
        key = f"{kind}:{item_id}"
        if key in self.pinned_media:
            self.pinned_media.remove(key)
            self.show_status("Unpinned from the sidebar")
        else:
            self.pinned_media.append(key)
            self.open_side_entries(entry_id(SECTION, SECTION_OF[kind]))
            self.show_status("Pinned to the sidebar")
        self.settings.setValue("pinnedMedia", json.dumps(self.pinned_media))
        self.refresh_pinned_media()
        self.apply_side_expansion()

    def refresh_pinned_media(self) -> None:
        """Show the pinned podcasts and stations in the sidebar, sorted by name."""
        entries = {PODCAST: [], STATION: []}
        icons = {PODCAST: asset_icon("mic"), STATION: asset_icon("radio")}
        for key in self.pinned_media:
            kind, _, value = key.partition(":")
            if not value.isdigit():
                continue
            if kind == PODCAST:
                podcast = self.podcasts.store.podcast(int(value))
                if podcast is not None:
                    entries[PODCAST].append((value, podcast.title, icons[PODCAST]))
            elif kind == STATION:
                station = self.radio.store.station(int(value))
                if station is not None:
                    entries[STATION].append((value, station.name, icons[STATION]))
        for kind, rows in entries.items():
            self.side_model.set_pinned(kind, sorted(rows, key=lambda row: row[1].casefold()))

    def on_side_section_moved(self, moved: str, target: str) -> None:
        """A section was dropped on another: it takes that one's place (after it when
        dragged down, before it when dragged up)."""
        model = self.side_model
        order = [model.value(model.index(row, 0)) for row in range(model.rowCount())]
        if moved not in order or target not in order:
            return
        moving_down = order.index(target) > order.index(moved)
        order.remove(moved)
        order.insert(order.index(target) + (1 if moving_down else 0), moved)
        model.section_order = order
        self.settings.setValue("sideSectionOrder", json.dumps(order))
        self.refresh_playlist_list(select=self.current_playlist)

    def show_pinned(self, kind: str, value: str) -> None:
        """Open a pinned podcast's or station's page."""
        w = self.window
        if kind == PODCAST:
            w.podcastsTab.setCurrentWidget(w.podcastsTabPage)
            self.podcast_tab.show_podcast(int(value))
        else:
            w.podcastsTab.setCurrentWidget(w.radioTab)
            self.radio_tab.show_station(int(value))

    def play_pinned(self, kind: str, value: str) -> None:
        if kind == PODCAST:
            self.podcast_tab.continue_podcast(int(value))
        else:
            self.play_station(int(value))

    def find_in_library(self, text: str) -> None:
        """Show the Songs tab, searching for `text` ("Artist - Title" works as words)."""
        w = self.window
        w.lineEdit.setText(text.replace(" - ", " "))
        self.search_songs_now()
        w.podcastsTab.setCurrentWidget(w.songsTab)

    def play_station(self, station_id: int) -> None:
        """Tune in to a station. The music waits meanwhile."""
        station = self.radio.store.station(station_id)
        if station is None:
            return
        if station_id == self.station_id and self.playback_state != "stopped":
            # Already tuned in: carry on (from where it was paused), do not reconnect.
            if self.playback_state == "paused":
                self.player.play()
            return
        self.save_episode_position()
        self.enter_side_mode()
        self.stop_episode()
        self.stop_station()
        self.station_id = station_id
        self.radio.played(station_id)
        self.radio_title = ""
        self._radio_song_position = 0
        self.show_status(f"Tuning in to {station.name}…")
        if station.hls:
            self.play_station_directly(station.url)
            return
        stream = RadioStream(station.url, station.name, self.radio.settings.keep_minutes * 60)
        stream.title_heard.connect(self.on_radio_title)
        stream.status.connect(self.show_status)
        stream.failed.connect(lambda message, s=stream: self.on_radio_failed(s, message))
        stream.tape_file_done.connect(self.on_tape_file_done)
        self.radio_stream = stream
        self.radio_tab.set_playing(station_id, can_tape=True)
        self.window.progressSlider.setEnabled(False)  # live radio cannot be seeked
        stream.start()
        self.player.play_radio(stream)
        self.radio_timer.start()

    def play_station_directly(self, url: str) -> None:
        """Some stations (HLS) can only be played by the player itself: no pausing
        back in time and no taping for those."""
        self.radio_stream = None
        self.radio_tab.set_playing(self.station_id, can_tape=False)
        self.radio_tab.set_behind(0)
        self.window.progressSlider.setEnabled(False)
        self.player.play_episode(url, 0)

    def stop_station(self) -> None:
        if self.station_id is None:
            return
        self.finish_radio_song()
        if self.radio_stream is not None:
            self.radio_stream.stop()
            self.radio_stream = None
        self.radio_timer.stop()
        self.window.progressSlider.setEnabled(True)
        self.station_id = None
        self.radio_title = ""
        self.radio_tab.set_playing(None)

    def on_radio_failed(self, stream: RadioStream, message: str) -> None:
        if stream is not self.radio_stream:
            return  # an old station's connection
        station = self.radio.store.station(self.station_id)
        stream.stop()
        if message == "hls" and station is not None:
            self.play_station_directly(station.url)
            return
        self.radio_stream = None
        self.player.stop()
        self.show_status(f"{station.name if station else 'The station'} cannot be played: {message}")

    def on_player_stream_title(self, title: str) -> None:
        """Titles from the player itself count only for a station it plays directly."""
        if self.station_id is not None and self.radio_stream is None:
            self.on_radio_title(title)

    def on_radio_title(self, title: str) -> None:
        """The listener now hears another song on the radio."""
        if self.station_id is None or title == self.radio_title:
            return
        ended = bool(self.radio_title)
        self.finish_radio_song()
        self.radio_title = title
        self._radio_song_position = self.position_ms
        self._radio_song_started_at = int(time.time())
        self.radio.heard(self.station_id, title)
        self.radio_tab.set_now_playing(title)
        self.show_station_info()
        self.discord.request_update()
        self.update_mpris()
        artist, song = split_title(title)
        if self.radio_scrobbling() and artist and song:
            self.lastfm.now_playing({"artist": artist, "title": song, "album": "", "duration": 0})
        if ended and self.sleep_at_end:  # "at the end of this song": the song changed
            self.sleep_now()

    def radio_scrobbling(self) -> bool:
        return self.radio.settings.scrobble and self.window.actionScrobble.isChecked()

    def finish_radio_song(self) -> None:
        """The song heard on the radio ended: scrobble it if it was heard long enough."""
        if not self.radio_title or not self.radio_scrobbling():
            return
        artist, song = split_title(self.radio_title)
        heard_ms = self.position_ms - self._radio_song_position
        if artist and song and heard_ms >= self.radio.settings.scrobble_after_seconds * 1000:
            self.lastfm.enqueue_scrobble({
                "artist": artist,
                "title": song,
                "album": "",
                "album_artist": "",
                "duration": 0,
                "timestamp": self._radio_song_started_at,
            })

    def play_favourite_beside(self, step: int) -> None:
        """Next/Previous on the radio: the next or previous favourite station."""
        ids = [s.id for s in self.radio.store.favourites()]
        if not ids:
            self.show_status("Add stations to your favourites to switch between them")
            return
        index = ids.index(self.station_id) + step if self.station_id in ids else 0
        self.play_station(ids[index % len(ids)])

    def show_radio_delay(self) -> None:
        behind = self.radio_stream.behind_seconds() if self.radio_stream is not None else 0.0
        self.radio_tab.set_behind(behind)
        w = self.window
        w.durationLabel.setText("LIVE" if behind < 2 else f"−{format_time(int(behind * 1000))}")

    def radio_go_live(self) -> None:
        stream = self.radio_stream
        if stream is None:
            return
        stream.go_live()
        self.player.play_radio(stream)  # starts afresh from the newest sound

    def set_taping(self, on: bool) -> None:
        stream = self.radio_stream
        if stream is None:
            self.radio_tab.set_taping(False)
            return
        if on:
            s = self.radio.settings
            stream.start_taping(s.tapes_folder(), s.tape_per_song, s.tape_from_song_start)
            self.show_status(f"Taping into {s.tapes_folder()}")
        else:
            stream.stop_taping()
            self.show_status("Taping stopped")

    def on_tape_file_done(self, path: Path, title: str) -> None:
        """A taped file is finished: give it tags, so the library knows what it is."""
        station = self.radio.store.station(self.station_id) if self.station_id else None
        if self.radio.settings.tape_tags and path.suffix.lower() == ".mp3":
            try:
                from mutagen.easyid3 import EasyID3
                from mutagen.id3 import ID3NoHeaderError

                try:
                    tags = EasyID3(str(path))
                except ID3NoHeaderError:
                    tags = EasyID3()
                artist, song = split_title(title)
                if song:
                    tags["title"] = song
                if artist:
                    tags["artist"] = artist
                if station is not None:
                    tags["album"] = station.name
                tags.save(str(path))
            except Exception as error:  # a tape without tags is still a tape
                print(f"MusicHive: could not tag {path.name}: {error}", file=sys.stderr)
        self.show_status(f"Taped “{title or path.stem}”")

    def remember_radio_song(self) -> None:
        if self.station_id is not None and self.radio_title:
            self.radio.remember(self.station_id, self.radio_title)
            self.show_status(f"Remembered “{self.radio_title}”")

    def show_station_info(self) -> None:
        """Fill the track info panel for the station (and the song heard on it)."""
        station = self.radio.store.station(self.station_id) if self.station_id else None
        if station is None:
            return
        w = self.window
        artist, song = split_title(self.radio_title)
        title = song or self.radio_title or station.name
        w.currentTrackLabel.setText(title)
        w.trackInfoTitle.setText(html.escape(title))
        w.trackInfoArtistAlbumArtist.setText(html.escape(artist or (station.name if self.radio_title else "")))
        w.trackInfoAlbumYear.setText(html.escape(f"Radio: {station.name}"))
        self.track_audio_format = ""
        quality = " ".join(p for p in (station.codec, f"{station.bitrate} kbps" if station.bitrate else "") if p)
        w.trackInfoFile.setText(
            f'<span style="color: {theme.dimmed_text_on_panel()}; font-size: 8pt">'
            f"{html.escape(quality or 'Live radio')}</span>"
        )
        self.update_window_title(title, artist or station.name)
        logo = self.radio_tab.logo_image(station.id)
        self.full_cover = logo
        self.set_visualiser_cover(logo)
        try:
            data = self.radio.logo_file(station.id).read_bytes() if logo else None
        except OSError:
            data = None
        self.full_cover_data = data
        self.mpris_art_url = cover_file_url(data) if data else ""
        self.lyrics_view.clear()
        self.rating_stars.set_rating(0, enabled=False)
        self.cover_view.set_image(logo)

    def radio_mpris_metadata(self) -> dict:
        station = self.radio.store.station(self.station_id)
        if station is None:
            return {"mpris:trackid": ("o", NO_TRACK)}
        artist, song = split_title(self.radio_title)
        metadata = {
            "mpris:trackid": ("o", f"/org/musichive/radio/{station.id}"),
            "xesam:title": ("s", song or self.radio_title or station.name),
            "xesam:artist": ("as", [artist or station.name]),
            "xesam:album": ("s", station.name),
        }
        if self.mpris_art_url:
            metadata["mpris:artUrl"] = ("s", self.mpris_art_url)
        return metadata

    def station_cover_key(self) -> str:
        if self.station_id is None or not self.radio.logo_file(self.station_id).is_file():
            return ""
        return f"radio:{self.station_id}"

    # ----- sleep timer -----

    def setup_sleep_timer(self) -> None:
        w = self.window
        self.sleep_timer = SleepTimer(w)
        self.sleep_timer.volume_factor.connect(self.on_sleep_fade)
        self.sleep_timer.expired.connect(self.sleep_now)
        self.sleep_timer.remaining_changed.connect(self.show_sleep_remaining)
        self.sleep_label = QLabel()
        self.sleep_label.setContentsMargins(0, 0, 8, 0)
        w.statusBar().addPermanentWidget(self.sleep_label)
        self.sleep_label.hide()
        group = QActionGroup(w)
        group.setExclusive(True)
        for action, minutes in (
            (w.actionSleepOff, 0),
            (w.actionSleep15, 15),
            (w.actionSleep30, 30),
            (w.actionSleep45, 45),
            (w.actionSleep60, 60),
            (w.actionSleep90, 90),
            (w.actionSleep120, 120),
            (w.actionSleepEndOfTrack, -1),
        ):
            group.addAction(action)
            action.triggered.connect(lambda _checked=False, m=minutes: self.set_sleep_timer(m))

    def set_sleep_timer(self, minutes: int) -> None:
        """0: off. -1: at the end of the song, episode or (on the radio) the song heard."""
        self.sleep_at_end = minutes == -1  # first: cancelling updates the status bar
        self.sleep_timer.cancel()
        if minutes > 0:
            fade = self.settings.value(FADE_KEY, DEFAULT_FADE_SECONDS, type=int)
            self.sleep_timer.start(minutes, fade)
            self.show_status(f"Sleep timer: playback pauses in {minutes} minutes")
        elif self.sleep_at_end:
            self.show_sleep_remaining(-1)
            self.show_status("Sleep timer: playback pauses when this ends")
        else:
            self.show_status("Sleep timer off")

    def show_sleep_remaining(self, minutes: int) -> None:
        label = self.sleep_label
        if minutes >= 0:
            label.setText(f"Sleep in {minutes} min")
        elif self.sleep_at_end:
            label.setText("Sleep at the end")
        label.setVisible(minutes >= 0 or self.sleep_at_end)

    def on_sleep_fade(self, factor: float) -> None:
        w = self.window
        if not w.muteButton.isChecked():
            self.player.set_volume(round(w.volumeSlider.value() * factor))

    def sleep_now(self) -> None:
        """Time is up: pause, then put the volume back for next time."""
        self.sleep_at_end = False
        self.sleep_timer.cancel()
        if self.playback_state == "playing":
            self.player.pause()
        w = self.window
        if not w.muteButton.isChecked():
            self.player.set_volume(w.volumeSlider.value())
        w.actionSleepOff.setChecked(True)
        self.show_sleep_remaining(-1)
        self.show_status("Sleep timer: paused. Good night!")

    def episode_mpris_metadata(self) -> dict:
        episode = self.podcasts.store.episode(self.episode_id)
        podcast = self.podcasts.store.podcast(episode.podcast_id) if episode else None
        if episode is None or podcast is None:
            return {"mpris:trackid": ("o", NO_TRACK)}
        metadata = {
            "mpris:trackid": ("o", f"/org/musichive/episode/{episode.id}"),
            "xesam:title": ("s", episode.title),
            "xesam:artist": ("as", [podcast.author or podcast.title]),
            "xesam:album": ("s", podcast.title),
            "xesam:genre": ("as", ["Podcast"]),
        }
        duration_ms = self.duration_ms or episode.duration_ms
        if duration_ms:
            metadata["mpris:length"] = ("x", duration_ms * 1000)
        if self.mpris_art_url:
            metadata["mpris:artUrl"] = ("s", self.mpris_art_url)
        return metadata

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
        elif self.station_id is not None:
            self.play_station(self.station_id)
        elif self.episode_id is not None:
            self.play_episode(self.episode_id)  # the episode again, where it was left off
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
        if self.station_id is not None:
            self.play_favourite_beside(1)
            return
        if self.episode_id is not None:
            following = self.episode_beside(newer=True)
            if following is None:
                self.show_status("This is the newest episode")
            else:
                self.play_episode(following)
            return
        if self.current_index < 0:
            shown = self.play_source_rows()
            if shown:  # nothing playing yet: start from the top of the list
                self.play_order.start_at(shown[0], shown)
                self.play_index(shown[0])
            return
        row = self.play_order.next()
        if row is None:
            self.show_status("This is the last song")
        else:
            self.play_index(row)

    def play_previous(self) -> None:
        """Restart the track if it is well underway, otherwise go back one."""
        if self.station_id is not None:
            self.play_favourite_beside(-1)
            return
        if self.episode_id is not None:
            earlier = self.episode_beside(newer=False)
            if self.position_ms > PREVIOUS_RESTART_THRESHOLD_MS or earlier is None:
                self.player.seek(0)
                self.discord.request_update()
            else:
                self.play_episode(earlier)
            return
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

    def on_slider_released(self, slider: QSlider | None = None) -> None:
        self.player.seek((slider or self.window.progressSlider).value())
        self.discord.request_update()

    def on_slider_action(self, action: int, slider: QSlider | None = None) -> None:
        """Handle keyboard and wheel changes (arrows, PageUp/Down, Home/End).

        Slider-move actions come from clicks and drags; those are handled
        once, on release, by on_slider_released.
        """
        if action == QAbstractSlider.SliderAction.SliderMove.value:
            return
        slider = slider or self.window.progressSlider
        if not slider.isSliderDown():
            self.player.seek(slider.sliderPosition())
            self.discord.request_update()

    # ----- player -> UI -----

    def on_track_started(self, track: Path) -> None:
        self.apply_sound_presets(track)
        if self.station_id is not None:
            self.show_station_info()
        elif self.episode_id is not None:
            self.show_episode_info()
        else:
            self.update_track_info(track)
        self.duration_ms = 0  # the real length arrives from the player in a moment
        self.begin_listening()
        self.discord.request_update()
        self.update_mpris()

    def apply_sound_presets(self, track: Path | None = None) -> None:
        """Use the equaliser and visualiser presets of the playing song and its playlist.

        `track` is the song that starts; by default the current one. Episodes and
        stations use the equaliser and visualiser as the user set them.
        """
        presets = SoundPresets()
        library = self.loaded_library
        if track is None and 0 <= self.current_index < len(self.tracks):
            track = self.tracks[self.current_index]
        if library is not None and track is not None and not self.away_from_music:
            try:
                rel_path = self.relative_path(track)
                stored = library_db.load_track_settings(library.db_path, [rel_path]).get(rel_path)
            except (ValueError, sqlite3.Error):  # not in the library, or no database
                stored = None
            song = SoundPresets(*stored) if stored else None
            playlist = None
            if self.play_source is not None and self.playlist_store is not None:
                loaded = self.playlist_store.load(self.play_source)
                playlist = SoundPresets(loaded.equaliser, loaded.visualiser)
            presets = chosen_presets(song, playlist)
        old, self.active_presets = self.active_presets, presets
        # Only on a change, so that tweaks made while a song plays are kept.
        if presets.equaliser != old.equaliser:
            eq = equaliser_preset(self.settings, presets.equaliser) if presets.equaliser else None
            self.player.set_equalizer(eq or load_equaliser(self.settings))
        if presets.visualiser != old.visualiser:
            style = load_visualiser_presets(self.settings).get(presets.visualiser)
            self.set_visualiser_style(style or load_visualiser_settings(self.settings))

    def on_track_ending(self) -> None:
        """A crossfade is due. If nothing comes next, the track just plays to its end."""
        if self.away_from_music:
            return  # episodes play to their end and never crossfade; radio never ends
        if not self.play_order.ends_after_current():
            self.on_track_finished()

    def on_track_finished(self) -> None:
        if self.station_id is not None:
            # A station played directly ended its stream: tune in again.
            self.play_station(self.station_id)
            return
        if self.episode_id is not None:
            self.finish_episode()
            return
        if self.sleep_at_end:  # the sleep timer: the next song waits, paused
            row = self.play_order.next(ended_naturally=True)
            if row is not None:
                self.load_track_paused(row)
            self.sleep_now()
            return
        stop_here = self.play_order.take_stop_after()
        self.update_stop_after_action()
        row = self.play_order.next(ended_naturally=True)
        if row is None:
            self.show_status("Reached the end of the library")
        elif stop_here:
            # "End playback after song": the next track waits, paused.
            self.load_track_paused(row)
        else:
            self.play_index(row)

    def on_state_changed(self, state: str) -> None:
        self.playback_state = state
        w = self.window
        if state == "paused":
            self.save_episode_position()

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
            if state == "paused" and not self.restoring_session:
                self.show_status("Paused")
            else:  # stopped
                self.position_ms = 0
                w.progressSlider.setValue(0)
                w.currentTimeLabel.setText(format_time(0))
        self.mini_player.show_playing(w.playButton.icon(), w.playButton.toolTip())

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
        if self.episode_id is not None and (
            time.monotonic() - self._episode_saved_at >= EPISODE_SAVE_SECONDS
        ):
            self.save_episode_position()
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
    app.setStyle(AssetIconStyle())
    # Qt draws nothing with this system's Noto Color Emoji, so use a font it can draw.
    QFontDatabase.addApplicationFont(str(EMOJI_FONT_FILE))
    QFontDatabase.setApplicationEmojiFontFamilies(EMOJI_FONTS)
    app.setApplicationName("MusicHive")
    app.setDesktopFileName("musichive")  # must match musichive.desktop
    # Dark or light mode and the highlight colour, before any window is loaded.
    theme.setup(QSettings("MusicHive", "MusicHive"), UI_DIR)
    music_hive = MusicHive()
    # Menus and dialogs only see the application palette, so give them the
    # selection colours from player.ui instead of the system accent colour,
    # and in light mode all of its colours instead of the system's.
    window_palette = music_hive.window.palette()
    palette = app.palette()
    roles = [
        QPalette.ColorRole.Highlight,
        QPalette.ColorRole.HighlightedText,
        QPalette.ColorRole.Accent,
    ]
    if theme.active_mode() == "light":
        roles += [
            QPalette.ColorRole.Window,
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Base,
            QPalette.ColorRole.AlternateBase,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.Button,
            QPalette.ColorRole.ButtonText,
            QPalette.ColorRole.BrightText,
            QPalette.ColorRole.PlaceholderText,
            QPalette.ColorRole.Light,
            QPalette.ColorRole.Midlight,
            QPalette.ColorRole.Mid,
            QPalette.ColorRole.Dark,
        ]
    for role in roles:
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            palette.setColor(group, role, window_palette.color(group, role))
    app.setPalette(palette)
    app.aboutToQuit.connect(music_hive.shutdown)
    music_hive.show()
    exit_code = app.exec()
    # Delete the windows while the QApplication still exists. Otherwise Python
    # may free them after it on exit, which crashes (SIGSEGV).
    # Only windows without a parent: deleting one also deletes the windows it owns.
    for window in QApplication.topLevelWidgets():
        if shiboken6.isValid(window) and window.parent() is None:
            shiboken6.delete(window)
    if music_hive.restart_requested:
        # Start again in this process. Nothing after this runs, so save first.
        music_hive.settings.sync()
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, *sys.argv])
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
