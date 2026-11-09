"""Showing long text in a QLabel without letting it change the window's size."""

from html import escape

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QLabel, QSizePolicy


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


class ElidedLabel(QObject):
    """Makes a QLabel wrap its text and cut it off with "…" after a few lines.

    The label takes its width from the layout, never from its text, and the
    whole text is available as a tooltip when it does not fit.
    """

    def __init__(self, label: QLabel, max_lines: int = 3) -> None:
        super().__init__(label)
        self._label = label
        self._max_lines = max_lines
        self._text = ""  # on one line, ready to be wrapped
        self._tooltip = ""

        label.setWordWrap(False)  # the line breaks are put in by wrap_and_elide
        label.setTextFormat(Qt.TextFormat.PlainText)
        policy = label.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        label.setSizePolicy(policy)
        label.installEventFilter(self)

    def set_text(self, text: str) -> None:
        """Show this text; empty text hides the label so it takes no space."""
        self._text = " ".join(text.split())
        self._tooltip = "<p>" + escape(text).replace("\n", "<br>") + "</p>"
        self._update()
        self._label.setVisible(bool(self._text))

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.FontChange):
            self._update()
        return False

    def _update(self) -> None:
        label = self._label
        shown, cut_off = wrap_and_elide(
            self._text, QFontMetrics(label.font()), max(1, label.width()), self._max_lines
        )
        label.setText(shown)
        label.setToolTip(self._tooltip if cut_off else "")
