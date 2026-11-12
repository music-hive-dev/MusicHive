"""Keyboard shortcuts: every action of the main window can have one, and the
Shortcuts tab of the settings window changes them.

The defaults are the shortcuts player.ui gives the actions. Only the ones the
user changed are saved, as "shortcuts/<action name>" (an empty text means none).
"""

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTreeWidget,
    QTreeWidgetItem,
)

SETTINGS_GROUP = "shortcuts"
OTHER_GROUP_TITLE = "Other"  # actions that are in no menu
ACTION_ROLE = Qt.ItemDataRole.UserRole
KEYS_COLUMN_WIDTH = 150  # pixels, for the shortcut next to each action
HEADING_ROWS = 2  # a group's heading takes about as much room as this many rows


def action_title(action: QAction) -> str:
    return action.text().replace("&&", "\0").replace("&", "").replace("\0", "&")


class Shortcuts:
    """Knows each action's default shortcut and applies the user's own."""

    def __init__(self, window: QMainWindow, settings: QSettings) -> None:
        self._window = window
        self._settings = settings
        self.groups = self._find_actions()
        self.defaults = {
            action.objectName(): action.shortcut()
            for _title, actions in self.groups
            for action in actions
        }
        for _title, actions in self.groups:
            for action in actions:
                saved = settings.value(self._key(action), None)
                if saved is not None:
                    action.setShortcut(QKeySequence(str(saved), QKeySequence.PortableText))

    def _find_actions(self) -> list[tuple[str, list[QAction]]]:
        """The actions as the menu bar orders them, then the ones in no menu."""
        groups = []
        seen: set[QAction] = set()

        def collect(menu, into: list[QAction]) -> None:
            for action in menu.actions():
                if action.menu() is not None:
                    collect(action.menu(), into)
                elif not action.isSeparator() and action.objectName().startswith("action"):
                    if action not in seen:
                        seen.add(action)
                        into.append(action)

        for entry in self._window.menuBar().actions():
            if entry.menu() is not None:
                actions: list[QAction] = []
                collect(entry.menu(), actions)
                if actions:
                    groups.append((action_title(entry), actions))
        others = [
            action
            for action in self._window.findChildren(
                QAction, options=Qt.FindChildOption.FindDirectChildrenOnly
            )
            if action.objectName().startswith("action") and action not in seen
        ]
        if others:
            others.sort(key=lambda action: action_title(action).casefold())
            groups.append((OTHER_GROUP_TITLE, others))
        return groups

    def _key(self, action: QAction) -> str:
        return f"{SETTINGS_GROUP}/{action.objectName()}"

    def all_actions(self) -> list[QAction]:
        return [action for _title, actions in self.groups for action in actions]

    def owner_of(self, sequence: QKeySequence, besides: QAction) -> QAction | None:
        """The other action that already has this shortcut, if any."""
        if sequence.isEmpty():
            return None
        for action in self.all_actions():
            if action is not besides and action.shortcut() == sequence:
                return action
        return None

    def set(self, action: QAction, sequence: QKeySequence) -> None:
        """Give the action this shortcut (an empty one takes it away) and remember it."""
        action.setShortcut(sequence)
        if sequence == self.defaults.get(action.objectName(), QKeySequence()):
            self._settings.remove(self._key(action))
        else:
            self._settings.setValue(
                self._key(action), sequence.toString(QKeySequence.PortableText)
            )

    def default(self, action: QAction) -> QKeySequence:
        return self.defaults.get(action.objectName(), QKeySequence())


def _split_in_two(groups: list[tuple[str, list[QAction]]]) -> int:
    """How many of the groups go in the left column, so both are about as long.
    The groups keep the menu bar's order, down the left column and then the right."""
    sizes = [len(actions) + HEADING_ROWS for _title, actions in groups]
    total = sum(sizes)
    best, best_difference, left = len(groups), total, 0
    for count, size in enumerate(sizes, start=1):
        left += size
        difference = abs(total - 2 * left)
        if difference < best_difference:
            best, best_difference = count, difference
    return best


class ShortcutsSettingsTab:
    """Connects the Shortcuts tab of settings.ui: pick an action, press the keys.

    Each menu gets a heading and a small list of its actions, in two columns.
    """

    def __init__(self, dialog: QDialog, shortcuts: Shortcuts) -> None:
        self._dialog = d = dialog
        self._shortcuts = shortcuts
        self._items: dict[QAction, QTreeWidgetItem] = {}
        self._lists: list[QTreeWidget] = []
        self._current: QAction | None = None

        split = _split_in_two(shortcuts.groups)
        for index, (title, actions) in enumerate(shortcuts.groups):
            column = d.shortcutsColumnLeft if index < split else d.shortcutsColumnRight
            self._add_group(column, title, actions)

        d.shortcutsEdit.setMaximumSequenceLength(1)  # one key combination, not a chord
        d.shortcutsEdit.editingFinished.connect(self._on_keys_entered)
        d.shortcutsClear.clicked.connect(lambda _checked=False: self._assign(QKeySequence()))
        d.shortcutsReset.clicked.connect(lambda _checked=False: self._reset_selected())
        d.shortcutsResetAll.clicked.connect(lambda _checked=False: self._reset_all())
        self._show_selected()

    def _add_group(self, column, title: str, actions: list[QAction]) -> None:
        """A heading and the list of the group's actions, above the column's spacer."""
        parent = self._dialog.shortcutsContents
        heading = QLabel(title, parent)
        heading.setObjectName("shortcutsGroupTitle")  # styled in player.ui
        actions_list = QTreeWidget(parent)
        actions_list.setObjectName("shortcutsGroupList")  # styled in player.ui
        actions_list.setColumnCount(2)
        actions_list.setHeaderHidden(True)
        actions_list.setRootIsDecorated(False)
        actions_list.setUniformRowHeights(True)
        actions_list.setFrameShape(QFrame.Shape.NoFrame)
        actions_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        actions_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        actions_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = actions_list.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1, KEYS_COLUMN_WIDTH)
        for action in actions:
            item = QTreeWidgetItem(actions_list, [action_title(action), ""])
            item.setData(0, ACTION_ROLE, action)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if action.toolTip() and action.toolTip() != action_title(action):
                item.setToolTip(0, action.toolTip())
            self._items[action] = item
            self._show(action)
        # As tall as its rows: the page scrolls, not each list. Polished first, so
        # the row sizes include the stylesheet's margins and padding.
        actions_list.ensurePolished()
        height = sum(actions_list.sizeHintForRow(row) for row in range(len(actions)))
        actions_list.setFixedHeight(height + 2 * actions_list.frameWidth())
        actions_list.currentItemChanged.connect(
            lambda item, _previous, source=actions_list: self._on_picked(source, item)
        )
        layout = column
        layout.insertWidget(layout.count() - 1, heading)
        layout.insertWidget(layout.count() - 1, actions_list)
        self._lists.append(actions_list)

    def _on_picked(self, source: QTreeWidget, item: QTreeWidgetItem | None) -> None:
        """One action is picked at a time, whichever list it is in."""
        if item is None:
            return
        for other in self._lists:
            if other is not source:
                other.blockSignals(True)
                other.clearSelection()
                other.setCurrentItem(None)
                other.blockSignals(False)
        self._current = item.data(0, ACTION_ROLE)
        self._show_selected()

    def _selected(self) -> QAction | None:
        return self._current

    def _show(self, action: QAction) -> None:
        item = self._items[action]
        item.setText(1, action.shortcut().toString(QKeySequence.NativeText))
        font = item.font(1)
        font.setBold(action.shortcut() != self._shortcuts.default(action))  # changed
        item.setFont(1, font)

    def _show_selected(self) -> None:
        d = self._dialog
        action = self._selected()
        for widget in (d.shortcutsEdit, d.shortcutsClear, d.shortcutsReset):
            widget.setEnabled(action is not None)
        d.shortcutsEdit.setKeySequence(action.shortcut() if action else QKeySequence())

    def _on_keys_entered(self) -> None:
        action = self._selected()
        if action is not None and self._dialog.shortcutsEdit.keySequence() != action.shortcut():
            self._assign(self._dialog.shortcutsEdit.keySequence())

    def _assign(self, sequence: QKeySequence, action: QAction | None = None) -> None:
        """Give the selected action this shortcut; a taken one moves if the user agrees."""
        action = action or self._selected()
        if action is None:
            return
        owner = self._shortcuts.owner_of(sequence, action)
        if owner is not None:
            keys = sequence.toString(QKeySequence.NativeText)
            answer = QMessageBox.question(
                self._dialog,
                "Shortcut in use",
                f"{keys} is the shortcut of “{action_title(owner)}”.\n"
                f"Move it to “{action_title(action)}”?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._show_selected()  # put the old shortcut back in the field
                return
            self._shortcuts.set(owner, QKeySequence())
            self._show(owner)
        self._shortcuts.set(action, sequence)
        self._show(action)
        self._show_selected()

    def _reset_selected(self) -> None:
        action = self._selected()
        if action is not None:
            self._assign(self._shortcuts.default(action), action)

    def _reset_all(self) -> None:
        answer = QMessageBox.question(
            self._dialog, "Reset shortcuts", "Put every shortcut back as it was at first?"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for action in self._shortcuts.all_actions():
            self._shortcuts.set(action, self._shortcuts.default(action))
            self._show(action)
        self._show_selected()
