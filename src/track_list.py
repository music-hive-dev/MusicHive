"""The library list: a model for the current library and a row painter."""

from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QImage, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from src.track_metadata import TrackInfo


class TrackListModel(QAbstractListModel):
    """Holds the tracks of the current library, as loaded from its database."""

    TitleRole = Qt.ItemDataRole.UserRole + 1
    SubtitleRole = Qt.ItemDataRole.UserRole + 2
    CoverRole = Qt.ItemDataRole.UserRole + 3

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

    # ----- QAbstractListModel -----

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._paths)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._paths):
            return None
        info = self._infos[index.row()]

        if role in (Qt.ItemDataRole.DisplayRole, self.TitleRole):
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

class QueueModel(QAbstractListModel):
    """The tracks in the order they will play, starting with the playing one.

    It stores only library row numbers and reads everything else (title, tags,
    cover) from the library model, so nothing is copied.
    """

    def __init__(self, library_model: TrackListModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._library = library_model
        self._rows: list[int] = []
        library_model.modelReset.connect(self._on_library_reset)

    def set_rows(self, rows: list[int]) -> None:
        """Show these library rows, in this order."""
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        library_row = self._rows[index.row()]
        return self._library.data(self._library.index(library_row), role)

    def _on_library_reset(self) -> None:
        # The library's rows were renumbered, so the old row numbers are stale.
        self.set_rows([])


class TrackDelegate(QStyledItemDelegate):
    """Paints a row as: [cover] Title / Artist – Album (Year)."""

    COVER_SIZE = 48
    PADDING = 6
    TEXT_GAP = 10

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
        rect = option.rect
        pad = self.PADDING

        # Cover, or a generic music icon when there is none
        cover_rect = QRect(rect.left() + pad, rect.top() + pad, self.COVER_SIZE, self.COVER_SIZE)
        cover = index.data(TrackListModel.CoverRole)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if isinstance(cover, QImage):
            painter.drawImage(cover_rect, cover)
        else:
            icon = QIcon.fromTheme("audio-x-generic")
            if icon.isNull():
                painter.fillRect(cover_rect, option.palette.mid())
            else:
                icon.paint(painter, cover_rect)

        # Text to the right of the cover
        text_left = cover_rect.right() + 1 + self.TEXT_GAP
        text_width = max(0, rect.right() - pad - text_left + 1)
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