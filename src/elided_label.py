"""Showing long text in a QLabel without letting it change the window's size."""

from html import escape

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QLabel, QLayout, QSizePolicy, QWidget


def wrap_and_elide(
    text: str, metrics: QFontMetrics, width: int, max_lines: int
) -> tuple[str, bool]:
    """Wrap `text` to `width` pixels, ending in "…" if it needs more than `max_lines`.

    Returns the lines joined by newlines, and whether anything was cut off.
    """
    right = Qt.TextElideMode.ElideRight
    words = text.split(" ")
    lines: list[str] = []
    next_word = 0
    while len(lines) < max_lines - 1 and next_word < len(words):
        line = words[next_word]
        next_word += 1
        while next_word < len(words):
            longer = f"{line} {words[next_word]}"
            if metrics.horizontalAdvance(longer) > width:
                break
            line = longer
            next_word += 1
        lines.append(metrics.elidedText(line, right, width))  # one very long word

    rest = " ".join(words[next_word:])
    last = metrics.elidedText(rest, right, width)
    if rest:
        lines.append(last)
    return "\n".join(lines), last != rest


def _layout_holding(layout: QLayout | None, widget: QWidget) -> QLayout | None:
    """The layout, at any depth inside `layout`, that holds `widget` itself."""
    if layout is None:
        return None
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.widget() is widget:
            return layout
        found = _layout_holding(item.layout(), widget)
        if found is not None:
            return found
    return None


class ElidedLabel(QObject):
    """Makes a QLabel wrap its text and cut it off with "…" after a few lines.

    The label takes its width from the layout, never from its text, and the
    whole text is available as a tooltip when it does not fit.

    With `fit_beside` (the picture of a playlist, artist or podcast header), it
    also uses no more lines than leave its column of text as tall as the
    picture, counting the title and the other lines above and below it.
    """

    def __init__(self, label: QLabel, max_lines: int = 3, fit_beside: QWidget | None = None) -> None:
        super().__init__(label)
        self._label = label
        self._max_lines = max_lines
        self._fit_beside = fit_beside
        self._text = ""  # on one line, ready to be wrapped
        self._tooltip = ""

        label.setWordWrap(False)  # the line breaks are put in by wrap_and_elide
        label.setTextFormat(Qt.TextFormat.PlainText)
        policy = label.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        label.setSizePolicy(policy)
        label.installEventFilter(self)
        if fit_beside is not None:
            fit_beside.installEventFilter(self)  # its size decides how many lines fit

    def set_text(self, text: str) -> None:
        """Show this text; empty text hides the label so it takes no space."""
        self._text = " ".join(text.split())
        self._tooltip = "<p>" + escape(text).replace("\n", "<br>") + "</p>"
        self._update()
        self._label.setVisible(bool(self._text))

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.FontChange, QEvent.Type.Show):
            self._update()
        return False

    def refit(self) -> None:
        """Lines beside the label changed (shown, hidden or new text): fit again."""
        self._update()

    def _lines_that_fit(self, metrics: QFontMetrics) -> int:
        """How many lines leave the text column no taller than the picture."""
        picture = self._fit_beside
        label = self._label
        if picture is None:
            return self._max_lines
        height = picture.height() if picture.height() > 1 else picture.maximumHeight()
        parent = label.parentWidget()
        column = _layout_holding(parent.layout() if parent is not None else None, label)
        if column is None:
            return self._max_lines
        spacing = column.spacing() if column.spacing() >= 0 else 6
        margins = column.contentsMargins()
        others = 0
        shown = 1  # the label itself
        for index in range(column.count()):
            item = column.itemAt(index)
            widget = item.widget()
            if widget is label:
                continue
            if widget is not None and not widget.isVisibleTo(parent):
                continue
            if item.spacerItem() is not None:
                continue  # a stretching gap: no room, and Qt puts no spacing beside it
            others += item.sizeHint().height()
            shown += 1
        room = height - others - spacing * (shown - 1) - margins.top() - margins.bottom()
        return max(1, min(self._max_lines, room // max(1, metrics.lineSpacing())))

    def _update(self) -> None:
        label = self._label
        metrics = QFontMetrics(label.font())
        shown, cut_off = wrap_and_elide(
            self._text, metrics, max(1, label.width()), self._lines_that_fit(metrics)
        )
        label.setText(shown)
        label.setToolTip(self._tooltip if cut_off else "")
