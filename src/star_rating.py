"""Five clickable stars inside a QLabel."""

from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QStyle

STAR_COUNT = 5
STAR_SIDE = 16  # pixels


class StarRating(QObject):
    """Shows a rating of 0 to 5 stars in a label and lets the user change it.

    Hovering shows what a click would give. Clicking the star of the current
    rating clears it. The label's alignment can be set in Qt Designer.
    """

    rated = Signal(int)  # the rating the user chose, from 0 to 5

    def __init__(self, label: QLabel, empty_icon: QIcon, full_icon: QIcon) -> None:
        super().__init__(label)
        self._label = label
        self._empty_icon = empty_icon
        self._full_icon = full_icon
        self._rating = 0
        self._hover = 0  # the star under the mouse, or 0
        self._enabled = False
        label.installEventFilter(self)
        self._draw()

    def set_rating(self, rating: int, enabled: bool = True) -> None:
        """Show this rating. Without `enabled`, there is nothing to rate."""
        self._rating = rating
        self._enabled = enabled
        self._hover = 0
        self._draw()

    def eventFilter(self, watched, event) -> bool:
        if not self._enabled:
            return False
        kind = event.type()
        if kind == QEvent.Type.MouseMove:
            self._set_hover(self._star_at(event.position().toPoint().x()))
        elif kind == QEvent.Type.Leave:
            self._set_hover(0)
        elif kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            star = self._star_at(event.position().toPoint().x())
            if star:
                self.rated.emit(0 if star == self._rating else star)
        return False

    def _set_hover(self, star: int) -> None:
        if star != self._hover:
            self._hover = star
            self._draw()

    def _stars_rect(self) -> QRect:
        """Where the stars are inside the label, following its alignment."""
        label = self._label
        size = QSize(STAR_COUNT * STAR_SIDE, STAR_SIDE)
        return QStyle.alignedRect(label.layoutDirection(), label.alignment(), size, label.contentsRect())

    def _star_at(self, x: int) -> int:
        """The star (1 to 5) at this position in the label, or 0."""
        rect = self._stars_rect()
        return (x - rect.left()) // STAR_SIDE + 1 if rect.left() <= x <= rect.right() else 0

    def _draw(self) -> None:
        filled = self._hover or self._rating
        ratio = self._label.devicePixelRatioF()
        side = round(STAR_SIDE * ratio)
        pixmap = QPixmap(STAR_COUNT * side, side)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        for star in range(STAR_COUNT):
            icon = self._full_icon if star < filled else self._empty_icon
            icon.paint(painter, QRect(star * side, 0, side, side))
        painter.end()
        pixmap.setDevicePixelRatio(ratio)
        self._label.setPixmap(pixmap)
