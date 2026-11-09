"""Models for the Playlists tab, and a few small helpers."""

from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QMimeData,
    QModelIndex,
    QObject,
    Qt,
    Signal,
)
from PySide6.QtGui import QImage, QImageReader, QMovie
from PySide6.QtWidgets import QWidget

from src.track_list import (
    LAST_PLAYED_COLUMN,
    POSITION_COLUMN_KEY,
    TRACK_DRAG_MIME,
    Column,
    SortedTrackModel,
    TrackListModel,
    make_track_drag,
    read_track_drag,
)


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


def is_animated_gif(path: str | Path) -> bool:
    """True if the file is a GIF with more than one frame."""
    reader = QImageReader(str(path))
    return bytes(reader.format()) == b"gif" and reader.imageCount() != 1


def _modified_time(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


class IconAnimation(QObject):
    """Plays a GIF, handing out each frame cropped to a square.

    Until the first frame arrives, whoever shows it keeps showing the still icon.
    """

    frame_ready = Signal(QImage)

    def __init__(self, path: Path, side: int = 256, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self.last_frame: QImage | None = None
        self._side = side
        self._modified = _modified_time(path)
        self._movie = QMovie(str(path))
        self._movie.frameChanged.connect(self._on_frame_changed)

    def matches(self, path: Path | None) -> bool:
        """True if this is already playing that file, as it is on disk now."""
        return path == self.path and _modified_time(self.path) == self._modified

    def start(self) -> None:
        self._movie.start()

    def dispose(self) -> None:
        """Stop for good."""
        self._movie.stop()
        self.deleteLater()

    def _on_frame_changed(self, _frame_number: int) -> None:
        self.last_frame = make_square_icon(self._movie.currentImage(), self._side)
        self.frame_ready.emit(self.last_frame)


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

    columns_changed = Signal()

    def __init__(self, library_model: TrackListModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library = library_model
        self._rows: list[int] = []
        self.name: str | None = None  # the playlist being shown
        self.locked = False
        library_model.modelReset.connect(self._on_library_reset)
        library_model.dataChanged.connect(self._on_library_data_changed)
        library_model.columns_changed.connect(self.columns_changed)

    @property
    def columns(self) -> list[Column]:
        return self._library.columns

    def is_position_column(self, column: int) -> bool:
        columns = self.columns
        return 0 <= column < len(columns) and columns[column].key == POSITION_COLUMN_KEY

    def set_playlist(self, name: str | None, rows: list[int], locked: bool) -> None:
        """Show a playlist: its name, its songs in order, and whether it is locked."""
        self.name = name
        self.locked = locked
        self.set_rows(rows)

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

    def drag_data(self, rows: list[int]) -> QMimeData:
        return make_track_drag([self._rows[row] for row in rows], self.name)

    def sort_key(self, row: int, column: int):
        if self.is_position_column(column):
            return row
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
        if self.is_position_column(index.column()) and role == Qt.ItemDataRole.DisplayRole:
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


class HistoryTrackModel(PlaylistTrackModel):
    """The History table: the usual columns, always ending with Last Played."""

    @property
    def columns(self) -> list[Column]:
        columns = self._library.columns
        if any(column.key == LAST_PLAYED_COLUMN.key for column in columns):
            return columns  # the user has already chosen to show it
        return [*columns, LAST_PLAYED_COLUMN]

    def _is_extra_column(self, column: int) -> bool:
        return self._library.columnCount() <= column < len(self.columns)

    def sort_key(self, row: int, column: int):
        if self._is_extra_column(column):
            return LAST_PLAYED_COLUMN.sort_key(self._library.info(self.library_row(row)))
        return super().sort_key(row, column)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if not self._is_extra_column(section) or orientation != Qt.Orientation.Horizontal:
            return super().headerData(section, orientation, role)
        if role == Qt.ItemDataRole.DisplayRole:
            return LAST_PLAYED_COLUMN.title
        return super().headerData(0, orientation, role)  # looks like the other headers

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if (
            index.isValid()
            and self._is_extra_column(index.column())
            and role == Qt.ItemDataRole.DisplayRole
        ):
            return LAST_PLAYED_COLUMN.text(self._library.info(self.library_row(index.row())))
        return super().data(index, role)


class PlaylistSortedModel(SortedTrackModel):
    """The playlist table's sorted model. It also decides what dropped songs do.

    Songs dragged inside the playlist are rearranged; songs from anywhere else
    are added. Rows in the signals are library rows, and the second value is
    the library row the songs go in front of (-1 means the end).
    """

    songs_added = Signal(list, int)
    songs_rearranged = Signal(list, int)
    drop_refused = Signal(str)  # why the songs being dragged cannot be dropped here

    def in_saved_order(self) -> bool:
        """True if the table shows the playlist in its own order."""
        if self.sourceModel().is_position_column(self._sort_column):
            return self._sort_order == Qt.SortOrder.AscendingOrder
        return self._sort_column == -1

    def _refusal(self, data: QMimeData) -> str | None:
        playlist = self.sourceModel()
        if playlist.name is None:
            return "Select a playlist first"
        if playlist.locked:
            return f"'{playlist.name}' is locked"
        if read_track_drag(data)[1] == playlist.name and not self.in_saved_order():
            return "To rearrange songs, click the # column to sort by playlist order"
        return None

    def canDropMimeData(self, data, action, row, column, parent) -> bool:
        if not data.hasFormat(TRACK_DRAG_MIME):
            return False
        problem = self._refusal(data)
        if problem is not None:
            self.drop_refused.emit(problem)
        return problem is None

    def dropMimeData(self, data, action, row, column, parent) -> bool:
        if not self.canDropMimeData(data, action, row, column, parent):
            return False
        rows, source = read_track_drag(data)
        playlist = self.sourceModel()
        rearranging = source == playlist.name
        moving = set(rows) if rearranging else set()

        # In a sorted table the drop position says nothing about the playlist's order.
        count = playlist.rowCount()
        first = row if row >= 0 and self.in_saved_order() else count
        following = (playlist.library_row(position) for position in range(first, count))
        before = next((song for song in following if song not in moving), -1)

        (self.songs_rearranged if rearranging else self.songs_added).emit(rows, before)
        return True
