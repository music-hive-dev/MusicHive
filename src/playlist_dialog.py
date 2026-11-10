"""The playlist window (playlist_edit.ui): a playlist's title, image and description."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox, QWidget

from src.auto_playlist import COMPARISONS, LIMIT_ORDERS, LIMIT_UNITS
from src.lastfm_dialog import load_dialog
from src.number_field import NumberField
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
    rules: list[dict] | None = None  # an auto-playlist's rules (None for a normal playlist)
    match_any: bool = False
    no_duplicates: bool = False
    limit: dict | None = None


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


class AutoPlaylistEditDialog(PlaylistEditDialog):
    """The window of an auto-playlist: the same fields, plus its rules.

    `tags` lists the tags on offer as (name, title).
    """

    def __init__(
        self,
        ui_path: Path,
        rule_ui_path: Path,
        tags: list[tuple[str, str]],
        rules: list[dict],
        match_any: bool,
        no_duplicates: bool,
        limit: dict | None,
        *args,
    ) -> None:
        super().__init__(ui_path, *args)
        self._rule_ui_path = rule_ui_path
        self._tags = tags
        self._rows: list[QWidget] = []
        self._rules: list[dict] = []
        self._match_any = match_any
        self._no_duplicates = no_duplicates
        self.dialog.autoPlaylistMatchCombo.setCurrentIndex(1 if match_any else 0)  # all, any
        self.dialog.autoPlaylistNoDuplicatesCheck.setChecked(no_duplicates)
        self._limit = limit
        self._limit_amount = NumberField(self.dialog.autoPlaylistLimitAmount)
        self._show_limit(limit)
        self.dialog.autoPlaylistAddRuleButton.clicked.connect(
            lambda _checked=False: self._add_row()
        )
        for rule in rules:
            self._add_row(rule)
        if not rules:
            self._add_row()

    def run(self) -> PlaylistDetails | None:
        details = super().run()
        if details is not None:
            details.rules = self._rules
            details.match_any = self._match_any
            details.no_duplicates = self._no_duplicates
            details.limit = self._limit
        return details

    def _on_save(self) -> None:
        self._rules = [
            {
                "tag": row.ruleTagCombo.currentData(),
                "comparison": COMPARISONS[row.ruleComparisonCombo.currentIndex()],
                "value": row.ruleValueEdit.text().strip(),
            }
            for row in self._rows
            if row.ruleValueEdit.text().strip()  # a rule without a value says nothing
        ]
        self._match_any = self.dialog.autoPlaylistMatchCombo.currentIndex() == 1
        self._no_duplicates = self.dialog.autoPlaylistNoDuplicatesCheck.isChecked()
        self._limit = self._entered_limit()
        super()._on_save()

    def _show_limit(self, limit: dict | None) -> None:
        d = self.dialog
        for widget in (
            d.autoPlaylistLimitAmount,
            d.autoPlaylistLimitUnit,
            d.autoPlaylistLimitByLabel,
            d.autoPlaylistLimitOrder,
        ):
            widget.setEnabled(limit is not None)
            d.autoPlaylistLimitCheck.toggled.connect(widget.setEnabled)
        d.autoPlaylistLimitCheck.setChecked(limit is not None)
        if limit is not None:
            self._limit_amount.setValue(limit["amount"])
            d.autoPlaylistLimitUnit.setCurrentIndex(LIMIT_UNITS.index(limit["unit"]))
            d.autoPlaylistLimitOrder.setCurrentIndex(LIMIT_ORDERS.index(limit["order"]))

    def _entered_limit(self) -> dict | None:
        d = self.dialog
        if not d.autoPlaylistLimitCheck.isChecked():
            return None
        return {
            "amount": self._limit_amount.value(),
            "unit": LIMIT_UNITS[d.autoPlaylistLimitUnit.currentIndex()],
            "order": LIMIT_ORDERS[d.autoPlaylistLimitOrder.currentIndex()],
        }

    def _add_row(self, rule: dict | None = None) -> None:
        contents = self.dialog.autoPlaylistRulesContents
        row = load_dialog(self._rule_ui_path, contents)
        for name, title in self._tags:
            row.ruleTagCombo.addItem(title, name)
        if rule is None:
            row.ruleTagCombo.setCurrentIndex(max(0, row.ruleTagCombo.findData("genre")))
        else:
            row.ruleTagCombo.setCurrentIndex(max(0, row.ruleTagCombo.findData(rule["tag"])))
            if rule["comparison"] in COMPARISONS:
                row.ruleComparisonCombo.setCurrentIndex(COMPARISONS.index(rule["comparison"]))
            row.ruleValueEdit.setText(rule["value"])
        row.ruleRemoveButton.clicked.connect(lambda _checked=False, r=row: self._remove_row(r))
        self._rows.append(row)
        layout = contents.layout()
        layout.insertWidget(layout.count() - 1, row)  # above the spacer

    def _remove_row(self, row: QWidget) -> None:
        self._rows.remove(row)
        self.dialog.autoPlaylistRulesContents.layout().removeWidget(row)
        row.deleteLater()
