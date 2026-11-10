"""The filter bar next to a song table: pick a tag, then one of its values."""

from collections.abc import Callable

from PySide6.QtCore import QModelIndex, QObject, QSettings, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QAbstractButton, QComboBox, QListView

from src.library_groups import GroupListModel, TrackGroup, songs_text
from src.track_list import ASSETS_DIR, TrackDelegate, setup_track_view

# The tag each entry of a filter combo box stands for, in the order player.ui lists them.
FILTER_KINDS = ("album", "album_artist", "artist", "decade", "genre", "year")

# The image of a filter row that has no picture of its own.
PLACEHOLDERS = {
    "album": "disc.svg",
    "album_artist": "user.svg",
    "artist": "user.svg",
    "decade": "list-music.svg",
    "genre": "list-music.svg",
    "year": "list-music.svg",
}


class FilterBar(QObject):
    """A combo box, a list and a button that shows or hides them.

    `groups_for(kind, rows)` gives the values of a tag among some library rows,
    with each group's rows as positions in `rows`. `cover_for(kind, group, row)`
    gives the picture of a value whose first song is library row `row`.
    The bar does not store what is chosen: it says so through `chosen`.
    """

    chosen = Signal(str, str)  # kind, value ("" means every song)
    group_menu_requested = Signal(list, object)  # the entry's library rows, screen position

    def __init__(
        self,
        combo: QComboBox,
        view: QListView,
        button: QAbstractButton,
        groups_for: Callable[[str, list[int]], list[TrackGroup]],
        cover_for: Callable[[str, TrackGroup, int], QImage | None],
        settings: QSettings,
        visible_key: str,
    ) -> None:
        super().__init__(view)
        self._combo = combo
        self._view = view
        self._groups_for = groups_for
        self._cover_for = cover_for
        self._settings = settings
        self._visible_key = visible_key
        self._updating = False  # True while the code itself changes the widgets
        self._signature: tuple | None = None  # (owner, kind, rows) the list was built from

        self._model = GroupListModel(self._cover, self)
        setup_track_view(view, self._model)
        self._delegates = {
            kind: TrackDelegate(view, ASSETS_DIR / placeholder)
            for kind, placeholder in PLACEHOLDERS.items()
        }
        view.selectionModel().currentChanged.connect(self._on_value_selected)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(self._on_context_menu)
        combo.currentIndexChanged.connect(self._on_kind_chosen)

        button.toggled.connect(self._set_visible)
        visible = settings.value(visible_key, True, type=bool)
        button.setChecked(visible)
        self._set_visible(visible)

    def filtered(self, rows: list[int], kind: str, value: str) -> list[int]:
        """The songs among `rows` that have this tag value, in the same order.

        All of them if nothing is chosen, or if no song has that value any more.
        """
        if not value or kind not in FILTER_KINDS:
            return rows
        for group in self._groups_for(kind, rows):
            if group.key == value:
                return [rows[position] for position in sorted(group.rows)]
        return rows

    def show(self, owner: str, rows: list[int], kind: str, value: str) -> list[int]:
        """Show the filter of `owner` (what these rows belong to). Returns the rows it lets through."""
        kind = kind if kind in FILTER_KINDS else FILTER_KINDS[0]
        model = self._model
        self._updating = True
        self._combo.setCurrentIndex(FILTER_KINDS.index(kind))
        # Rebuilding the list would lose its scroll position, so only do it when needed.
        signature = (owner, kind, tuple(rows))
        if signature != self._signature:
            self._signature = signature
            everything = TrackGroup(
                "", f"All {self._combo.currentText()}s", [], subtitle=songs_text(len(rows))
            )
            model.set_groups([everything, *self._groups_for(kind, rows)])
            self._view.setItemDelegate(self._delegates[kind])
        self._view.setCurrentIndex(model.index(max(model.row_of(value), 0)))
        self._updating = False
        chosen = self.group(value)
        return [rows[position] for position in sorted(chosen.rows)] if chosen.key else rows

    def group(self, value: str) -> TrackGroup | None:
        """The list's entry for a value. If no song has that value any more, the `All` entry."""
        return self._model.group(value) or self._model.group("")

    def clear(self) -> None:
        """There is nothing to filter: show an empty list."""
        self._updating = True
        self._signature = None
        self._model.set_groups([])
        self._updating = False

    def forget(self) -> None:
        """The library changed, so tags may have too: rebuild the list next time."""
        self._signature = None

    def refresh(self, key: str) -> None:
        """Repaint a value's row (its picture has changed)."""
        self._model.refresh(key)

    # ----- internal -----

    def _cover(self, group: TrackGroup) -> QImage | None:
        if not group.key or self._signature is None:
            return None
        _owner, kind, rows = self._signature
        return self._cover_for(kind, group, rows[group.rows[0]])

    def _on_context_menu(self, pos) -> None:
        """Right-clicking an entry asks for its menu; "All ..." has none."""
        group = self._model.group_at(self._view.indexAt(pos).row())
        if group is None or not group.key or self._signature is None:
            return
        _owner, _kind, rows = self._signature
        library_rows = [rows[position] for position in sorted(group.rows)]
        self.group_menu_requested.emit(library_rows, self._view.viewport().mapToGlobal(pos))

    def _set_visible(self, visible: bool) -> None:
        self._combo.setVisible(visible)
        self._view.setVisible(visible)
        self._settings.setValue(self._visible_key, visible)

    def _on_kind_chosen(self, index: int) -> None:
        if not self._updating and 0 <= index < len(FILTER_KINDS):
            self.chosen.emit(FILTER_KINDS[index], "")

    def _on_value_selected(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self._updating or not current.isValid():
            return
        group = self._model.group_at(current.row())
        index = self._combo.currentIndex()
        if group is not None and 0 <= index < len(FILTER_KINDS):
            self.chosen.emit(FILTER_KINDS[index], group.key)
