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
ALBUM_VOLUME_KEY = "sound/albumVolume"
NO_CROSSFADE_IN_ALBUM_KEY = "sound/noCrossfadeInAlbum"


@dataclass(frozen=True)
class SoundSettings:
    crossfade: bool = False
    crossfade_seconds: float = 5.0
    crossfade_on_skip: bool = False
    fade_on_pause: bool = False
    skip_silence: bool = False
    album_volume: bool = False  # one volume for an album that plays in order
    no_crossfade_in_album: bool = False  # tracks of an album in order follow without one


def load_sound_settings(settings: QSettings) -> SoundSettings:
    defaults = SoundSettings()
    return SoundSettings(
        crossfade=settings.value(CROSSFADE_KEY, defaults.crossfade, type=bool),
        crossfade_seconds=settings.value(CROSSFADE_SECONDS_KEY, defaults.crossfade_seconds, type=float),
        crossfade_on_skip=settings.value(CROSSFADE_ON_SKIP_KEY, defaults.crossfade_on_skip, type=bool),
        fade_on_pause=settings.value(FADE_ON_PAUSE_KEY, defaults.fade_on_pause, type=bool),
        skip_silence=settings.value(SKIP_SILENCE_KEY, defaults.skip_silence, type=bool),
        album_volume=settings.value(ALBUM_VOLUME_KEY, defaults.album_volume, type=bool),
        no_crossfade_in_album=settings.value(
            NO_CROSSFADE_IN_ALBUM_KEY, defaults.no_crossfade_in_album, type=bool
        ),
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
        d.soundAlbumVolume.setChecked(current.album_volume)
        d.soundNoCrossfadeInAlbum.setChecked(current.no_crossfade_in_album)
        self._show_crossfade_options()

        for checkbox in (
            d.soundCrossfade,
            d.soundCrossfadeOnSkip,
            d.soundFadeOnPause,
            d.soundSkipSilence,
            d.soundAlbumVolume,
            d.soundNoCrossfadeInAlbum,
        ):
            checkbox.toggled.connect(lambda _on: self._changed())
        self._crossfade_seconds.valueChanged.connect(lambda _value: self._changed())

    def _show_crossfade_options(self) -> None:
        d = self._dialog
        enabled = d.soundCrossfade.isChecked()
        d.soundCrossfadeLabel.setEnabled(enabled)
        d.soundCrossfadeSeconds.setEnabled(enabled)
        d.soundCrossfadeOnSkip.setEnabled(enabled)
        d.soundNoCrossfadeInAlbum.setEnabled(enabled)

    def _changed(self) -> None:
        d = self._dialog
        self._show_crossfade_options()
        current = SoundSettings(
            crossfade=d.soundCrossfade.isChecked(),
            crossfade_seconds=self._crossfade_seconds.value(),
            crossfade_on_skip=d.soundCrossfadeOnSkip.isChecked(),
            fade_on_pause=d.soundFadeOnPause.isChecked(),
            skip_silence=d.soundSkipSilence.isChecked(),
            album_volume=d.soundAlbumVolume.isChecked(),
            no_crossfade_in_album=d.soundNoCrossfadeInAlbum.isChecked(),
        )
        settings = self._settings
        settings.setValue(CROSSFADE_KEY, current.crossfade)
        settings.setValue(CROSSFADE_SECONDS_KEY, current.crossfade_seconds)
        settings.setValue(CROSSFADE_ON_SKIP_KEY, current.crossfade_on_skip)
        settings.setValue(FADE_ON_PAUSE_KEY, current.fade_on_pause)
        settings.setValue(SKIP_SILENCE_KEY, current.skip_silence)
        settings.setValue(ALBUM_VOLUME_KEY, current.album_volume)
        settings.setValue(NO_CROSSFADE_IN_ALBUM_KEY, current.no_crossfade_in_album)
        self._on_change(current)
