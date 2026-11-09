"""The equaliser window, its presets and the settings it remembers."""

import bisect
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QDialogButtonBox, QInputDialog, QMessageBox, QWidget

from src.lastfm_dialog import load_dialog

BAND_FREQUENCIES = {
    10: (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000),
    15: (25, 40, 63, 100, 160, 250, 400, 630, 1000, 1600, 2500, 4000, 6300, 10000, 16000),
}
# Each band covers this many octaves around its frequency.
BAND_OCTAVES = {10: 1.0, 15: 2 / 3}
BAND_COUNTS = (10, 15)  # in the order of the choices in the window
MAX_BANDS = max(BAND_COUNTS)
MAX_GAIN_DB = 12
BALANCE_STEPS = 20  # the balance slider goes this far to each side

# Gains in dB of the ten bands. A 15-band equaliser gets the same curve.
PRESETS = {
    "Flat": (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "Acoustic": (3, 2, 1, 1, 2, 1, 2, 2, 2, 1),
    "Bass Boost": (6, 5, 4, 2, 0, 0, 0, 0, 0, 0),
    "Classical": (3, 2, 1, 0, 0, 0, -1, -2, -2, -3),
    "Electronic": (5, 4, 1, 0, -2, 2, 1, 2, 4, 5),
    "Jazz": (3, 2, 1, 2, -1, -1, 0, 1, 2, 3),
    "Loudness": (5, 3, 0, 0, -1, 0, -1, -2, 3, 4),
    "Pop": (-1, 1, 3, 4, 3, 0, -1, -1, 0, 1),
    "Rock": (4, 3, -2, -3, -1, 1, 3, 4, 4, 4),
    "Treble Boost": (0, 0, 0, 0, 0, 1, 3, 5, 6, 7),
    "Vocal": (-2, -2, -1, 1, 3, 4, 3, 1, 0, -1),
}
DEFAULT_PRESET = "Flat"

CURRENT_KEY = "equaliser/current"
PRESETS_KEY = "equaliser/presets"


@dataclass
class EqSettings:
    band_count: int = BAND_COUNTS[0]
    gains: list[float] | None = None  # dB of each band
    preamp: float = 0.0  # dB
    balance: int = 0  # from -BALANCE_STEPS (left) to BALANCE_STEPS (right)
    preset: str = DEFAULT_PRESET  # the preset these values came from; "" if changed since

    def __post_init__(self) -> None:
        if self.gains is None:
            self.gains = [0] * self.band_count

    @property
    def balance_position(self) -> float:
        """The balance from -1.0 (left) to 1.0 (right)."""
        return self.balance / BALANCE_STEPS


def resample_gains(gains: list[float], old_count: int, new_count: int) -> list[float]:
    """The same curve for a different number of bands."""
    if old_count == new_count:
        return list(gains)
    positions = [math.log2(frequency) for frequency in BAND_FREQUENCIES[old_count]]
    result = []
    for frequency in BAND_FREQUENCIES[new_count]:
        position = min(max(math.log2(frequency), positions[0]), positions[-1])
        upper = min(bisect.bisect_left(positions, position), len(positions) - 1)
        lower = max(upper - 1, 0)
        span = positions[upper] - positions[lower]
        share = (position - positions[lower]) / span if span else 0.0
        # Adding 0.0 turns a -0.0 into a plain 0.0.
        result.append(round(gains[lower] + (gains[upper] - gains[lower]) * share, 1) + 0.0)
    return result


def _built_in_preset(name: str, band_count: int, balance: int) -> EqSettings:
    gains = resample_gains(list(PRESETS[name]), BAND_COUNTS[0], band_count)
    # Turning the level down by the biggest boost keeps the loudest band from distorting.
    return EqSettings(band_count, gains, -max(0, max(gains)), balance, name)


def _clamp_db(value: object) -> float:
    return round(min(max(float(value), -MAX_GAIN_DB), MAX_GAIN_DB), 1)


def _parse(data: object) -> EqSettings | None:
    """Saved values as settings, or None if they make no sense."""
    if not isinstance(data, dict):
        return None
    try:
        band_count = int(data["band_count"])
        gains = [_clamp_db(gain) for gain in data["gains"]]
        preamp = _clamp_db(data["preamp"])
        balance = min(max(int(data["balance"]), -BALANCE_STEPS), BALANCE_STEPS)
        preset = str(data.get("preset", ""))
    except (KeyError, TypeError, ValueError):
        return None
    if band_count not in BAND_COUNTS or len(gains) != band_count:
        return None
    return EqSettings(band_count, gains, preamp, balance, preset)


def _read_json(settings: QSettings, key: str) -> object:
    try:
        return json.loads(settings.value(key, "", type=str))
    except ValueError:
        return None


def load_equaliser(settings: QSettings) -> EqSettings:
    """The equaliser as it was last set."""
    return _parse(_read_json(settings, CURRENT_KEY)) or EqSettings()


def _load_user_presets(settings: QSettings) -> dict[str, EqSettings]:
    data = _read_json(settings, PRESETS_KEY)
    if not isinstance(data, dict):
        return {}
    parsed = {name: _parse(values) for name, values in data.items()}
    return {name: preset for name, preset in parsed.items() if preset is not None}


def _frequency_text(frequency: int) -> str:
    return f"{frequency / 1000:g}k" if frequency >= 1000 else str(frequency)


def _balance_text(balance: int) -> str:
    if not balance:
        return "Centre"
    side = "R" if balance > 0 else "L"
    return f"{side} {abs(balance) * 100 // BALANCE_STEPS}%"


def _set_quietly(widget, value: float) -> None:
    """Show a value on a slider or number field without that counting as a change."""
    widget.blockSignals(True)
    widget.setValue(value)
    widget.blockSignals(False)


class EqualiserWindow:
    """Connects the widgets of equaliser.ui. Every change is heard at once.

    The window always remembers the equaliser as it was last set; Save keeps
    the current values as a named preset.
    """

    def __init__(
        self,
        ui_path: Path,
        parent: QWidget,
        settings: QSettings,
        on_change: Callable[[EqSettings], None],
    ) -> None:
        self.dialog = d = load_dialog(ui_path, parent)
        self._settings = settings
        self._on_change = on_change
        self._current = load_equaliser(settings)
        self._user_presets = _load_user_presets(settings)
        self._sliders = [getattr(d, f"eqBand{band}") for band in range(MAX_BANDS)]
        self._values = [getattr(d, f"eqBandValue{band}") for band in range(MAX_BANDS)]
        self._frequencies = [getattr(d, f"eqBandFreq{band}") for band in range(MAX_BANDS)]

        for band, (slider, value) in enumerate(zip(self._sliders, self._values)):
            slider.valueChanged.connect(lambda gain, b=band: self._on_band_moved(b, gain))
            value.valueChanged.connect(lambda gain, b=band: self._on_band_typed(b, gain))
        d.eqPreamp.valueChanged.connect(self._on_preamp_moved)
        d.eqPreampValue.valueChanged.connect(self._on_preamp_typed)
        d.eqBalance.valueChanged.connect(self._on_balance_moved)
        d.eqBandMode.activated.connect(self._on_band_mode_chosen)
        d.eqPreset.activated.connect(self._on_preset_chosen)
        d.eqSavePreset.clicked.connect(lambda _checked=False: self._save_preset())
        d.eqDeletePreset.clicked.connect(lambda _checked=False: self._delete_preset())
        d.eqButtonBox.rejected.connect(d.reject)
        # Enter confirms a typed number; it must not press Close as well.
        d.eqButtonBox.button(QDialogButtonBox.StandardButton.Close).setAutoDefault(False)

        self._fill_presets()
        self._show_current()

    def show(self) -> None:
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    # ----- showing the values -----

    def _fill_presets(self) -> None:
        combo = self.dialog.eqPreset
        combo.clear()
        combo.addItems(list(PRESETS))
        if self._user_presets:
            combo.insertSeparator(combo.count())
            combo.addItems(sorted(self._user_presets, key=str.casefold))

    def _show_preset_name(self) -> None:
        d = self.dialog
        name = self._current.preset
        d.eqPreset.setCurrentIndex(d.eqPreset.findText(name) if name else -1)
        d.eqDeletePreset.setEnabled(self._current.preset in self._user_presets)

    def _show_current(self) -> None:
        """Put the current values on the widgets, without that counting as a change."""
        d = self.dialog
        current = self._current
        frequencies = BAND_FREQUENCIES[current.band_count]
        d.eqBandMode.setCurrentIndex(BAND_COUNTS.index(current.band_count))
        for band, (slider, value, frequency) in enumerate(
            zip(self._sliders, self._values, self._frequencies)
        ):
            shown = band < current.band_count
            for widget in (slider, value, frequency):
                widget.setVisible(shown)
            if shown:
                _set_quietly(slider, round(current.gains[band]))
                _set_quietly(value, current.gains[band])
                frequency.setText(_frequency_text(frequencies[band]))
        _set_quietly(d.eqPreamp, round(current.preamp))
        _set_quietly(d.eqPreampValue, current.preamp)
        _set_quietly(d.eqBalance, current.balance)
        d.eqBalanceValue.setText(_balance_text(current.balance))
        self._show_preset_name()

    # ----- changes -----

    def _publish(self) -> None:
        self._on_change(self._current)
        self._settings.setValue(CURRENT_KEY, json.dumps(asdict(self._current)))

    def _changed_by_hand(self) -> None:
        self._current.preset = ""
        self._show_preset_name()
        self._publish()

    def _on_band_moved(self, band: int, gain: int) -> None:
        self._current.gains[band] = float(gain)
        _set_quietly(self._values[band], gain)
        self._changed_by_hand()

    def _on_band_typed(self, band: int, gain: float) -> None:
        self._current.gains[band] = gain
        _set_quietly(self._sliders[band], round(gain))
        self._changed_by_hand()

    def _on_preamp_moved(self, gain: int) -> None:
        self._current.preamp = float(gain)
        _set_quietly(self.dialog.eqPreampValue, gain)
        self._changed_by_hand()

    def _on_preamp_typed(self, gain: float) -> None:
        self._current.preamp = gain
        _set_quietly(self.dialog.eqPreamp, round(gain))
        self._changed_by_hand()

    def _on_balance_moved(self, balance: int) -> None:
        self._current.balance = balance
        self.dialog.eqBalanceValue.setText(_balance_text(balance))
        self._changed_by_hand()

    def _on_band_mode_chosen(self, index: int) -> None:
        current = self._current
        band_count = BAND_COUNTS[index]
        if band_count == current.band_count:
            return
        current.gains = resample_gains(current.gains, current.band_count, band_count)
        current.band_count = band_count
        if current.preset not in PRESETS:
            current.preset = ""  # a saved preset is for the number of bands it was made with
        self._show_current()
        self._publish()

    def _on_preset_chosen(self, index: int) -> None:
        name = self.dialog.eqPreset.itemText(index)
        saved = self._user_presets.get(name)
        if saved is not None:
            self._current = replace(saved, gains=list(saved.gains), preset=name)
        else:
            current = self._current
            self._current = _built_in_preset(name, current.band_count, current.balance)
        self._show_current()
        self._publish()

    # ----- saved presets -----

    def _write_user_presets(self) -> None:
        data = {name: asdict(preset) for name, preset in self._user_presets.items()}
        self._settings.setValue(PRESETS_KEY, json.dumps(data))

    def _save_preset(self) -> None:
        d = self.dialog
        suggestion = self._current.preset if self._current.preset in self._user_presets else ""
        name, accepted = QInputDialog.getText(d, "Save preset", "Preset name:", text=suggestion)
        name = name.strip()
        if not accepted or not name:
            return
        if name in PRESETS:
            QMessageBox.warning(d, "Save preset", "A built-in preset already has that name.")
            return
        if name in self._user_presets and name != self._current.preset:
            answer = QMessageBox.question(d, "Save preset", f"Replace the preset '{name}'?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._current.preset = name
        self._user_presets[name] = replace(self._current, gains=list(self._current.gains))
        self._write_user_presets()
        self._fill_presets()
        self._show_preset_name()
        self._publish()

    def _delete_preset(self) -> None:
        d = self.dialog
        name = self._current.preset
        if name not in self._user_presets:
            return
        answer = QMessageBox.question(d, "Delete preset", f"Delete the preset '{name}'?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        del self._user_presets[name]
        self._write_user_presets()
        self._current.preset = ""
        self._fill_presets()
        self._show_preset_name()
        self._publish()
