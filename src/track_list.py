"""The track lists: a table model for the library (with a sorting layer on top),
a model for the Upcoming list, and the row painter the Upcoming list uses."""

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QAbstractProxyModel,
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QRect,
    QSize,
    Qt,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QImage, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from src.formatting import format_time
from src.track_metadata import TrackInfo

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
PLACEHOLDER_ICON = ASSETS_DIR / "eighth-notes.svg"  # shown when a track has no cover
STOP_AFTER_ICON = ASSETS_DIR / "eject.svg"  # marks the track playback will end after

_RIGHT_ALIGNED = (Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter).value
_LEFT_ALIGNED = (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter).value


# ----- the library table's columns -----


@dataclass(frozen=True)
class Column:
    """One column of the library table. Adding a column means adding one of these."""

    title: str
    text: Callable[[TrackInfo], str]  # what a cell shows
    sort_key: Callable[[TrackInfo], object]  # what the column sorts by
    width: int = 120  # starting width, in pixels
    align_right: bool = False
    stretch: bool = False  # takes up the space that is left over


def _text_key(text: str) -> str:
    """Sort key for text: ignores case and accents (so "Ärzte" sorts under A)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _year_key(info: TrackInfo) -> int:
    return int(info.year) if info.year.isdigit() else 0


COLUMNS = (
    Column(
        "#",
        text=lambda info: str(info.track_number) if info.track_number else "",
        sort_key=lambda info: info.track_number or 0,
        width=44,
        align_right=True,
    ),
    Column(
        "Title",
        text=lambda info: info.title,
        sort_key=lambda info: _text_key(info.title),
        width=260,
        stretch=True,
    ),
    Column(
        "Artist",
        text=lambda info: info.artist,
        sort_key=lambda info: _text_key(info.artist),
        width=170,
    ),
    Column(
        "Album",
        text=lambda info: info.album,
        sort_key=lambda info: _text_key(info.album),
        width=200,
    ),
    Column(
        "Year",
        text=lambda info: info.year,
        sort_key=_year_key,
        width=60,
        align_right=True,
    ),
    Column(
        "Time",
        text=lambda info: format_time(info.duration_ms) if info.duration_ms else "",
        sort_key=lambda info: info.duration_ms or 0,
        width=64,
        align_right=True,
    ),
    Column(
        "Genre",
        text=lambda info: info.genre,
        sort_key=lambda info: _text_key(info.genre),
        width=130,
    ),
)


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

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._paths: list[Path] = []
        self._infos: list[TrackInfo] = []
        self._cover_data: dict[str, bytes] = {}  # cover_key -> JPEG bytes
        self._cover_images: dict[str, QImage] = {}  # decoded on first use

    # ----- public API -----

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
        return COLUMNS[column].sort_key(self._infos[row])

    def remove_row(self, row: int) -> None:
        """Remove one track from the list (its file was deleted)."""
        if not 0 <= row < len(self._paths):
            return
        self.beginResetModel()
        del self._paths[row]
        del self._infos[row]
        self.endResetModel()

    # ----- QAbstractTableModel -----

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._paths)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if orientation != Qt.Orientation.Horizontal or not 0 <= section < len(COLUMNS):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section].title
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return _RIGHT_ALIGNED if COLUMNS[section].align_right else _LEFT_ALIGNED
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._paths):
            return None
        info = self._infos[index.row()]
        has_column = 0 <= index.column() < len(COLUMNS)

        if role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[index.column()].text(info) if has_column else None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if has_column and COLUMNS[index.column()].align_right:
                return _RIGHT_ALIGNED
            return None
        if role == self.TitleRole:
            return info.title
        if role == self.SubtitleRole:
            return info.subtitle
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

    def setSourceModel(self, source_model) -> None:
        self.beginResetModel()
        super().setSourceModel(source_model)
        source_model.modelAboutToBeReset.connect(self.beginResetModel)
        source_model.modelReset.connect(self._on_source_reset)
        self._apply_sort()
        self.endResetModel()

    def source_rows(self) -> list[int]:
        """Library rows in the order they are shown."""
        return list(self._order)

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
        self._apply_sort()
        self.endResetModel()

    def _apply_sort(self) -> None:
        source = self.sourceModel()
        count = source.rowCount() if source is not None else 0
        rows = list(range(count))
        column = self._sort_column
        if source is not None and 0 <= column < source.columnCount():
            keys = [source.sort_key(row, column) for row in rows]
            descending = self._sort_order == Qt.SortOrder.DescendingOrder
            # The sort is stable, so equal values keep their library order.
            rows.sort(key=keys.__getitem__, reverse=descending)
        self._order = rows
        inverse = [0] * count
        for position, row in enumerate(rows):
            inverse[row] = position
        self._inverse = inverse


class QueueModel(QAbstractListModel):
    """The tracks in the order they will play, starting with the playing one.

    It stores only library row numbers and reads everything else (title, tags,
    cover) from the library model, so nothing is copied.
    """

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
        return self._library.data(self._library.index(library_row, 0), role)

    def _on_library_reset(self) -> None:
        # The library's rows were renumbered, so the old row numbers are stale.
        self.set_rows([])


class TrackDelegate(QStyledItemDelegate):
    """Paints a row as: [cover] Title / Artist – Album (Year)."""

    COVER_SIZE = 48
    PADDING = 6
    TEXT_GAP = 10
    MARKER_SIZE = 16

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._placeholder = QIcon(str(PLACEHOLDER_ICON))
        self._stop_icon = QIcon(str(STOP_AFTER_ICON))

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
            painter.drawImage(cover_rect, cover)
        elif not self._placeholder.isNull():
            self._placeholder.paint(painter, cover_rect)
        else:
            painter.fillRect(cover_rect, option.palette.mid())

        # "Playback ends after this track" marker, at the right edge
        right_pad = pad
        if stop_after:
            marker_rect = QRect(
                rect.right() - pad - self.MARKER_SIZE + 1,
                rect.center().y() - self.MARKER_SIZE // 2,
                self.MARKER_SIZE,
                self.MARKER_SIZE,
            )
            self._stop_icon.paint(painter, marker_rect)
            right_pad = pad + self.MARKER_SIZE + self.TEXT_GAP

        # Text to the right of the cover
        text_left = cover_rect.right() + 1 + self.TEXT_GAP
        text_width = max(0, rect.right() - right_pad - text_left + 1)
        half = self.COVER_SIZE // 2

        color_role = option.palette.ColorRole.HighlightedText if selected else option.palette.ColorRole.Text
        title_color = option.palette.color(color_role)
        subtitle_color = QColor(title_color)
        subtitle_color.setAlpha(170)

        title = index.data(TrackListModel.TitleRole) or ""
        subtitle = index.data(TrackListModel.SubtitleRole) or ""

        title_font = QFont(option.font)
        title_font.setBold(True)
        title_text = QFontMetrics(title_font).elidedText(
            title, Qt.TextElideMode.ElideRight, text_width
        )

        painter.setFont(title_font)
        painter.setPen(title_color)
        if subtitle:
            title_rect = QRect(text_left, cover_rect.top(), text_width, half)
            painter.drawText(
                title_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                title_text,
            )
            subtitle_text = QFontMetrics(option.font).elidedText(
                subtitle, Qt.TextElideMode.ElideRight, text_width
            )
            subtitle_rect = QRect(
                text_left, cover_rect.top() + half, text_width, self.COVER_SIZE - half
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