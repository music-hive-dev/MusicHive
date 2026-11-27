"""Showing a cover image as a square inside a QLabel."""

from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QObject, QSize, Qt
from PySide6.QtGui import QIcon, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy

MAX_SIDE = 512  # the cover never grows beyond this many pixels
WORKING_SIDE = 1024  # huge covers are shrunk to this once, so resizing stays fast


def _read_shrunk(reader: QImageReader, max_side: int) -> QImage | None:
    """Read the image so that its shorter side is at most `max_side` pixels.

    The reader shrinks it while decoding, so a huge cover never has to be held
    at full size: a 3000 x 3000 picture would take 36 MB, this one about 4 MB.
    """
    size = reader.size()
    shorter = min(size.width(), size.height())
    if shorter > max_side:
        scale = max_side / shorter
        reader.setScaledSize(
            QSize(max(1, round(size.width() * scale)), max(1, round(size.height() * scale)))
        )
    image = reader.read()
    return None if image.isNull() else image


def decode_cover(data: bytes | None, max_side: int = WORKING_SIDE) -> QImage | None:
    """A cover from its bytes, at a size for the screen (None if there is none)."""
    if not data:
        return None
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    return _read_shrunk(QImageReader(buffer), max_side)


def load_cover(path: Path, max_side: int = WORKING_SIDE) -> QImage | None:
    """A cover from an image file, at a size for the screen (None if unreadable)."""
    return _read_shrunk(QImageReader(str(path)), max_side)


def _centre_square(image: QImage) -> QImage:
    """The largest centred square of `image`, at most WORKING_SIDE pixels wide."""
    side = min(image.width(), image.height())
    x = (image.width() - side) // 2
    y = (image.height() - side) // 2
    square = image.copy(x, y, side, side)
    if side > WORKING_SIDE:
        square = square.scaled(
            WORKING_SIDE,
            WORKING_SIDE,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return square


class SquareCover(QObject):
    """Makes a QLabel show a cover image as a square that follows its width.

    The label keeps a square shape (up to MAX_SIDE) and the image is cropped
    from the centre to fill it, so it is never stretched or squashed.
    """

    def __init__(self, label: QLabel, placeholder: QIcon | None = None) -> None:
        super().__init__(label)
        self._label = label
        self._placeholder = placeholder
        self._square: QImage | None = None
        self._rendered_side = 0

        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setScaledContents(False)
        # Ignore the picture's own size when laying out; otherwise a label
        # showing a big picture could never be made narrower again.
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        label.installEventFilter(self)
        self._refresh(force=True)

    def set_image(self, image: QImage | None) -> None:
        """Show `image`, or the placeholder if it is None or unreadable."""
        if image is None or image.isNull():
            self._square = None
        else:
            self._square = _centre_square(image)
        self._refresh(force=True)

    def eventFilter(self, watched, event) -> bool:
        if watched is self._label and event.type() == QEvent.Type.Resize:
            self._refresh()
        return False

    def _refresh(self, force: bool = False) -> None:
        label = self._label
        side = max(1, min(label.width(), MAX_SIDE))
        label.setFixedHeight(side)  # keeps the label square

        dpr = label.devicePixelRatioF()
        pixel_side = max(1, round(side * dpr))
        if not force and pixel_side == self._rendered_side:
            return  # nothing changed, so skip the (smooth) rescale
        self._rendered_side = pixel_side

        if self._square is not None:
            scaled = self._square.scaled(
                pixel_side,
                pixel_side,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            pixmap = QPixmap.fromImage(scaled)
            pixmap.setDevicePixelRatio(dpr)
            label.setPixmap(pixmap)
        elif self._placeholder is not None and not self._placeholder.isNull():
            half = max(1, side // 2)
            label.setPixmap(self._placeholder.pixmap(half, half))
        else:
            label.clear()