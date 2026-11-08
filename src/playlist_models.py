"""Models for the Playlists tab, and a few small helpers."""

from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractListModel,
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QObject,
    Qt,
    Signal,
)
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QWidget

from src.track_list import TrackListModel


def make_square_icon(image: QImage, side: int = 256) -> QImage:
    """Crop an image to a centred square, and shrink it to `side` pixels if larger."""
    size = min(image.width(), image.height())
    x = (image.width() - size) // 2
    y = (image.height() - size) // 2
    square = image.copy(x, y, size, size)
    if size > side:
        square = square.scaled(
            side,
            side,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return square


class ClickFilter(QObject):
    """Makes any widget (for example a QLabel) emit `clicked` when it is clicked."""

    clicked = Signal()

    def __init__(self, widget: QWidget) -> None:
        super().__init__(widget)
        widget.setCursor(Qt.CursorShape.PointingHandCursor)
        widget.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
            and watched.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
        return False


class PlaylistTrackModel(QAbstractTableModel):
    """The songs of one playlist, as rows of the library model.

    It shows the same columns as the library table, except that "#" is the
    position in the playlist. It stores only library row numbers and reads
    everything else from the library model.
    """

    def __init__(self, library_model: TrackListModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library = library_model
        self._rows: list[int] = []
        library_model.modelReset.connect(self._on_library_reset)
        library_model.dataChanged.connect(self._on_library_data_changed)

    def set_rows(self, rows: list[int]) -> None:
        """Show these library rows, in this order."""
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def library_row(self, row: int) -> int:
        """The library row shown in row `row` of this model, or -1."""
        return self._rows[row] if 0 <= row < len(self._rows) else -1

    def playlist_row_of(self, library_row: int) -> int:
        """Where a library row appears in this model, or -1."""
        try:
            return self._rows.index(library_row)
        except ValueError:
            return -1

    def sort_key(self, row: int, column: int):
        if column == 0:
            return row  # the position in the playlist
        return self._library.sort_key(self._rows[row], column)

    # ----- QAbstractTableModel -----

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else self._library.columnCount()

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        return self._library.headerData(section, orientation, role)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        if index.column() == 0 and role == Qt.ItemDataRole.DisplayRole:
            return str(index.row() + 1)
        library_index = self._library.index(self._rows[index.row()], index.column())
        return self._library.data(library_index, role)

    # ----- internal -----

    def _on_library_reset(self) -> None:
        # The library's rows were renumbered, so the old row numbers are stale.
        self.set_rows([])

    def _on_library_data_changed(self, top_left, bottom_right, roles=None) -> None:
        """A library cell changed (for example a play count): repaint it here."""
        first, last = top_left.row(), bottom_right.row()
        for position, library_row in enumerate(self._rows):
            if first <= library_row <= last:
                self.dataChanged.emit(
                    self.index(position, top_left.column()),
                    self.index(position, bottom_right.column()),
                )


@dataclass
class PlaylistEntry:
    name: str
    song_count: int
    icon: QImage | None = None


class PlaylistListModel(QAbstractListModel):
    """The side menu: one row per playlist, with its icon, name and song count.

    It answers the same roles as the library model, so the Upcoming list's row
    painter can draw it.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._entries: list[PlaylistEntry] = []

    def set_entries(self, entries: list[PlaylistEntry]) -> None:
        self.beginResetModel()
        self._entries = list(entries)
        self.endResetModel()

    def name_at(self, row: int) -> str | None:
        return self._entries[row].name if 0 <= row < len(self._entries) else None

    def row_of(self, name: str) -> int:
        for row, entry in enumerate(self._entries):
            if entry.name == name:
                return row
        return -1

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        entry = self._entries[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, TrackListModel.TitleRole):
            return entry.name
        if role == TrackListModel.SubtitleRole:
            return f"{entry.song_count} song{'' if entry.song_count == 1 else 's'}"
        if role == TrackListModel.CoverRole:
            return entry.icon
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry.name
        return None