"""The playlist window (playlist_edit.ui): a playlist's title, image and description."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QColor, QIcon, QImage, QPixmap
from PySide6.QtWidgets import QColorDialog, QDialog, QFileDialog, QMessageBox, QWidget

from src.auto_playlist import COMPARISONS, LIMIT_ORDERS, LIMIT_UNITS
from src.header_box import box_colour
from src.i18n import tr
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
    equaliser: str = ""  # the equaliser preset used while it plays ("" for none)
    visualiser: str = ""  # the visualiser preset used while it plays ("" for none)
    colour: str = ""  # the header box's colour, "#rrggbb"; "" takes it from the picture


def fill_preset_combo(combo, names: list[str], chosen: str) -> None:
    """The preset names after the combo's first entry ("None"), with `chosen` selected.
    A preset that has since been deleted is still listed, so it is not lost unseen."""
    while combo.count() > 1:
        combo.removeItem(1)
    combo.addItems(names)
    if chosen and chosen not in names:
        combo.addItem(chosen)
    combo.setCurrentIndex(combo.findText(chosen) if chosen else 0)


def chosen_preset(combo) -> str:
    """The preset picked in a combo filled by fill_preset_combo; "" for none."""
    return combo.currentText() if combo.currentIndex() > 0 else ""


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
        equaliser: str = "",
        visualiser: str = "",
        equaliser_names: list[str] | None = None,
        visualiser_names: list[str] | None = None,
        colour: str = "",
    ) -> None:
        """`equaliser_names` and `visualiser_names` are the presets to choose from.
        `colour` is the header box's chosen colour; "" takes it from the picture."""
        self._colour = colour
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
        fill_preset_combo(d.playlistEditEqualiser, equaliser_names or [], equaliser)
        fill_preset_combo(d.playlistEditVisualiser, visualiser_names or [], visualiser)
        self._show_icon()

        self._image_click = ClickFilter(d.playlistEditImage)
        self._image_click.clicked.connect(self._choose_image)
        d.playlistEditChooseImageButton.clicked.connect(self._choose_image)
        d.playlistEditRemoveImageButton.clicked.connect(self._remove_image)
        d.playlistEditColourButton.clicked.connect(lambda _checked=False: self._choose_colour())
        d.playlistEditResetColourButton.clicked.connect(lambda _checked=False: self._reset_colour())
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
                equaliser=chosen_preset(d.playlistEditEqualiser),
                visualiser=chosen_preset(d.playlistEditVisualiser),
                colour=self._colour,
            )
        self._stop_preview()
        d.deleteLater()
        return details

    def _on_save(self) -> None:
        d = self.dialog
        problem = self._check_name(d.playlistEditTitle.text())
        if problem is not None:
            QMessageBox.warning(d, tr("Playlist name"), problem)
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
        self._show_colour()  # the automatic colour follows the picture

    # ----- the header colour -----

    def _shown_colour(self) -> QColor | None:
        """The colour the header will have: the chosen one, or the picture's."""
        if self._colour:
            return QColor(self._colour)
        return box_colour(self._icon)

    def _show_colour(self) -> None:
        d = self.dialog
        button = d.playlistEditColourButton
        colour = self._shown_colour()
        button.setText(
            tr("Custom {colour}").format(colour=self._colour) if self._colour else tr("Automatic")
        )
        if colour is not None and colour.isValid():
            text = "#000000" if colour.lightness() > 150 else "#ffffff"  # readable on the swatch
            button.setStyleSheet(
                f"QPushButton#playlistEditColourButton {{ background-color: {colour.name()}; color: {text}; }}"
            )
        else:
            button.setStyleSheet("")  # no picture: the box keeps its usual colour
        d.playlistEditResetColourButton.setEnabled(bool(self._colour))

    def _choose_colour(self) -> None:
        start = self._shown_colour()
        colour = QColorDialog.getColor(
            start if start is not None and start.isValid() else QColor("#312d34"),
            self.dialog,
            tr("Header colour"),
        )
        if colour.isValid():
            self._colour = colour.name()
            self._show_colour()

    def _reset_colour(self) -> None:
        self._colour = ""
        self._show_colour()

    def _choose_image(self) -> None:
        start_dir = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)
        path, _ = QFileDialog.getOpenFileName(
            self.dialog,
            tr("Choose an image for the playlist"),
            start_dir,
            tr("Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"),
        )
        if not path:
            return
        image = QImage(path)
        if image.isNull():
            QMessageBox.warning(
                self.dialog, tr("Playlist image"), tr("That image could not be read.")
            )
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
            self._add_row(rule)  # no rules: every song of the library qualifies

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
