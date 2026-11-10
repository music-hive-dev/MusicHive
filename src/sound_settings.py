"""The Sound tab of the settings window, and the settings it remembers."""

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QDialog

from src.number_field import NumberField

CROSSFADE_KEY = "sound/crossfade"
CROSSFADE_SECONDS_KEY = "sound/crossfadeSeconds"
CROSSFADE_ON_SKIP_KEY = "sound/crossfadeOnSkip"
FADE_ON_PAUSE_KEY = "sound/fadeOnPause"
SKIP_SILENCE_KEY = "sound/skipSilence"


@dataclass(frozen=True)
class SoundSettings:
    crossfade: bool = False
    crossfade_seconds: float = 5.0
    crossfade_on_skip: bool = False
    fade_on_pause: bool = False
    skip_silence: bool = False


def load_sound_settings(settings: QSettings) -> SoundSettings:
    defaults = SoundSettings()
    return SoundSettings(
        settings.value(CROSSFADE_KEY, defaults.crossfade, type=bool),
        settings.value(CROSSFADE_SECONDS_KEY, defaults.crossfade_seconds, type=float),
        settings.value(CROSSFADE_ON_SKIP_KEY, defaults.crossfade_on_skip, type=bool),
        settings.value(FADE_ON_PAUSE_KEY, defaults.fade_on_pause, type=bool),
        settings.value(SKIP_SILENCE_KEY, defaults.skip_silence, type=bool),
    )


class SoundSettingsTab:
    """Connects the Sound tab of settings.ui. Every change applies at once."""

    def __init__(
        self,
        dialog: QDialog,
        settings: QSettings,
        on_change: Callable[[SoundSettings], None],
    ) -> None:
        self._dialog = d = dialog
        self._settings = settings
        self._on_change = on_change

        self._crossfade_seconds = NumberField(d.soundCrossfadeSeconds)
        current = load_sound_settings(settings)
        d.soundCrossfade.setChecked(current.crossfade)
        self._crossfade_seconds.setValue(current.crossfade_seconds)
        d.soundCrossfadeOnSkip.setChecked(current.crossfade_on_skip)
        d.soundFadeOnPause.setChecked(current.fade_on_pause)
        d.soundSkipSilence.setChecked(current.skip_silence)
        self._show_crossfade_options()

        for checkbox in (
            d.soundCrossfade,
            d.soundCrossfadeOnSkip,
            d.soundFadeOnPause,
            d.soundSkipSilence,
        ):
            checkbox.toggled.connect(lambda _on: self._changed())
        self._crossfade_seconds.valueChanged.connect(lambda _value: self._changed())

    def _show_crossfade_options(self) -> None:
        d = self._dialog
        enabled = d.soundCrossfade.isChecked()
        d.soundCrossfadeLabel.setEnabled(enabled)
        d.soundCrossfadeSeconds.setEnabled(enabled)
        d.soundCrossfadeOnSkip.setEnabled(enabled)

    def _changed(self) -> None:
        d = self._dialog
        self._show_crossfade_options()
        current = SoundSettings(
            d.soundCrossfade.isChecked(),
            self._crossfade_seconds.value(),
            d.soundCrossfadeOnSkip.isChecked(),
            d.soundFadeOnPause.isChecked(),
            d.soundSkipSilence.isChecked(),
        )
        settings = self._settings
        settings.setValue(CROSSFADE_KEY, current.crossfade)
        settings.setValue(CROSSFADE_SECONDS_KEY, current.crossfade_seconds)
        settings.setValue(CROSSFADE_ON_SKIP_KEY, current.crossfade_on_skip)
        settings.setValue(FADE_ON_PAUSE_KEY, current.fade_on_pause)
        settings.setValue(SKIP_SILENCE_KEY, current.skip_silence)
        self._on_change(current)
