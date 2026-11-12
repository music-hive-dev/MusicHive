"""Type to find: typing in a list or table jumps to the first song that matches.

In a table it looks in the column the table is sorted by (or Title, when it is
in library order), and only if nothing matches there in the other columns; in a
list, in what the rows show (the title). While a search
is going on, Space is part of it instead of pausing the music.
"""

import time

from PySide6.QtCore import QEvent, QModelIndex, QObject, QPoint, Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QAbstractItemView, QComboBox, QTableView, QToolTip, QWidget

FIND_RESET_KEY = "general/findResetSeconds"
DEFAULT_RESET_SECONDS = 1.0
_reset_seconds = DEFAULT_RESET_SECONDS  # after a pause this long, typing starts a new search
FIND_TIP_OFFSET = (6, 0)  # pixels right of and below the mouse pointer
TITLE_COLUMN = "Title"
_COMMAND_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.MetaModifier
)


class TypeAheadSearch(QObject):
    """Watches the keys of one item view and moves to the row that matches."""

    def __init__(self, view: QAbstractItemView) -> None:
        super().__init__(view)
        self._view = view
        self._text = ""
        self._last_key = 0.0
        view.installEventFilter(self)

    # ----- keys -----

    def _searching(self) -> bool:
        return bool(self._text) and time.monotonic() - self._last_key < _reset_seconds

    def _typed(self, event) -> str:
        """The character a key press types, or "" for keys that do something else."""
        if event.modifiers() & _COMMAND_MODIFIERS:
            return ""
        text = event.text()
        if len(text) != 1 or not text.isprintable():
            return ""
        if text == " " and not self._searching():
            return ""  # Space on its own still plays and pauses
        return text

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.ShortcutOverride:
            # Claim typed characters before a shortcut (Space: Play/Pause) gets them.
            if self._typed(event) or (
                self._searching() and event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Escape)
            ):
                event.accept()
                return True
        elif kind == QEvent.Type.KeyPress:
            text = self._typed(event)
            if text:
                self._add(text)
                return True
            if self._searching() and event.key() == Qt.Key.Key_Backspace:
                self._text = self._text[:-1]
                self._last_key = time.monotonic()
                self._show_text()
                if self._text:
                    self._find(from_next=False)
                return True
            if self._searching() and event.key() == Qt.Key.Key_Escape:
                self._end()
                return True
        elif kind == QEvent.Type.FocusOut:
            self._end()
        return False

    def _add(self, character: str) -> None:
        if not self._searching():
            self._text = ""
        self._text += character
        self._last_key = time.monotonic()
        self._show_text()
        # The same letter again and again steps through the songs that start with it.
        repeated = len(self._text) > 1 and set(self._text) == {character}
        self._find(from_next=repeated or len(self._text) == 1)

    def _end(self) -> None:
        self._text = ""
        QToolTip.hideText()

    def _show_text(self) -> None:
        view = self._view
        if self._text:
            # Beside the mouse pointer, where the eyes are; QToolTip keeps it on screen.
            position = QCursor.pos() + QPoint(*FIND_TIP_OFFSET)
            QToolTip.showText(position, f"Find: {self._text}", view)
        else:
            QToolTip.hideText()

    # ----- finding -----

    def _columns(self) -> list[int]:
        """The columns to look in, best first: a table's sorted column (or Title, when
        it is in library order), then its other visible columns from left to right."""
        view, model = self._view, self._view.model()
        if not isinstance(view, QTableView):
            return [0]
        header = view.horizontalHeader()
        visible = [
            header.logicalIndex(position)
            for position in range(header.count())
            if not header.isSectionHidden(header.logicalIndex(position))
        ]
        first = header.sortIndicatorSection()
        if not (view.isSortingEnabled() and 0 <= first < model.columnCount()):
            titles = [
                column
                for column in range(model.columnCount())
                if model.headerData(column, Qt.Orientation.Horizontal) == TITLE_COLUMN
            ]
            first = titles[0] if titles else (visible[0] if visible else 0)
        return [first, *(column for column in visible if column != first)]

    def _find(self, from_next: bool) -> None:
        """Go to the first song, from the current one on, that matches what was typed.

        The first column is searched first; only if no song matches there are the
        other columns searched, one after another.
        """
        view, model = self._view, self._view.model()
        if model is None or model.rowCount() == 0:
            return
        wanted = self._text.casefold()
        if len(set(wanted)) == 1:
            wanted = wanted[0]  # "bbb" steps through the b's
        count = model.rowCount()
        current = view.currentIndex().row()
        start = (current + 1 if from_next else current) % count if current >= 0 else 0
        columns = self._columns()
        for column in columns:
            row = self._find_in(column, wanted, start)
            if row is not None:
                self._go_to(model.index(row, columns[0]))
                return

    def _find_in(self, column: int, wanted: str, start: int) -> int | None:
        """The first row from `start` on (wrapping round) whose text in this column
        starts with `wanted`; if none does, the first that contains it."""
        model = self._view.model()
        count = model.rowCount()
        containing = None  # the first row that has it somewhere else than at the start
        for offset in range(count):
            row = (start + offset) % count
            value = model.data(model.index(row, column))
            text = str(value).casefold() if value is not None else ""
            if text.startswith(wanted):
                return row
            if containing is None and wanted in text:
                containing = row
        return containing

    def _go_to(self, index: QModelIndex) -> None:
        view = self._view
        view.setCurrentIndex(index)  # also selects it (and only it)
        view.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)


def set_reset_seconds(seconds: float) -> None:
    """How long a pause in typing starts a new search (Settings, General)."""
    global _reset_seconds
    _reset_seconds = seconds


def add_type_ahead(window: QWidget) -> list[TypeAheadSearch]:
    """Type to find in every list and table of the window that takes key presses."""
    searches = []
    for view in window.findChildren(QAbstractItemView):
        if view.focusPolicy() == Qt.FocusPolicy.NoFocus or _inside_combo_box(view):
            continue
        searches.append(TypeAheadSearch(view))
    return searches


def _inside_combo_box(widget: QWidget) -> bool:
    """A combo box's drop-down list has its own type-to-find."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QComboBox):
            return True
        parent = parent.parentWidget()
    return False
