"""The playlist window (playlist_edit.ui): a playlist's title, image and description."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox, QWidget

from src.lastfm_dialog import load_dialog
from src.playlist_models import (
    ClickFilter,
    IconAnimation,
    is_animated_gif,
    make_square_icon,
)

PREVIEW_SIZE = 128


@dataclass
class PlaylistDetails:
    name: str
    description: str
    icon: QImage | None  # the image to use; None means no image
    icon_changed: bool
    animation: Path | None = None  # the GIF the image came from, if it is animated


class PlaylistEditDialog:
    """Connects the widgets of playlist_edit.ui. Nothing is saved here: run()
    returns what the user entered, and the caller stores it."""

    def __init__(
        self,
        ui_path: Path,
        parent: QWidget,
        placeholder: QIcon,
        check_name: Callable[[str], str | None],
        window_title: str,
        name: str = "",
        description: str = "",
        icon: QImage | None = None,
        animation: Path | None = None,
    ) -> None:
        self._placeholder = placeholder
        self._check_name = check_name  # says why a title cannot be used, or None
        self._icon = icon
        self._icon_changed = False
        self._animation = animation
        self._playing: IconAnimation | None = None  # the preview, if it is animated
        self.dialog = load_dialog(ui_path, parent)
        d = self.dialog

        d.setWindowTitle(window_title)
        d.playlistEditTitle.setText(name)
        d.playlistEditTitle.selectAll()
        d.playlistEditTitle.setFocus()
        d.playlistEditDescription.setPlainText(description)
        self._show_icon()

        self._image_click = ClickFilter(d.playlistEditImage)
        self._image_click.clicked.connect(self._choose_image)
        d.playlistEditChooseImageButton.clicked.connect(self._choose_image)
        d.playlistEditRemoveImageButton.clicked.connect(self._remove_image)
        d.playlistEditButtonBox.accepted.connect(self._on_save)
        d.playlistEditButtonBox.rejected.connect(d.reject)

    def run(self) -> PlaylistDetails | None:
        """Show the window. Returns what was entered, or None if it was cancelled."""
        d = self.dialog
        details = None
        if d.exec() == QDialog.DialogCode.Accepted:
            details = PlaylistDetails(
                d.playlistEditTitle.text().strip(),
                d.playlistEditDescription.toPlainText().strip(),
                self._icon,
                self._icon_changed,
                self._animation,
            )
        self._stop_preview()
        d.deleteLater()
        return details

    def _on_save(self) -> None:
        d = self.dialog
        problem = self._check_name(d.playlistEditTitle.text())
        if problem is not None:
            QMessageBox.warning(d, "Playlist name", problem)
            d.playlistEditTitle.setFocus()
            return
        d.accept()

    def _stop_preview(self) -> None:
        if self._playing is not None:
            self._playing.dispose()
            self._playing = None

    def _show_frame(self, frame: QImage) -> None:
        self.dialog.playlistEditImage.setPixmap(QPixmap.fromImage(frame))

    def _show_icon(self) -> None:
        d = self.dialog
        self._stop_preview()
        if self._animation is not None:
            self._playing = IconAnimation(self._animation)
            self._playing.frame_ready.connect(self._show_frame)
            self._playing.start()
        if self._icon is None:
            pixmap = self._placeholder.pixmap(PREVIEW_SIZE, PREVIEW_SIZE)
        else:
            pixmap = QPixmap.fromImage(self._icon)
        d.playlistEditImage.setPixmap(pixmap)
        d.playlistEditRemoveImageButton.setEnabled(self._icon is not None)

    def _choose_image(self) -> None:
        start_dir = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)
        path, _ = QFileDialog.getOpenFileName(
            self.dialog,
            "Choose an image for the playlist",
            start_dir,
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
        )
        if not path:
            return
        image = QImage(path)
        if image.isNull():
            QMessageBox.warning(self.dialog, "Playlist image", "That image could not be read.")
            return
        self._icon = make_square_icon(image)
        self._icon_changed = True
        self._animation = Path(path) if is_animated_gif(path) else None
        self._show_icon()

    def _remove_image(self) -> None:
        self._icon = None
        self._icon_changed = True
        self._animation = None
        self._show_icon()
