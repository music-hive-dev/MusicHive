"""The track lists: a table model for the library (with a sorting layer on top),
a model for the Upcoming list, and the row painter the Upcoming list uses."""

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QAbstractProxyModel,
    QAbstractTableModel,
    QMimeData,
    QModelIndex,
    QObject,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from src import theme
from src.formatting import format_time
from src.track_metadata import TrackInfo

PLACEHOLDER_ICON = "eighth-notes.svg"  # shown when a track has no cover
STOP_AFTER_ICON = "eject.svg"  # marks the track playback will end after

_LEFT_ALIGNED = (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter).value

TRACK_DRAG_MIME = "application/x-musichive-tracks"
UPCOMING_DRAG_MIME = "application/x-musichive-from-upcoming"  # positions dragged out of Upcoming


def make_track_drag(rows: list[int], playlist: str | None = None) -> QMimeData:
    """What a drag carries: library rows, and the playlist they were dragged from."""
    data = QMimeData()
    payload = json.dumps({"rows": rows, "playlist": playlist})
    data.setData(TRACK_DRAG_MIME, payload.encode("utf-8"))
    return data


def read_track_drag(data: QMimeData) -> tuple[list[int], str | None]:
    """The library rows and source playlist of a drag made by make_track_drag."""
    payload = json.loads(bytes(data.data(TRACK_DRAG_MIME)).decode("utf-8"))
    return payload["rows"], payload["playlist"]


# ----- the library table's columns -----


@dataclass(frozen=True)
class Column:
    """One column of the song tables."""

    title: str
    text: Callable[[TrackInfo], str]  # what a cell shows
    sort_key: Callable[[TrackInfo], object]  # what the column sorts by
    width: int = 120  # starting width, in pixels
    tag: str = ""  # the file tag it shows; "" for the built-in columns

    @property
    def key(self) -> str:
        """Identifies the column in saved layouts."""
        return f"{TAG_KEY_PREFIX}{self.tag}" if self.tag else self.title


def text_key(text: str) -> str:
    """Sort key for text: ignores case and accents (so "Ärzte" sorts under A)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _first_genre(text: str) -> str:
    """The first genre of "Rock, Pop", with a small plus if there are more."""
    genres = [genre for genre in re.split(r"\s*[;,]\s*", text.strip()) if genre]
    if not genres:
        return ""
    return f"{genres[0]}\u207a" if len(genres) > 1 else genres[0]


def _year_key(info: TrackInfo) -> int:
    return int(info.year) if info.year.isdigit() else 0


COLUMNS = (
    Column(
        "#",
        text=lambda info: str(info.track_number) if info.track_number else "",
        sort_key=lambda info: info.track_number or 0,
        width=44,
    ),
    Column(
        "Title",
        text=lambda info: info.title,
        sort_key=lambda info: text_key(info.title),
        width=260,
    ),
    Column(
        "Artist",
        text=lambda info: info.artist,
        sort_key=lambda info: text_key(info.artist),
        width=170,
    ),
    Column(
        "Album",
        text=lambda info: info.album,
        sort_key=lambda info: text_key(info.album),
        width=200,
    ),
    Column(
        "Year",
        text=lambda info: info.year,
        sort_key=_year_key,
        width=60,
    ),
    Column(
        "Time",
        text=lambda info: format_time(info.duration_ms) if info.duration_ms else "",
        sort_key=lambda info: info.duration_ms or 0,
        width=64,
    ),
    Column(
        "Genre",
        text=lambda info: _first_genre(info.genre),
        sort_key=lambda info: text_key(info.genre),
        width=130,
    ),
    Column(
        "Plays",
        text=lambda info: str(info.play_count) if info.play_count else "",
        sort_key=lambda info: info.play_count,
        width=56,
    ),
)

RATING_COLUMN_KEY = "Rating"


def _date_text(seconds: float | None) -> str:
    return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M") if seconds else ""


LAST_PLAYED_COLUMN = Column(
    "Last Played",
    text=lambda info: _date_text(info.last_played),
    sort_key=lambda info: info.last_played or 0,
    width=130,
)

# Built-in columns that are only offered in the column window, not shown by default.
OPTIONAL_COLUMNS = (
    Column(
        "Date Added",
        text=lambda info: _date_text(info.added_at),
        sort_key=lambda info: info.added_at or 0,
        width=130,
    ),
    LAST_PLAYED_COLUMN,
    Column(
        RATING_COLUMN_KEY,
        text=lambda info: "★" * info.rating,
        sort_key=lambda info: info.rating,
        width=90,
    ),
)

POSITION_COLUMN_KEY = "#"  # in a playlist, this column shows the position
TAG_KEY_PREFIX = "tag:"

# These tags already have a built-in column, which handles them better.
_TAGS_WITH_A_COLUMN = {"album", "artist", "genre", "rating", "title", "tracknumber"}

# Tag names are stored in lower case without spaces; these need more than a capital.
_TAG_TITLES = {
    "albumartist": "Album Artist",
    "albumartistsort": "Album Artist Sort",
    "albumsort": "Album Sort",
    "artistsort": "Artist Sort",
    "bpm": "BPM",
    "composersort": "Composer Sort",
    "discnumber": "Disc Number",
    "discsubtitle": "Disc Subtitle",
    "encodedby": "Encoded By",
    "initialkey": "Initial Key",
    "isrc": "ISRC",
    "originalalbum": "Original Album",
    "originalartist": "Original Artist",
    "originaldate": "Original Date",
    "playcount": "Play Count",
    "releasedate": "Release Date",
    "titlesort": "Title Sort",
}


def _tag_sort_key(text: str) -> tuple:
    """Numbers in number order (for BPM and the like), and before any text."""
    return (0, int(text), "") if text.isdecimal() else (1, 0, text_key(text))


def tag_column(name: str) -> Column:
    return Column(
        _TAG_TITLES.get(name, name.capitalize()),
        text=lambda info: info.tags.get(name, ""),
        sort_key=lambda info: _tag_sort_key(info.tags.get(name, "")),
        tag=name,
    )


def columns_from_keys(keys: list[str]) -> list[Column]:
    """The columns of a saved layout. An empty layout gives the default columns."""
    built_in = {column.key: column for column in (*COLUMNS, *OPTIONAL_COLUMNS)}
    columns = []
    for key in dict.fromkeys(keys):
        if key in built_in:
            columns.append(built_in[key])
        elif key.startswith(TAG_KEY_PREFIX):
            columns.append(tag_column(key[len(TAG_KEY_PREFIX):]))
    return columns or list(COLUMNS)


def all_columns(tag_names: list[str]) -> list[Column]:
    """Every column on offer: the built-in ones, then the tags by name."""
    tags = [tag_column(name) for name in tag_names if name not in _TAGS_WITH_A_COLUMN]
    return [
        *COLUMNS,
        *OPTIONAL_COLUMNS,
        *sorted(tags, key=lambda column: column.title.casefold()),
    ]

# ----- models -----


class TrackListModel(QAbstractTableModel):
    """The tracks of the current library, as loaded from its database.

    Row n is always library row n, whatever the table is sorted by: sorting
    happens in SortedTrackModel, so the play order can keep using these rows.
    """

    TitleRole = Qt.ItemDataRole.UserRole + 1
    SubtitleRole = Qt.ItemDataRole.UserRole + 2
    CoverRole = Qt.ItemDataRole.UserRole + 3
    StopAfterRole = Qt.ItemDataRole.UserRole + 4  # only the Upcoming list uses it
    ArtistRole = Qt.ItemDataRole.UserRole + 5
    DurationRole = Qt.ItemDataRole.UserRole + 6

    columns_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.columns: list[Column] = list(COLUMNS)
        self._paths: list[Path] = []
        self._infos: list[TrackInfo] = []
        self._cover_data: dict[str, bytes] = {}  # cover_key -> JPEG bytes
        self._cover_images: dict[str, QImage] = {}  # decoded on first use

    # ----- public API -----

    def set_columns(self, columns: list[Column]) -> None:
        """Choose the columns. The rows stay, so nothing that uses row numbers is disturbed."""
        self.columns = columns
        self.columns_changed.emit()

    def set_library(
        self,
        paths: list[Path],
        infos: list[TrackInfo],
        covers: dict[str, bytes],
    ) -> None:
        """Replace the whole list with a library's tracks."""
        self.beginResetModel()
        self._paths = list(paths)
        self._infos = list(infos)
        self._cover_data = covers
        self._cover_images = {}
        self.endResetModel()

    def clear(self) -> None:
        self.set_library([], [], {})

    def info(self, row: int) -> TrackInfo | None:
        """The tags of the track in `row`, or None if there is no such row."""
        return self._infos[row] if 0 <= row < len(self._infos) else None

    def sort_key(self, row: int, column: int):
        """What `row` sorts by in `column`."""
        return self.columns[column].sort_key(self._infos[row])

    def drag_data(self, rows: list[int]) -> QMimeData:
        return make_track_drag(rows)

    def set_path(self, row: int, path: Path) -> None:
        """Note where a track's file is now (its file was moved)."""
        if 0 <= row < len(self._paths):
            self._paths[row] = path

    def remove_row(self, row: int) -> None:
        """Remove one track from the list (its file was deleted)."""
        if not 0 <= row < len(self._paths):
            return
        self.beginResetModel()
        del self._paths[row]
        del self._infos[row]
        self.endResetModel()

    def set_play_count(self, row: int, count: int) -> None:
        """Update one track's play count and repaint its cell."""
        if not 0 <= row < len(self._infos):
            return
        self._infos[row].play_count = count
        column = self._plays_column()
        if column >= 0:
            cell = self.index(row, column)
            self.dataChanged.emit(cell, cell)

    def update_track(self, row: int, info: TrackInfo, covers: dict[str, bytes]) -> None:
        """Show new tags for a track and repaint its row."""
        if not 0 <= row < len(self._infos):
            return
        self._infos[row] = info
        self._cover_data.update(covers)
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.columns) - 1))

    def set_last_played(self, row: int, played_at: float) -> None:
        """Note when a track was last played and repaint its cell."""
        if not 0 <= row < len(self._infos):
            return
        self._infos[row].last_played = played_at
        self._repaint_cell(row, LAST_PLAYED_COLUMN.key)

    def set_rating(self, row: int, rating: int) -> None:
        """Change a track's star rating and repaint its cell."""
        if 0 <= row < len(self._infos):
            self._infos[row].rating = rating
            self._repaint_cell(row, RATING_COLUMN_KEY)

    def _repaint_cell(self, row: int, key: str) -> None:
        keys = [column.key for column in self.columns]
        if key in keys:
            cell = self.index(row, keys.index(key))
            self.dataChanged.emit(cell, cell)

    def set_all_play_counts(self, counts: list[int]) -> None:
        """Replace every track's play count (one number per row) and repaint."""
        for info, count in zip(self._infos, counts):
            info.play_count = count
        column = self._plays_column()
        if self._infos and column >= 0:
            self.dataChanged.emit(
                self.index(0, column),
                self.index(len(self._infos) - 1, column),
            )

    def _plays_column(self) -> int:
        """Where the Plays column is, or -1 if it is not shown."""
        return next((i for i, column in enumerate(self.columns) if column.key == "Plays"), -1)

    # ----- QAbstractTableModel -----

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._paths)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if orientation != Qt.Orientation.Horizontal or not 0 <= section < len(self.columns):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section].title
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return _LEFT_ALIGNED
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._paths):
            return None
        info = self._infos[index.row()]
        has_column = 0 <= index.column() < len(self.columns)

        if role == Qt.ItemDataRole.DisplayRole:
            return self.columns[index.column()].text(info) if has_column else None
        if role == self.TitleRole:
            return info.title
        if role == self.SubtitleRole:
            return info.subtitle
        if role == self.ArtistRole:
            return info.artist
        if role == self.DurationRole:
            return format_time(info.duration_ms) if info.duration_ms else ""
        if role == self.CoverRole:
            return self._cover_image(info.cover_key)
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(self._paths[index.row()])
        return None

    # ----- internal -----

    def _cover_image(self, key: str | None) -> QImage | None:
        if not key:
            return None
        image = self._cover_images.get(key)
        if image is None:
            data = self._cover_data.get(key)
            if data is None:
                return None
            image = QImage.fromData(data)
            if image.isNull():
                return None
            self._cover_images[key] = image
        return image


class SortedTrackModel(QAbstractProxyModel):
    """Shows the library model's rows in sorted order.

    Sorting is done here, in Python, with precomputed keys. That is much
    faster than Qt's built-in sorting for a large library, which would call
    back into Python twice for every comparison.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._order: list[int] = []  # shown row -> library row
        self._inverse: list[int] = []  # library row -> shown row
        self._sort_column = -1  # -1 means library order
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._shown: set[int] | None = None  # library rows let through (None = all)

    def set_shown_rows(self, rows: list[int] | None) -> None:
        """Show only these library rows (None shows them all)."""
        shown = None if rows is None else set(rows)
        if shown != self._shown:
            self.beginResetModel()
            self._shown = shown
            self._apply_sort()
            self.endResetModel()

    def setSourceModel(self, source_model) -> None:
        self.beginResetModel()
        super().setSourceModel(source_model)
        source_model.modelAboutToBeReset.connect(self.beginResetModel)
        source_model.modelReset.connect(self._on_source_reset)
        source_model.dataChanged.connect(self._on_source_data_changed)
        source_model.columns_changed.connect(self._on_source_columns_changed)
        self._apply_sort()
        self.endResetModel()

    @property
    def columns(self) -> list[Column]:
        return self.sourceModel().columns

    def source_rows(self) -> list[int]:
        """Library rows in the order they are shown."""
        return list(self._order)

    # ----- drag and drop -----

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDragEnabled
        )

    def mimeTypes(self) -> list[str]:
        return [TRACK_DRAG_MIME]

    def mimeData(self, indexes) -> QMimeData:
        """The dragged songs, in the order the table shows them."""
        shown = sorted({index.row() for index in indexes})
        return self.sourceModel().drag_data([self._order[row] for row in shown])

    # ----- QAbstractProxyModel -----

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if parent.isValid() or not 0 <= row < len(self._order):
            return QModelIndex()
        if not 0 <= column < self.columnCount():
            return QModelIndex()
        return self.createIndex(row, column)

    def parent(self, index: QModelIndex = QModelIndex()) -> QModelIndex:
        return QModelIndex()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._order)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        source = self.sourceModel()
        return 0 if parent.isValid() or source is None else source.columnCount()

    def mapToSource(self, proxy_index: QModelIndex) -> QModelIndex:
        source = self.sourceModel()
        if source is None or not proxy_index.isValid():
            return QModelIndex()
        if not 0 <= proxy_index.row() < len(self._order):
            return QModelIndex()
        return source.index(self._order[proxy_index.row()], proxy_index.column())

    def mapFromSource(self, source_index: QModelIndex) -> QModelIndex:
        if not source_index.isValid() or not 0 <= source_index.row() < len(self._inverse):
            return QModelIndex()
        return self.index(self._inverse[source_index.row()], source_index.column())

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """Called by the table when a column title is clicked."""
        self._sort_column = column
        self._sort_order = order

        self.layoutAboutToBeChanged.emit()
        old_order = self._order
        old_indexes = self.persistentIndexList()
        self._apply_sort()
        # Keep the selection on the same tracks after they move.
        new_indexes = [
            self.index(self._inverse[old_order[i.row()]], i.column())
            if 0 <= i.row() < len(old_order)
            else QModelIndex()
            for i in old_indexes
        ]
        self.changePersistentIndexList(old_indexes, new_indexes)
        self.layoutChanged.emit()

    # ----- internal -----

    def _on_source_reset(self) -> None:
        self._shown = None  # those row numbers no longer mean anything
        self._apply_sort()
        self.endResetModel()

    def _on_source_columns_changed(self) -> None:
        self.beginResetModel()
        self._apply_sort()
        self.endResetModel()

    def _on_source_data_changed(self, top_left, bottom_right, roles=None) -> None:
        """A cell changed in the library: repaint it where it is shown."""
        for source_row in range(top_left.row(), bottom_right.row() + 1):
            position = self._inverse[source_row] if 0 <= source_row < len(self._inverse) else -1
            if position >= 0:
                self.dataChanged.emit(
                    self.index(position, top_left.column()),
                    self.index(position, bottom_right.column()),
                )

    def _apply_sort(self) -> None:
        source = self.sourceModel()
        count = source.rowCount() if source is not None else 0
        rows = [row for row in range(count) if self._shown is None or row in self._shown]
        column = self._sort_column
        if source is not None and 0 <= column < source.columnCount():
            keys = {row: source.sort_key(row, column) for row in rows}
            descending = self._sort_order == Qt.SortOrder.DescendingOrder
            # The sort is stable, so equal values keep their library order.
            rows.sort(key=keys.__getitem__, reverse=descending)
        self._order = rows
        inverse = [-1] * count  # -1 means the row is not shown
        for position, row in enumerate(rows):
            inverse[row] = position
        self._inverse = inverse


def setup_track_view(view, model, placeholder: str | None = None) -> None:
    """Give a list view a model and the cover-and-two-lines row painter.
    `placeholder` is an icon file in assets/."""
    view.setModel(model)
    if placeholder is None:
        view.setItemDelegate(TrackDelegate(view))
    else:
        view.setItemDelegate(TrackDelegate(view, placeholder))
    view.setUniformItemSizes(True)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)


class QueueModel(QAbstractListModel):
    """The tracks in the order they will play, starting with the playing one.

    It stores only library row numbers and reads everything else (title, tags,
    cover) from the library model, so nothing is copied.

    Songs dragged in from other lists can be dropped between its rows: the
    signal gives their library rows and how far down the list they go. Songs
    dragged within the list are moved: songs_moved gives the positions they
    came from and the position they were dropped at.
    """

    songs_dropped = Signal(list, int)
    songs_moved = Signal(list, int)

    def __init__(self, library_model: TrackListModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library = library_model
        self._rows: list[int] = []
        self._stop_after = -1  # row of this list playback ends after, or -1
        library_model.modelReset.connect(self._on_library_reset)

    def set_rows(self, rows: list[int], stop_after: int = -1) -> None:
        """Show these library rows, in this order."""
        self.beginResetModel()
        self._rows = list(rows)
        self._stop_after = stop_after
        self.endResetModel()

    def library_row(self, index: QModelIndex) -> int:
        """The library row shown at `index`, or -1."""
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return -1
        return self._rows[index.row()]

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if not index.isValid():
            # Drops land between rows only: the rows themselves don't accept them.
            return flags | Qt.ItemFlag.ItemIsDropEnabled
        return flags | Qt.ItemFlag.ItemIsDragEnabled

    def mimeTypes(self) -> list[str]:
        return [TRACK_DRAG_MIME]

    def mimeData(self, indexes) -> QMimeData:
        positions = sorted(index.row() for index in indexes)
        data = make_track_drag([self._rows[position] for position in positions])
        data.setData(UPCOMING_DRAG_MIME, json.dumps(positions).encode("utf-8"))
        return data

    def canDropMimeData(self, data, action, row, column, parent) -> bool:
        return data.hasFormat(TRACK_DRAG_MIME)

    def dropMimeData(self, data, action, row, column, parent) -> bool:
        if not self.canDropMimeData(data, action, row, column, parent):
            return False
        # Below the playing track (row 0) at the earliest; -1 means the end.
        offset = max(1, row) if row >= 0 else len(self._rows)
        if data.hasFormat(UPCOMING_DRAG_MIME):
            positions = json.loads(bytes(data.data(UPCOMING_DRAG_MIME)).decode("utf-8"))
            self.songs_moved.emit(positions, offset)
            return True
        rows = read_track_drag(data)[0]
        if not rows:
            return False
        self.songs_dropped.emit(rows, offset)
        return True

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        library_row = self.library_row(index)
        if library_row < 0:
            return None
        if role == TrackListModel.StopAfterRole:
            return index.row() == self._stop_after
        if role == Qt.ItemDataRole.DisplayRole:
            role = TrackListModel.TitleRole
        elif role == TrackListModel.SubtitleRole:
            role = TrackListModel.ArtistRole  # the second line here is only the artist
        return self._library.data(self._library.index(library_row, 0), role)

    def _on_library_reset(self) -> None:
        # The library's rows were renumbered, so the old row numbers are stale.
        self.set_rows([])


class TrackDelegate(QStyledItemDelegate):
    """Paints a row as: [cover] Title / Artist – Album (Year)."""

    COVER_SIZE = 45
    PADDING = 3
    COVER_RADIUS = 7  # the row's 10px corners, less the padding around the cover
    TEXT_GAP = 10
    MARKER_SIZE = 16

    def __init__(
        self, parent: QObject | None = None, placeholder: str = PLACEHOLDER_ICON
    ) -> None:
        super().__init__(parent)
        # Icons from assets/, in the colours of dark or light mode.
        self._placeholder = QIcon(str(theme.asset_path(placeholder)))
        self._stop_icon = QIcon(str(theme.asset_path(STOP_AFTER_ICON)))

    def sizeHint(self, option, index) -> QSize:
        return QSize(200, self.COVER_SIZE + 2 * self.PADDING)

    def paint(self, painter: QPainter, option, index) -> None:
        # Let the style draw the background, hover and selection highlight.
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.icon = QIcon()
        widget = option.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        stop_after = bool(index.data(TrackListModel.StopAfterRole))
        rect = option.rect
        pad = self.PADDING

        # Cover, or a placeholder when there is none
        cover_rect = QRect(rect.left() + pad, rect.top() + pad, self.COVER_SIZE, self.COVER_SIZE)
        cover = index.data(TrackListModel.CoverRole)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if isinstance(cover, QImage):
            rounded = QPainterPath()
            rounded.addRoundedRect(QRectF(cover_rect), self.COVER_RADIUS, self.COVER_RADIUS)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipPath(rounded, Qt.ClipOperation.IntersectClip)
            painter.drawImage(cover_rect, cover)
            painter.restore()
        elif not self._placeholder.isNull():
            self._placeholder.paint(
                painter, cover_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
            )
        else:
            painter.fillRect(cover_rect, option.palette.mid())

        # Text to the right of the cover
        text_left = cover_rect.right() + 1 + self.TEXT_GAP
        text_width = max(0, rect.right() - pad - text_left + 1)

        # From the theme: the view's palette does not reliably carry the stylesheet's colours.
        title_color = QColor(theme.on_highlight() if selected else theme.colour(theme.TEXT))
        subtitle_color = QColor(title_color)
        subtitle_color.setAlpha(170)

        title = index.data(TrackListModel.TitleRole) or ""
        subtitle = index.data(TrackListModel.SubtitleRole) or ""

        title_font = QFont(option.font)
        title_font.setBold(True)
        title_metrics = QFontMetrics(title_font)
        title_height = title_metrics.height()
        # The marker needs the second line, even on a row with no text for it.
        two_lines = bool(subtitle) or stop_after

        # The duration sits at the right end of the title line; the title gives way to it.
        title_width = text_width
        duration = index.data(TrackListModel.DurationRole) or ""
        if duration:
            duration_width = QFontMetrics(option.font).horizontalAdvance(duration)
            title_width = max(0, text_width - duration_width - self.TEXT_GAP)
            line_height = title_height if two_lines else self.COVER_SIZE
            painter.setFont(option.font)
            painter.setPen(subtitle_color)
            painter.drawText(
                QRect(text_left, cover_rect.top(), text_width, line_height),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                duration,
            )

        # "Playback ends after this track" marker, under the duration
        subtitle_width = text_width
        if stop_after:
            subtitle_width = max(0, text_width - self.MARKER_SIZE - self.TEXT_GAP)
            marker_rect = QRect(
                text_left + text_width - self.MARKER_SIZE,
                cover_rect.top() + title_height,
                self.MARKER_SIZE,
                self.MARKER_SIZE,
            )
            self._stop_icon.paint(painter, marker_rect)

        title_text = title_metrics.elidedText(title, Qt.TextElideMode.ElideRight, title_width)

        painter.setFont(title_font)
        painter.setPen(title_color)
        if two_lines:
            title_rect = QRect(text_left, cover_rect.top(), text_width, title_height)
            painter.drawText(
                title_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                title_text,
            )
            subtitle_text = QFontMetrics(option.font).elidedText(
                subtitle, Qt.TextElideMode.ElideRight, subtitle_width
            )
            subtitle_rect = QRect(
                text_left,
                cover_rect.top() + title_height,
                text_width,
                self.COVER_SIZE - title_height,
            )
            painter.setFont(option.font)
            painter.setPen(subtitle_color)
            painter.drawText(
                subtitle_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                subtitle_text,
            )
        else:
            # No second line (the file has no artist/album/year tags):
            # centre the title next to the cover.
            full_rect = QRect(text_left, cover_rect.top(), text_width, self.COVER_SIZE)
            painter.drawText(
                full_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                title_text,
            )

        painter.restore()