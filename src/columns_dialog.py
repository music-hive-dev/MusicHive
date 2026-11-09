"""The column window (columns_edit.ui): which tags the song tables show, and in what order."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QListWidget, QListWidgetItem, QWidget

from src.lastfm_dialog import load_dialog
from src.track_list import Column


class ColumnsDialog:
    """Connects the widgets of columns_edit.ui. Nothing is saved here: run()
    returns the chosen column keys, and the caller applies them."""

    def __init__(
        self, ui_path: Path, parent: QWidget, shown: list[Column], available: list[Column]
    ) -> None:
        self.dialog = load_dialog(ui_path, parent)
        d = self.dialog
        self._fill(d.columnsEditShownList, shown)
        self._fill(d.columnsEditAvailableList, available)

        d.columnsEditButtonBox.accepted.connect(d.accept)
        d.columnsEditButtonBox.rejected.connect(d.reject)
        shown_rows = d.columnsEditShownList.model()
        shown_rows.rowsInserted.connect(self._update_ok_button)
        shown_rows.rowsRemoved.connect(self._update_ok_button)
        self._update_ok_button()

    def run(self) -> list[str] | None:
        """Open the window. Returns the column keys in order, None if cancelled."""
        if self.dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        shown = self.dialog.columnsEditShownList
        return [
            shown.item(row).data(Qt.ItemDataRole.UserRole) for row in range(shown.count())
        ]

    @staticmethod
    def _fill(view: QListWidget, columns: list[Column]) -> None:
        for column in columns:
            item = QListWidgetItem(column.title)
            item.setData(Qt.ItemDataRole.UserRole, column.key)
            view.addItem(item)

    def _update_ok_button(self, _parent=None, _first=0, _last=0) -> None:
        """A table needs at least one column."""
        d = self.dialog
        ok = d.columnsEditButtonBox.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(d.columnsEditShownList.count() > 0)
