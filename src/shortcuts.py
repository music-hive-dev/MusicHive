"""Keyboard shortcuts: every action of the main window can have one, and the
Shortcuts tab of the settings window changes them.

The defaults are the shortcuts player.ui gives the actions. Only the ones the
user changed are saved, as "shortcuts/<action name>" (an empty text means none).
"""

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QDialog, QMainWindow, QMessageBox, QTreeWidgetItem

SETTINGS_GROUP = "shortcuts"
OTHER_GROUP_TITLE = "Other"  # actions that are in no menu
ACTION_ROLE = Qt.ItemDataRole.UserRole


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


class ShortcutsSettingsTab:
    """Connects the Shortcuts tab of settings.ui: pick an action, press the keys."""

    def __init__(self, dialog: QDialog, shortcuts: Shortcuts) -> None:
        self._dialog = d = dialog
        self._shortcuts = shortcuts
        self._items: dict[QAction, QTreeWidgetItem] = {}

        tree = d.shortcutsTree
        tree.clear()
        for title, actions in shortcuts.groups:
            group = QTreeWidgetItem(tree, [title])
            group.setFlags(Qt.ItemFlag.ItemIsEnabled)
            group.setFirstColumnSpanned(True)
            for action in actions:
                item = QTreeWidgetItem(group, [action_title(action), ""])
                item.setData(0, ACTION_ROLE, action)
                if action.toolTip() and action.toolTip() != action_title(action):
                    item.setToolTip(0, action.toolTip())
                self._items[action] = item
                self._show(action)
        tree.expandAll()
        tree.resizeColumnToContents(0)

        d.shortcutsEdit.setMaximumSequenceLength(1)  # one key combination, not a chord
        d.shortcutsEdit.editingFinished.connect(self._on_keys_entered)
        tree.currentItemChanged.connect(lambda _item, _previous: self._show_selected())
        d.shortcutsClear.clicked.connect(lambda _checked=False: self._assign(QKeySequence()))
        d.shortcutsReset.clicked.connect(lambda _checked=False: self._reset_selected())
        d.shortcutsResetAll.clicked.connect(lambda _checked=False: self._reset_all())
        self._show_selected()

    def _selected(self) -> QAction | None:
        item = self._dialog.shortcutsTree.currentItem()
        return item.data(0, ACTION_ROLE) if item is not None else None

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
