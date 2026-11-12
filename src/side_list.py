"""The side list: plain text sections that open and close.

    Playlists
        Folder
            Playlist
        Playlist
    Podcasts
        Pinned podcast
    Radio Stations
        Pinned station
    Computer
        Pinned folder
    Devices
        Plugged-in device

The sections can be put in another order by dragging one onto the section whose
place it should take. The pinned podcasts and stations are sorted by name.
"""

import json

from PySide6.QtCore import QEvent, QMimeData, QModelIndex, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QDrag, QIcon, QPainter, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QStyledItemDelegate, QTreeView

from src.track_list import TRACK_DRAG_MIME, read_track_drag

SECTION, FOLDER, PLAYLIST, PIN, DEVICE = "section", "folder", "playlist", "pin", "device"
PODCAST, STATION = "podcast", "station"  # pinned podcasts and radio stations
PLAYLISTS_SECTION, COMPUTER_SECTION, DEVICES_SECTION = "playlists", "computer", "devices"
PODCASTS_SECTION, STATIONS_SECTION = "podcasts", "stations"
SECTIONS = (  # the sections and their titles, in the order they start in
    (PLAYLISTS_SECTION, "Playlists"),
    (PODCASTS_SECTION, "Podcasts"),
    (STATIONS_SECTION, "Radio Stations"),
    (COMPUTER_SECTION, "Computer"),
    (DEVICES_SECTION, "Devices"),
)
SECTION_OF = {PODCAST: PODCASTS_SECTION, STATION: STATIONS_SECTION}  # pinned kind -> section
ROW_DRAG_MIME = "application/x-musichive-side-row"  # a section being moved

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


class SectionDragger(QObject):
    """Starts dragging a section's title.

    Qt only drags selected rows, and a section cannot be selected (clicking it
    opens or closes it), so the drag is started here instead.
    """

    def __init__(self, view: QTreeView) -> None:
        super().__init__(view)
        self._view = view
        self._pressed = QModelIndex()
        self._press_position = QPoint()
        view.viewport().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            index = self._view.indexAt(event.position().toPoint())
            model = self._view.model()
            is_section = index.isValid() and model.kind(index) == SECTION
            self._pressed = index if is_section else QModelIndex()
            self._press_position = event.position().toPoint()
        elif kind == QEvent.Type.MouseMove and self._pressed.isValid():
            if not event.buttons() & Qt.MouseButton.LeftButton:
                self._pressed = QModelIndex()
            elif (event.position().toPoint() - self._press_position).manhattanLength() >= (
                QApplication.startDragDistance()
            ):
                self._start_drag()
                return True
        elif kind == QEvent.Type.MouseButtonRelease:
            self._pressed = QModelIndex()
        return False

    def _start_drag(self) -> None:
        index, self._pressed = self._pressed, QModelIndex()
        model = self._view.model()
        drag = QDrag(self._view)
        drag.setMimeData(model.mimeData([index]))
        drag.setPixmap(self._view.viewport().grab(self._view.visualRect(index)))
        drag.exec(Qt.DropAction.MoveAction)


class SideListModel(QStandardItemModel):
    """The rows of the side list. Songs can be dropped on a playlist."""

    songs_dropped = Signal(str, list)  # playlist name, library rows
    drop_refused = Signal(str)  # why the songs being dragged cannot be dropped here
    # A section was dropped on another to take its place: (what moved, where to).
    section_moved = Signal(str, str)

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
        self._pinned: dict[str, list[tuple[str, str, QIcon]]] = {PODCAST: [], STATION: []}
        self._pinned_sections: dict[str, QStandardItem] = {}
        self.section_order = [value for value, _title in SECTIONS]
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

        sections = {}
        for value, title in SECTIONS:
            sections[value] = self._opening_item(title, SECTION, value)
            sections[value].setDragEnabled(True)  # the sections can be put in another order
            sections[value].setDropEnabled(True)
        order = [v for v in self.section_order if v in sections]
        order += [v for v, _title in SECTIONS if v not in order]  # sections new in this version
        for value in order:
            self.appendRow(sections[value])

        section = sections[PLAYLISTS_SECTION]
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

        self._pinned_sections = {
            PODCAST: sections[PODCASTS_SECTION],
            STATION: sections[STATIONS_SECTION],
        }
        self._show_pinned()

        computer = sections[COMPUTER_SECTION]
        for path in pins:
            item = self._item(path.rstrip("/").rsplit("/", 1)[-1] or path, PIN, path)
            item.setIcon(self._pin_icon)
            item.setToolTip(path)
            computer.appendRow(item)

        self._devices_section = sections[DEVICES_SECTION]
        self._show_devices()

    def set_pinned(self, kind: str, entries: list[tuple[str, str, QIcon]]) -> None:
        """Show these pinned podcasts (kind PODCAST) or stations, as (id, name, icon)."""
        self._pinned[kind] = entries
        self._show_pinned()

    def _show_pinned(self) -> None:
        for kind, section in self._pinned_sections.items():
            section.removeRows(0, section.rowCount())
            for value, name, icon in self._pinned[kind]:
                item = self._item(name, kind, value)
                item.setIcon(_joined_icon(None, icon))
                item.setToolTip(name)
                section.appendRow(item)

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
        return [TRACK_DRAG_MIME, ROW_DRAG_MIME]

    def supportedDropActions(self):
        return Qt.DropAction.CopyAction | Qt.DropAction.MoveAction

    def mimeData(self, indexes) -> QMimeData:
        """A section being dragged to another place."""
        data = QMimeData()
        index = indexes[0] if indexes else QModelIndex()
        if self.kind(index) == SECTION:
            entry = {"kind": self.kind(index), "value": self.value(index)}
            data.setData(ROW_DRAG_MIME, json.dumps(entry).encode("utf-8"))
        return data

    def _moved_row(self, data) -> tuple[str, str] | None:
        try:
            entry = json.loads(bytes(data.data(ROW_DRAG_MIME)).decode("utf-8"))
            return entry["kind"], entry["value"]
        except (ValueError, KeyError, TypeError):
            return None

    def _row_target(self, data, parent) -> tuple[str, str] | None:
        """(moved, target) when the dragged section may take the place of `parent`."""
        moved = self._moved_row(data)
        if moved is None or moved[0] != SECTION:
            return None
        target = self.value(parent)
        if self.kind(parent) == SECTION and target != moved[1]:
            return moved[1], target
        return None

    def canDropMimeData(self, data, action, row, column, parent) -> bool:
        if data.hasFormat(ROW_DRAG_MIME):
            return self._row_target(data, parent) is not None
        if not data.hasFormat(TRACK_DRAG_MIME) or self.kind(parent) != PLAYLIST:
            return False
        name = self.value(parent)
        if self.is_locked(name):
            self.drop_refused.emit(f"'{name}' is locked")
            return False
        return read_track_drag(data)[1] != name

    def dropMimeData(self, data, action, row, column, parent) -> bool:
        if data.hasFormat(ROW_DRAG_MIME):
            target = self._row_target(data, parent)
            if target is not None:
                self.section_moved.emit(*target)  # the window saves the order and rebuilds
            return False  # nothing is moved here: the list is built again in the new order
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
