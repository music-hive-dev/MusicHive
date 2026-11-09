"""The side list: plain text sections that open and close.

    Playlists
        Folder
            Playlist
        Playlist
    Computer
        Pinned folder
    Devices
        Plugged-in device
"""

from PySide6.QtCore import QModelIndex, QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPainter, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QStyledItemDelegate

from src.track_list import TRACK_DRAG_MIME, read_track_drag

SECTION, FOLDER, PLAYLIST, PIN, DEVICE = "section", "folder", "playlist", "pin", "device"
PLAYLISTS_SECTION, COMPUTER_SECTION, DEVICES_SECTION = "playlists", "computer", "devices"

_KIND_ROLE = Qt.ItemDataRole.UserRole + 1
_VALUE_ROLE = Qt.ItemDataRole.UserRole + 2
_JOINED_ICON_SIDE = 48  # pixels per icon; they are shrunk to the row's size when drawn


def entry_id(kind: str, value: str) -> str:
    """Identifies a row across rebuilds of the list."""
    return f"{kind}:{value}"


def _joined_icon(arrow: QIcon | None, icon: QIcon) -> QIcon:
    """An arrow (or the space for one) and an icon, side by side as one wide icon."""
    side = _JOINED_ICON_SIDE
    gap = side // 4
    pixmap = QPixmap(2 * side + gap, side)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    if arrow is not None:
        arrow.paint(painter, QRect(0, 0, side, side))
    icon.paint(painter, QRect(side + gap, 0, side, side))
    painter.end()
    return QIcon(pixmap)


class SideListDelegate(QStyledItemDelegate):
    """Lets a row's icon be wider than it is tall."""

    def initStyleOption(self, option, index) -> None:
        # Read before the base class, which squeezes a wide icon into a small square.
        height = option.decorationSize.height()
        super().initStyleOption(option, index)
        option.decorationSize = option.icon.actualSize(QSize(10 * height, height))


class SideListModel(QStandardItemModel):
    """The rows of the side list. Songs can be dropped on a playlist."""

    songs_dropped = Signal(str, list)  # playlist name, library rows
    drop_refused = Signal(str)  # why the songs being dragged cannot be dropped here

    def __init__(
        self,
        closed_icon: QIcon,
        open_icon: QIcon,
        playlist_icon: QIcon,
        auto_playlist_icon: QIcon,
        folder_icon: QIcon,
        device_icon: QIcon,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._closed_icon = closed_icon
        self._open_icon = open_icon
        self._closed_folder_icon = _joined_icon(closed_icon, folder_icon)
        self._open_folder_icon = _joined_icon(open_icon, folder_icon)
        self._playlist_icon = _joined_icon(None, playlist_icon)
        self._auto_playlist_icon = _joined_icon(None, auto_playlist_icon)
        self._pin_icon = _joined_icon(None, folder_icon)
        self._device_icon = _joined_icon(None, device_icon)
        self._devices: list[tuple[str, str]] = []
        self._devices_section: QStandardItem | None = None
        self._playlists: dict[str, QStandardItem] = {}
        self._locked: set[str] = set()

    def rebuild(
        self,
        folders: list[str],
        playlists: list[tuple[str, str, bool, bool]],
        pins: list[str],
    ) -> None:
        """Fill the list. `playlists` holds (name, folder, locked, is_auto); "" means no folder."""
        self.clear()
        self._playlists.clear()
        self._locked = {name for name, _folder, locked, _auto in playlists if locked}

        section = self._opening_item("Playlists", SECTION, PLAYLISTS_SECTION)
        self.appendRow(section)
        folder_items = {}
        for folder in folders:
            folder_items[folder] = self._opening_item(folder, FOLDER, folder)
            section.appendRow(folder_items[folder])
        for name, folder, _locked, is_auto in playlists:
            item = self._item(name, PLAYLIST, name)
            item.setIcon(self._auto_playlist_icon if is_auto else self._playlist_icon)
            item.setSelectable(True)
            item.setDropEnabled(True)
            folder_items.get(folder, section).appendRow(item)
            self._playlists[name] = item

        computer = self._opening_item("Computer", SECTION, COMPUTER_SECTION)
        self.appendRow(computer)
        for path in pins:
            item = self._item(path.rstrip("/").rsplit("/", 1)[-1] or path, PIN, path)
            item.setIcon(self._pin_icon)
            item.setToolTip(path)
            computer.appendRow(item)

        self._devices_section = self._opening_item("Devices", SECTION, DEVICES_SECTION)
        self.appendRow(self._devices_section)
        self._show_devices()

    def set_devices(self, devices: list[tuple[str, str]]) -> None:
        """Show these plugged-in devices, given as (name, folder)."""
        self._devices = devices
        self._show_devices()

    def _show_devices(self) -> None:
        section = self._devices_section
        if section is None:
            return
        section.removeRows(0, section.rowCount())
        for name, path in self._devices:
            item = self._item(name, DEVICE, path)
            item.setIcon(self._device_icon)
            item.setToolTip(path)
            section.appendRow(item)

    def kind(self, index: QModelIndex) -> str | None:
        return index.data(_KIND_ROLE) if index.isValid() else None

    def value(self, index: QModelIndex) -> str | None:
        return index.data(_VALUE_ROLE) if index.isValid() else None

    def id_of(self, index: QModelIndex) -> str:
        return entry_id(self.kind(index), self.value(index))

    def playlist_index(self, name: str) -> QModelIndex:
        item = self._playlists.get(name)
        return item.index() if item is not None else QModelIndex()

    def opening_indexes(self) -> list[QModelIndex]:
        """Every row that opens and closes."""
        indexes = []
        for row in range(self.rowCount()):
            section = self.item(row)
            indexes.append(section.index())
            for child in range(section.rowCount()):
                if section.child(child).data(_KIND_ROLE) == FOLDER:
                    indexes.append(section.child(child).index())
        return indexes

    def set_open(self, index: QModelIndex, is_open: bool) -> None:
        """Make a row's arrow show whether it is open."""
        item = self.itemFromIndex(index)
        if item is not None:
            item.setIcon(self._arrow_icon(self.kind(index), is_open))

    def is_locked(self, name: str) -> bool:
        return name in self._locked

    def set_locked(self, name: str, locked: bool) -> None:
        (self._locked.add if locked else self._locked.discard)(name)

    # ----- dropping songs on a playlist -----

    def mimeTypes(self) -> list[str]:
        return [TRACK_DRAG_MIME]

    def canDropMimeData(self, data, action, row, column, parent) -> bool:
        if not data.hasFormat(TRACK_DRAG_MIME) or self.kind(parent) != PLAYLIST:
            return False
        name = self.value(parent)
        if self.is_locked(name):
            self.drop_refused.emit(f"'{name}' is locked")
            return False
        return read_track_drag(data)[1] != name

    def dropMimeData(self, data, action, row, column, parent) -> bool:
        if not self.canDropMimeData(data, action, row, column, parent):
            return False
        self.songs_dropped.emit(self.value(parent), read_track_drag(data)[0])
        return True

    # ----- internal -----

    @staticmethod
    def _item(text: str, kind: str, value: str) -> QStandardItem:
        item = QStandardItem(text)
        item.setData(kind, _KIND_ROLE)
        item.setData(value, _VALUE_ROLE)
        item.setEditable(False)
        item.setSelectable(False)
        item.setDragEnabled(False)
        item.setDropEnabled(False)
        return item

    def _opening_item(self, text: str, kind: str, value: str) -> QStandardItem:
        item = self._item(text, kind, value)
        item.setIcon(self._arrow_icon(kind, False))
        return item

    def _arrow_icon(self, kind: str, is_open: bool) -> QIcon:
        if kind == FOLDER:
            return self._open_folder_icon if is_open else self._closed_folder_icon
        return self._open_icon if is_open else self._closed_icon
