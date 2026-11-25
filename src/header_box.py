"""The box behind the header of a playlist, artist, album or podcast.

The box itself, its rounded corners and its usual colour are in player.ui
(playlistHeaderBox and the others). Here the header's picture gets rounded
corners too, and the box takes a colour from that picture: the cover colours'
background, which is dark in dark mode and light in light mode, so the text
stays readable on it. A placeholder icon gives the box its usual colour back.
"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QLabel, QWidget

from src import theme
from src.cover_colours import cover_colours

CORNER_RATIO = 0.155  # like the covers in the song lists (7 of 45 px); 15 px at 96 px
# player.ui rounds the box by that plus its padding (15 + 10 = 25 px), so the
# corners follow each other: change them together.


def rounded_pixmap(pixmap: QPixmap) -> QPixmap:
    """The picture with rounded corners; what is outside them becomes see-through."""
    if pixmap.isNull():
        return pixmap
    ratio = pixmap.devicePixelRatio() or 1.0
    width, height = pixmap.width() / ratio, pixmap.height() / ratio
    result = QPixmap(pixmap.size())
    result.setDevicePixelRatio(ratio)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    corners = QPainterPath()
    radius = min(width, height) * CORNER_RATIO
    corners.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
    painter.setClipPath(corners)
    painter.drawPixmap(0, 0, pixmap)
    painter.end()
    return result


def box_colour(image: QImage | None) -> QColor | None:
    """The colour for the box behind a picture; None for the usual one."""
    if image is None or image.isNull():
        return None
    dark_mode = QColor(theme.colour(theme.TEXT)).lightness() > 128  # light text
    colours = cover_colours(image, dark_mode=dark_mode)
    return colours.background if colours is not None else None


class HeaderBox:
    """One header: its box (a QFrame from player.ui) and its picture (a QLabel)."""

    def __init__(self, box: QWidget, picture: QLabel) -> None:
        self._box = box
        self._picture = picture

    def set_picture(self, pixmap: QPixmap, colour_from: QImage | None) -> None:
        """Show a picture, and colour the box from `colour_from` (None: the usual colour)."""
        self.set_frame(pixmap)
        self.set_colour(box_colour(colour_from))

    def set_frame(self, pixmap: QPixmap) -> None:
        """Show a picture without colouring the box again (a frame of an animation)."""
        self._picture.setPixmap(rounded_pixmap(pixmap))

    def set_colour(self, colour: QColor | None) -> None:
        name = self._box.objectName()
        # Only the colour is set here; the corners stay as player.ui has them.
        self._box.setStyleSheet(
            f"QFrame#{name} {{ background-color: {colour.name()}; }}" if colour is not None else ""
        )
