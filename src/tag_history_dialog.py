"""Edit > Tag Change History (tag_history.ui): the kept saves, and undoing one."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QHeaderView, QMessageBox, QTableWidgetItem, QWidget

from src import tag_history
from src.lastfm_dialog import load_dialog
from src.tag_history import HistoryEntry
from src.tag_writer import TagWriter

WHEN_COLUMN, CHANGE_COLUMN, SONGS_COLUMN, STATE_COLUMN = range(4)
INDEX_ROLE = Qt.ItemDataRole.UserRole


def _when(seconds: float) -> str:
    moment = datetime.fromtimestamp(seconds)
    today = datetime.now().date()
    if moment.date() == today:
        return f"Today {moment:%H:%M}"
    if (today - moment.date()).days == 1:
        return f"Yesterday {moment:%H:%M}"
    return f"{moment:%Y-%m-%d %H:%M}"


class TagHistoryWindow(QObject):
    """Connects the widgets of tag_history.ui."""

    def __init__(
        self,
        ui_path: Path,
        parent: QWidget,
        tag_writer: TagWriter,
        show_status: Callable[[str], None],
    ) -> None:
        super().__init__(parent)
        self.dialog = d = load_dialog(ui_path, parent)
        self._tag_writer = tag_writer
        self._show_status = show_status
        self._entries: list[HistoryEntry] = []
        # A finished save (an undo too) may add an entry: show it.
        tag_writer.tags_saved.connect(lambda _result: self._refresh_if_open())

        table = d.tagHistoryList
        header = table.horizontalHeader()
        header.setSectionResizeMode(WHEN_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(CHANGE_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(SONGS_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(STATE_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        table.itemSelectionChanged.connect(self._update_buttons)
        table.cellDoubleClicked.connect(lambda _row, _column: self._undo())
        d.tagHistoryUndoButton.clicked.connect(lambda _checked=False: self._undo())
        d.tagHistoryButtonBox.rejected.connect(d.reject)

    def show(self) -> None:
        self._refresh()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _refresh_if_open(self) -> None:
        if self.dialog.isVisible():
            self._refresh()

    def _refresh(self) -> None:
        chosen = self._selected()
        self._entries = tag_history.list_entries()
        table = self.dialog.tagHistoryList
        table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            when = QTableWidgetItem(_when(entry.when))
            when.setData(INDEX_ROLE, row)
            change = QTableWidgetItem(entry.description)
            change.setToolTip(f"{entry.description}\n{entry.root}")
            songs = QTableWidgetItem()
            songs.setData(Qt.ItemDataRole.DisplayRole, entry.songs)
            state = QTableWidgetItem("Undone" if entry.undone else "")
            table.setItem(row, WHEN_COLUMN, when)
            table.setItem(row, CHANGE_COLUMN, change)
            table.setItem(row, SONGS_COLUMN, songs)
            table.setItem(row, STATE_COLUMN, state)
            if chosen is not None and entry.id == chosen.id:
                table.selectRow(row)
        self._update_buttons()

    def _selected(self) -> HistoryEntry | None:
        table = self.dialog.tagHistoryList
        if not table.selectionModel().hasSelection():
            return None
        row = table.currentRow()
        return self._entries[row] if 0 <= row < len(self._entries) else None

    def _update_buttons(self) -> None:
        entry = self._selected()
        self.dialog.tagHistoryUndoButton.setEnabled(entry is not None and not entry.undone)

    def _undo(self) -> None:
        entry = self._selected()
        if entry is None or entry.undone:
            return
        if not entry.db_path.exists():
            QMessageBox.information(
                self.dialog, "Undo", "The library these songs belonged to is no longer there."
            )
            return
        jobs = tag_history.restore_jobs(entry)
        if not jobs:
            QMessageBox.warning(self.dialog, "Undo", "What these songs had before could not be read.")
            return
        count = f"{len(jobs)} song{'' if len(jobs) == 1 else 's'}"
        answer = QMessageBox.question(
            self.dialog,
            "Undo",
            f"Put back what {count} had before “{entry.description}”?\n\n"
            "If those tags were changed again since, that is replaced too.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._tag_writer.save_tags(
            jobs, entry.root, entry.db_path, f"Undo: {entry.description}", undoes=entry.id
        )
        tag_history.set_undone(entry.id, True)
        if entry.undoes:  # undoing an undo brings the change back
            tag_history.set_undone(entry.undoes, False)
        self._show_status(f"Undoing “{entry.description}” in {count}…")
        self._refresh()
