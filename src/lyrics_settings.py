"""The lyrics part of the Now Playing tab of the settings window: the text shadow
(also used by the mini player) and how "No lyrics for this song" fades away."""

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QDialog

from src.number_field import NumberField

SHADOW_KEY = "lyrics/shadow"
SHADOW_BLUR_KEY = "lyrics/shadowBlur"
SHADOW_DISTANCE_KEY = "lyrics/shadowDistance"
SHADOW_STRENGTH_KEY = "lyrics/shadowStrength"
SHOW_NO_LYRICS_KEY = "lyrics/showNoLyrics"
FADE_NO_LYRICS_KEY = "lyrics/fadeNoLyrics"
NO_LYRICS_HOLD_KEY = "lyrics/noLyricsHold"
NO_LYRICS_FADE_KEY = "lyrics/noLyricsFade"
SCALE_KEY = "lyrics/scaleWithWindow"
COVER_COLOUR_KEY = "lyrics/coverColour"


@dataclass(frozen=True)
class LyricsSettings:
    shadow: bool = False  # behind the lyrics and the mini player's title and artist
    shadow_blur: int = 8  # pixels
    shadow_distance: int = 2  # pixels down (and half of it to the right)
    shadow_strength: int = 86  # percent
    show_no_lyrics: bool = True  # "No lyrics for this song" shows at all
    fade_no_lyrics: bool = True  # "No lyrics for this song" fades away
    no_lyrics_hold: float = 3.0  # seconds it shows before fading
    no_lyrics_fade: float = 2.0  # seconds the fade takes
    scale_with_window: bool = False  # bigger lyrics in a bigger window (a TV, say)
    cover_colour: bool = False  # the lyrics in a striking colour of the playing cover


def load_lyrics_settings(settings: QSettings) -> LyricsSettings:
    d = LyricsSettings()

    def number(key: str, default, low, high, kind=int):
        return max(low, min(high, settings.value(key, default, type=kind)))

    return LyricsSettings(
        shadow=settings.value(SHADOW_KEY, d.shadow, type=bool),
        shadow_blur=number(SHADOW_BLUR_KEY, d.shadow_blur, 0, 100),
        shadow_distance=number(SHADOW_DISTANCE_KEY, d.shadow_distance, 0, 50),
        shadow_strength=number(SHADOW_STRENGTH_KEY, d.shadow_strength, 1, 100),
        show_no_lyrics=settings.value(SHOW_NO_LYRICS_KEY, d.show_no_lyrics, type=bool),
        fade_no_lyrics=settings.value(FADE_NO_LYRICS_KEY, d.fade_no_lyrics, type=bool),
        no_lyrics_hold=number(NO_LYRICS_HOLD_KEY, d.no_lyrics_hold, 0.0, 600.0, float),
        no_lyrics_fade=number(NO_LYRICS_FADE_KEY, d.no_lyrics_fade, 0.1, 600.0, float),
        scale_with_window=settings.value(SCALE_KEY, d.scale_with_window, type=bool),
        cover_colour=settings.value(COVER_COLOUR_KEY, d.cover_colour, type=bool),
    )


def save_lyrics_settings(settings: QSettings, style: LyricsSettings) -> None:
    for key, value in (
        (SHADOW_KEY, style.shadow),
        (SHADOW_BLUR_KEY, style.shadow_blur),
        (SHADOW_DISTANCE_KEY, style.shadow_distance),
        (SHADOW_STRENGTH_KEY, style.shadow_strength),
        (SHOW_NO_LYRICS_KEY, style.show_no_lyrics),
        (FADE_NO_LYRICS_KEY, style.fade_no_lyrics),
        (NO_LYRICS_HOLD_KEY, style.no_lyrics_hold),
        (NO_LYRICS_FADE_KEY, style.no_lyrics_fade),
        (SCALE_KEY, style.scale_with_window),
        (COVER_COLOUR_KEY, style.cover_colour),
    ):
        settings.setValue(key, value)


class LyricsSettingsTab:
    """Connects the lyrics options of settings.ui. Every change applies at once."""

    def __init__(
        self,
        dialog: QDialog,
        settings: QSettings,
        on_change: Callable[[LyricsSettings], None],
    ) -> None:
        self._dialog = d = dialog
        self._settings = settings
        self._on_change = on_change
        self._blur = NumberField(d.lyricsShadowBlur)
        self._distance = NumberField(d.lyricsShadowDistance)
        self._strength = NumberField(d.lyricsShadowStrength)
        self._hold = NumberField(d.lyricsNoLyricsHold)
        self._fade = NumberField(d.lyricsNoLyricsFade)
        self._showing = False
        self._show(load_lyrics_settings(settings))

        for field in (self._blur, self._distance, self._strength, self._hold, self._fade):
            field.valueChanged.connect(lambda _value: self._changed())
        for checkbox in (
            d.lyricsShadow, d.lyricsShowNoLyrics, d.lyricsFadeNoLyrics, d.lyricsScale,
            d.lyricsCoverColour,
        ):
            checkbox.toggled.connect(lambda _on: self._changed())
        d.lyricsReset.clicked.connect(lambda _checked=False: self._reset())

    def _show(self, style: LyricsSettings) -> None:
        d = self._dialog
        self._showing = True
        d.lyricsShadow.setChecked(style.shadow)
        d.lyricsScale.setChecked(style.scale_with_window)
        d.lyricsCoverColour.setChecked(style.cover_colour)
        self._blur.setValue(style.shadow_blur)
        self._distance.setValue(style.shadow_distance)
        self._strength.setValue(style.shadow_strength)
        d.lyricsShowNoLyrics.setChecked(style.show_no_lyrics)
        d.lyricsFadeNoLyrics.setChecked(style.fade_no_lyrics)
        self._hold.setValue(style.no_lyrics_hold)
        self._fade.setValue(style.no_lyrics_fade)
        self._showing = False
        self._show_enabled()

    def _show_enabled(self) -> None:
        """Options that only matter while their checkbox is ticked."""
        d = self._dialog
        shadow = d.lyricsShadow.isChecked()
        for widget in (
            d.lyricsShadowBlurLabel, d.lyricsShadowBlur,
            d.lyricsShadowDistanceLabel, d.lyricsShadowDistance,
            d.lyricsShadowStrengthLabel, d.lyricsShadowStrength,
        ):
            widget.setEnabled(shadow)
        shown = d.lyricsShowNoLyrics.isChecked()
        d.lyricsFadeNoLyrics.setEnabled(shown)
        fade = shown and d.lyricsFadeNoLyrics.isChecked()
        for widget in (
            d.lyricsNoLyricsHoldLabel, d.lyricsNoLyricsHold,
            d.lyricsNoLyricsFadeLabel, d.lyricsNoLyricsFade,
        ):
            widget.setEnabled(fade)

    def _reset(self) -> None:
        self._show(LyricsSettings())
        self._changed()

    def _changed(self) -> None:
        if self._showing:
            return
        d = self._dialog
        self._show_enabled()
        style = LyricsSettings(
            shadow=d.lyricsShadow.isChecked(),
            shadow_blur=self._blur.value(),
            shadow_distance=self._distance.value(),
            shadow_strength=self._strength.value(),
            show_no_lyrics=d.lyricsShowNoLyrics.isChecked(),
            fade_no_lyrics=d.lyricsFadeNoLyrics.isChecked(),
            no_lyrics_hold=self._hold.value(),
            no_lyrics_fade=self._fade.value(),
            scale_with_window=d.lyricsScale.isChecked(),
            cover_colour=d.lyricsCoverColour.isChecked(),
        )
        save_lyrics_settings(self._settings, style)
        self._on_change(style)
