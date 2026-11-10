"""A text field for a number, used instead of QSpinBox / QDoubleSpinBox.

The app's stylesheet cannot draw spin boxes properly (their inner field is cut
off), so number boxes are plain QLineEdits. Their limits are set in the .ui
file as dynamic properties of the field, so they stay editable in Designer:

    minimum, maximum (number or double), decimals (number, default 0),
    singleStep (number or double, default 1), suffix (string, e.g. " px")

A typed value is used when Enter is pressed or the field loses focus; anything
that is not a number puts the previous value back. The Up and Down keys, and
the mouse wheel while the field has focus, change it by one step.
"""

import math

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QLineEdit


class NumberField(QObject):
    """Gives a QLineEdit the value(), setValue() and valueChanged of a spin box."""

    valueChanged = Signal(object)  # the new value: int, or float with decimals

    def __init__(self, field: QLineEdit) -> None:
        super().__init__(field)
        self.field = field
        self._decimals = int(self._property("decimals", 0))
        self._minimum = float(self._property("minimum", 0))
        self._maximum = float(self._property("maximum", 99))
        self._step = float(self._property("singleStep", 1))
        self._suffix = str(self._property("suffix", ""))
        self._value = self._limited(float(self._property("value", self._minimum)))
        self._show()
        field.editingFinished.connect(self._on_edited)
        field.installEventFilter(self)

    def _property(self, name: str, default):
        value = self.field.property(name)
        return default if value is None else value

    # ----- like a spin box -----

    def value(self) -> int | float:
        return int(round(self._value)) if self._decimals == 0 else self._value

    def setValue(self, value: float) -> None:
        value = self._limited(float(value))
        changed = value != self._value
        self._value = value
        self._show()
        if changed:
            self.valueChanged.emit(self.value())

    def setEnabled(self, enabled: bool) -> None:
        self.field.setEnabled(enabled)

    # ----- internal -----

    def _limited(self, value: float) -> float:
        value = max(self._minimum, min(self._maximum, value))
        return round(value, self._decimals)

    def _show(self) -> None:
        self.field.setText(f"{self._value:.{self._decimals}f}{self._suffix}")

    def _typed(self) -> float | None:
        text = self.field.text().strip()
        suffix = self._suffix.strip()
        if suffix and text.lower().endswith(suffix.lower()):
            text = text[: -len(suffix)]
        try:
            value = float(text.strip().replace(",", "."))
        except ValueError:
            return None
        return value if math.isfinite(value) else None

    def _on_edited(self) -> None:
        typed = self._typed()
        if typed is None:
            self._show()  # not a number: put back what was there
        else:
            self.setValue(typed)  # also tidies up what was typed

    def eventFilter(self, watched, event) -> bool:
        if watched is self.field and self.field.isEnabled():
            steps = 0
            if event.type() == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Up:
                    steps = 1
                elif event.key() == Qt.Key.Key_Down:
                    steps = -1
            elif event.type() == QEvent.Type.Wheel and self.field.hasFocus():
                delta = event.angleDelta().y()
                steps = 1 if delta > 0 else -1 if delta < 0 else 0
            if steps:
                typed = self._typed()
                start = self._value if typed is None else typed
                self.setValue(start + steps * self._step)
                return True
        return super().eventFilter(watched, event)
