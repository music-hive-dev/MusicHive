"""Drags that show a short label instead of a picture of the dragged rows.

Qt draws the rows being dragged as they look in the list. Here a drag shows
only the first item's name ("Title - Artist" for a song), and how many more
items come with it. The label is a QLabel named dragLabel, styled in player.ui.
"""

from PySide6.QtCore import QModelIndex, QPoint, Qt
from PySide6.QtGui import QDrag, QPixmap, QRegion
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import QAbstractItemView, QLabel, QListView, QTableView, QTreeView, QWidget

from src.track_list import TrackListModel

NAME_WIDTH = 360  # longer names are cut off with "…"


def drag_text(indexes: list[QModelIndex], font_metrics=None) -> str:
    """'Title - Artist', or 'Title - Artist and 3 more songs…' for several songs."""
    rows: dict[int, QModelIndex] = {}
    for index in indexes:
        rows.setdefault(index.row(), index)
    if not rows:
        return ""
    first = rows[min(rows)]
    title = first.data(TrackListModel.TitleRole)
    is_song = title is not None
    if is_song:
        artist = first.data(TrackListModel.ArtistRole) or ""
        name = f"{title} - {artist}" if artist else str(title)
    else:
        name = str(first.siblingAtColumn(0).data(Qt.ItemDataRole.DisplayRole) or "")
    if font_metrics is not None:
        name = font_metrics.elidedText(name, Qt.TextElideMode.ElideRight, NAME_WIDTH)
    more = len(rows) - 1
    if more <= 0:
        return name
    noun = ("song" if more == 1 else "songs") if is_song else ("item" if more == 1 else "items")
    return f"{name} and {more} more {noun}…"


def drag_pixmap(widget: QWidget, indexes: list[QModelIndex]) -> QPixmap:
    """The label a drag of `indexes` out of `widget` shows under the cursor."""
    # A child of the window, so the window's stylesheet styles it.
    label = QLabel(widget.window())
    label.setObjectName("dragLabel")
    label.ensurePolished()
    label.setText(drag_text(indexes, label.fontMetrics()))
    label.adjustSize()
    ratio = widget.devicePixelRatioF()
    pixmap = QPixmap(label.size() * ratio)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    # Without the window background, so the rounded corners stay see-through.
    label.render(pixmap, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    label.deleteLater()
    return pixmap


def start_label_drag(view: QAbstractItemView, supported_actions: Qt.DropAction) -> None:
    """QAbstractItemView.startDrag, with the label as the drag's picture."""
    model = view.model()
    indexes = [
        index
        for index in view.selectedIndexes()
        if model.flags(index) & Qt.ItemFlag.ItemIsDragEnabled
    ]
    if not indexes:
        return
    data = model.mimeData(indexes)
    if data is None:
        return
    drag = QDrag(view)
    drag.setMimeData(data)
    pixmap = drag_pixmap(view, indexes)
    drag.setPixmap(pixmap)
    drag.setHotSpot(QPoint(-12, -12))  # the label sits just below and right of the cursor
    default_action = view.defaultDropAction()
    if default_action == Qt.DropAction.IgnoreAction or not supported_actions & default_action:
        default_action = (
            Qt.DropAction.CopyAction
            if supported_actions & Qt.DropAction.CopyAction
            else Qt.DropAction.IgnoreAction
        )
    drag.exec(supported_actions, default_action)


class _LabelDragMixin:
    def startDrag(self, supported_actions) -> None:
        # Moving rows within one list relies on Qt's own bookkeeping, so it keeps Qt's drag.
        if self.dragDropMode() == QAbstractItemView.DragDropMode.InternalMove:
            super().startDrag(supported_actions)
        else:
            start_label_drag(self, supported_actions)


class LabelDragListView(_LabelDragMixin, QListView):
    pass


class LabelDragTableView(_LabelDragMixin, QTableView):
    pass


class LabelDragTreeView(_LabelDragMixin, QTreeView):
    pass


_VIEWS = {
    "QListView": LabelDragListView,
    "QTableView": LabelDragTableView,
    "QTreeView": LabelDragTreeView,
}


class LabelDragUiLoader(QUiLoader):
    """A QUiLoader whose list, table and tree views drag a label."""

    def createWidget(self, class_name, parent=None, name=""):
        view_class = _VIEWS.get(class_name)
        if view_class is None:
            return super().createWidget(class_name, parent, name)
        widget = view_class(parent)
        widget.setObjectName(name)
        return widget
