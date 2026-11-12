"""The visualiser part of the Now Playing tab of the settings window, and its settings."""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace

from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox

from src import theme
from src.flags import all_flags, flag_stripes, prepare_user_flags_dir, reload_flags
from src.number_field import NumberField

BARS_KEY = "visualiser/bars"
GAP_KEY = "visualiser/gap"
OPACITY_KEY = "visualiser/opacity"
FALL_SPEED_KEY = "visualiser/fallSpeed"
COLOURS_KEY = "visualiser/colours"
MINI_GAP_KEY = "visualiser/miniGap"
BACKGROUND_KEY = "visualiser/background"
BACKGROUND_BLUR_KEY = "visualiser/backgroundBlur"
BACKGROUND_DARKEN_KEY = "visualiser/backgroundDarken"
BACKGROUND_FIT_KEY = "visualiser/backgroundFit"
MINI_BACKGROUND_KEY = "visualiser/miniBackground"
MINI_COLOURS_KEY = "visualiser/miniColours"
MINI_BARS_KEY = "visualiser/miniBars"
GLOW_KEY = "visualiser/glow"
SMOOTH_KEY = "visualiser/smooth"
PRESETS_KEY = "visualiser/presets"  # {name: settings}, as JSON

# How the cover fills the space, for each entry of visualiserBackgroundFit.
FILL, FIT, STRETCH = range(3)

# How much of its height a bar keeps each frame (at 30 frames a second), for each
# entry of visualiserFallSpeed in settings.ui: slow, normal, fast.
FALL_PER_FRAME = (0.93, 0.86, 0.75)
FRAMES_PER_SECOND = 30
SMOOTH_FRAMES_PER_SECOND = 60  # "60 frames a second"

HIGHLIGHT = "highlight"  # every bar in the highlight colour
RAINBOW = "rainbow"  # a smooth rainbow from red to violet
COVER = "cover"  # the playing cover's colours: the bars, and the background behind them
RAINBOW_HUES = 300  # the rainbow runs from red (0) to violet (300) across the bars

# The colours that are not flags, in the order settings.ui lists them; the flags
# (flags.py, read from JSON files) follow them in the lists.
BASIC_COLOURS = (HIGHLIGHT, RAINBOW, COVER)
SAME = "same"  # the mini player's bars look like the big visualiser's
MINI_BASIC_COLOURS = (SAME, *BASIC_COLOURS)


def is_colours(value: str) -> bool:
    """Whether the big visualiser's bars can have these colours."""
    return value in BASIC_COLOURS or value in all_flags()


def is_mini_colours(value: str) -> bool:
    return value == SAME or is_colours(value)


DARKEST_FLAG_LIGHTNESS = 45  # out of 255: black stripes would vanish on a dark window
FLAG_ICON_SIZE = (24, 16)


@dataclass(frozen=True)
class VisualiserSettings:
    bars: int = 12
    gap: int = 8  # pixels between bars
    opacity: int = 100  # percent; the lyrics are drawn on top of the bars
    fall_speed: int = 1  # an index into FALL_PER_FRAME
    colours: str = HIGHLIGHT  # one of BASIC_COLOURS, or a flag's id (flags.py)
    mini_colours: str = SAME  # SAME, or one of the above
    mini_bars: int = 12
    mini_gap: int = 50  # percent of `gap` the mini player uses; it is small
    background: bool = False  # the playing song's cover, blurred, behind the big bars
    background_blur: int = 100  # pixels
    background_darken: int = 50  # percent
    background_fit: int = FILL  # FILL, FIT or STRETCH
    mini_background: bool = False  # the cover behind the mini player's bars
    glow: bool = False  # a soft light around the bars, in their colours
    smooth: bool = False  # 60 frames a second instead of 30


def for_mini_player(style: VisualiserSettings) -> VisualiserSettings:
    """The settings as the mini player's visualiser uses them: its own colours, bars,
    gap and background; the rest as the big one has them."""
    return replace(
        style,
        colours=style.colours if style.mini_colours == SAME else style.mini_colours,
        bars=style.mini_bars,
        gap=round(style.gap * style.mini_gap / 100),
        background=style.mini_background,
    )


def bar_colours(colours: str, bar_count: int, highlight: QColor) -> list[QColor]:
    """The colour of each bar, left to right."""
    if colours == RAINBOW:
        return [
            QColor.fromHsv(RAINBOW_HUES * bar // max(1, bar_count - 1), 220, 235)
            for bar in range(bar_count)
        ]
    stripes = flag_stripes(colours)
    if stripes is None:
        return [QColor(highlight) for _ in range(bar_count)]
    result = []
    for bar in range(bar_count):  # each stripe gets an equal share of the bars
        color = QColor(stripes[bar * len(stripes) // bar_count])
        if color.lightness() < DARKEST_FLAG_LIGHTNESS:
            color = QColor.fromHsl(color.hslHue(), color.hslSaturation(), DARKEST_FLAG_LIGHTNESS)
        result.append(color)
    return result


def bars_for_flag(colours: str, bars: int) -> int:
    """The bar count nearest to `bars` that gives every stripe of the flag the same
    number of bars. Other colours keep `bars` as it is."""
    stripes = flag_stripes(colours)
    if stripes is None:
        return bars
    count = len(stripes)  # a wider stripe is listed twice, so it gets twice the bars
    per_stripe = max(1, round(bars / count))
    return per_stripe * count


def flag_icon(colours: str) -> QIcon:
    """A small picture of a flag (or the rainbow) for the combo box."""
    width, height = FLAG_ICON_SIZE
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    stripes = flag_stripes(colours) or [c.name() for c in bar_colours(RAINBOW, 6, QColor())]
    for index, stripe in enumerate(stripes):
        top = index * height // len(stripes)
        bottom = (index + 1) * height // len(stripes)
        painter.fillRect(0, top, width, bottom - top, QColor(stripe))
    painter.end()
    return QIcon(pixmap)


def _clamped(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def load_visualiser_settings(settings: QSettings) -> VisualiserSettings:
    d = VisualiserSettings()

    def number(key: str, default: int, low: int, high: int) -> int:
        return _clamped(settings.value(key, default, type=int), low, high)

    def flag(key: str, default: bool) -> bool:
        return settings.value(key, default, type=bool)

    colours = settings.value(COLOURS_KEY, d.colours, type=str)
    mini_colours = settings.value(MINI_COLOURS_KEY, d.mini_colours, type=str)
    return VisualiserSettings(
        bars=number(BARS_KEY, d.bars, 1, 200),
        gap=number(GAP_KEY, d.gap, 0, 100),
        opacity=number(OPACITY_KEY, d.opacity, 1, 100),
        fall_speed=number(FALL_SPEED_KEY, d.fall_speed, 0, len(FALL_PER_FRAME) - 1),
        colours=colours if is_colours(colours) else d.colours,
        mini_colours=mini_colours if is_mini_colours(mini_colours) else d.mini_colours,
        mini_bars=number(MINI_BARS_KEY, d.mini_bars, 1, 200),
        mini_gap=number(MINI_GAP_KEY, d.mini_gap, 0, 100),
        background=flag(BACKGROUND_KEY, d.background),
        background_blur=number(BACKGROUND_BLUR_KEY, d.background_blur, 0, 200),
        background_darken=number(BACKGROUND_DARKEN_KEY, d.background_darken, 0, 100),
        background_fit=number(BACKGROUND_FIT_KEY, d.background_fit, FILL, STRETCH),
        mini_background=flag(MINI_BACKGROUND_KEY, d.mini_background),
        glow=flag(GLOW_KEY, d.glow),
        smooth=flag(SMOOTH_KEY, d.smooth),
    )


def _clean_style(style: VisualiserSettings) -> VisualiserSettings:
    """The settings with every value within its limits."""
    d = VisualiserSettings()
    return replace(
        style,
        bars=_clamped(style.bars, 1, 200),
        gap=_clamped(style.gap, 0, 100),
        opacity=_clamped(style.opacity, 1, 100),
        fall_speed=_clamped(style.fall_speed, 0, len(FALL_PER_FRAME) - 1),
        colours=style.colours if is_colours(style.colours) else d.colours,
        mini_colours=style.mini_colours if is_mini_colours(style.mini_colours) else d.mini_colours,
        mini_bars=_clamped(style.mini_bars, 1, 200),
        mini_gap=_clamped(style.mini_gap, 0, 100),
        background_blur=_clamped(style.background_blur, 0, 200),
        background_darken=_clamped(style.background_darken, 0, 100),
        background_fit=_clamped(style.background_fit, FILL, STRETCH),
    )


def _parse_style(data: object) -> VisualiserSettings | None:
    """A saved preset as settings; values it lacks are the defaults."""
    if not isinstance(data, dict):
        return None
    d = VisualiserSettings()
    values = {}
    for item in fields(VisualiserSettings):
        value = data.get(item.name)
        default = getattr(d, item.name)
        # bool is a kind of int, so the types are compared exactly.
        if value is not None and type(value) is type(default):
            values[item.name] = value
    return _clean_style(VisualiserSettings(**values))


# Presets that come with MusicHive: they cannot be replaced or deleted.
BUILT_IN_PRESETS = {"Default": VisualiserSettings()}


def _load_user_presets(settings: QSettings) -> dict[str, VisualiserSettings]:
    """The presets the user saved, by name."""
    try:
        data = json.loads(settings.value(PRESETS_KEY, "", type=str))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    parsed = {str(name): _parse_style(values) for name, values in data.items()}
    return {
        name: style
        for name, style in parsed.items()
        if style is not None and name not in BUILT_IN_PRESETS
    }


def load_visualiser_presets(settings: QSettings) -> dict[str, VisualiserSettings]:
    """Every visualiser preset by name, the built-in ones first."""
    return {**BUILT_IN_PRESETS, **_load_user_presets(settings)}


def save_visualiser_presets(settings: QSettings, presets: dict[str, VisualiserSettings]) -> None:
    data = {name: asdict(style) for name, style in presets.items()}
    settings.setValue(PRESETS_KEY, json.dumps(data))


def visualiser_preset_names(settings: QSettings) -> list[str]:
    """Every preset, built-in ones first, as the settings window lists them."""
    return [*BUILT_IN_PRESETS, *sorted(_load_user_presets(settings), key=str.casefold)]


def save_visualiser_settings(settings: QSettings, style: VisualiserSettings) -> None:
    for key, value in (
        (BARS_KEY, style.bars),
        (GAP_KEY, style.gap),
        (OPACITY_KEY, style.opacity),
        (FALL_SPEED_KEY, style.fall_speed),
        (COLOURS_KEY, style.colours),
        (MINI_COLOURS_KEY, style.mini_colours),
        (MINI_BARS_KEY, style.mini_bars),
        (MINI_GAP_KEY, style.mini_gap),
        (BACKGROUND_KEY, style.background),
        (BACKGROUND_BLUR_KEY, style.background_blur),
        (BACKGROUND_DARKEN_KEY, style.background_darken),
        (BACKGROUND_FIT_KEY, style.background_fit),
        (MINI_BACKGROUND_KEY, style.mini_background),
        (GLOW_KEY, style.glow),
        (SMOOTH_KEY, style.smooth),
    ):
        settings.setValue(key, value)


class VisualiserSettingsTab:
    """Connects the visualiser options of settings.ui. Every change applies at once."""

    def __init__(
        self,
        dialog: QDialog,
        settings: QSettings,
        on_change: Callable[[VisualiserSettings], None],
    ) -> None:
        self._dialog = d = dialog
        self._settings = settings
        self._on_change = on_change

        self._bars = NumberField(d.visualiserBars)
        self._gap = NumberField(d.visualiserGap)
        self._opacity = NumberField(d.visualiserOpacity)
        self._mini_bars = NumberField(d.visualiserMiniBars)
        self._mini_gap = NumberField(d.visualiserMiniGap)
        self._blur = NumberField(d.visualiserBackgroundBlur)
        self._darken = NumberField(d.visualiserBackgroundDarken)
        self._showing = False  # True while _show fills in the fields
        self._fill_colour_lists()
        self._show(load_visualiser_settings(settings))

        for field in (
            self._bars, self._gap, self._opacity,
            self._mini_bars, self._mini_gap, self._blur, self._darken,
        ):
            field.valueChanged.connect(lambda _value: self._changed())
        for combo in (d.visualiserFallSpeed, d.visualiserBackgroundFit):
            combo.activated.connect(lambda _index: self._changed())
        for checkbox in (
            d.visualiserBackground, d.visualiserMiniBackground, d.visualiserGlow, d.visualiserSmooth,
        ):
            checkbox.toggled.connect(lambda _on: self._changed())
        d.visualiserColours.activated.connect(lambda _index: self._colours_chosen())
        d.visualiserMiniColours.activated.connect(lambda _index: self._mini_colours_chosen())
        d.visualiserReset.clicked.connect(lambda _checked=False: self._reset())
        d.visualiserFlagsFolder.clicked.connect(lambda _checked=False: self._open_flags_folder())
        d.visualiserPreset.activated.connect(lambda _index: self._preset_chosen())
        d.visualiserSavePreset.clicked.connect(lambda _checked=False: self._save_preset())
        d.visualiserDeletePreset.clicked.connect(lambda _checked=False: self._delete_preset())
        self._fill_presets()

    def _show(self, style: VisualiserSettings) -> None:
        d = self._dialog
        self._showing = True
        self._bars.setValue(style.bars)
        self._gap.setValue(style.gap)
        self._opacity.setValue(style.opacity)
        d.visualiserFallSpeed.setCurrentIndex(style.fall_speed)
        d.visualiserColours.setCurrentIndex(max(0, d.visualiserColours.findData(style.colours)))
        d.visualiserMiniColours.setCurrentIndex(
            max(0, d.visualiserMiniColours.findData(style.mini_colours))
        )
        self._mini_bars.setValue(style.mini_bars)
        self._mini_gap.setValue(style.mini_gap)
        d.visualiserBackground.setChecked(style.background)
        self._blur.setValue(style.background_blur)
        self._darken.setValue(style.background_darken)
        d.visualiserBackgroundFit.setCurrentIndex(style.background_fit)
        d.visualiserMiniBackground.setChecked(style.mini_background)
        d.visualiserGlow.setChecked(style.glow)
        d.visualiserSmooth.setChecked(style.smooth)
        self._showing = False
        self._show_background_options()

    def _show_background_options(self) -> None:
        """The background's options only matter while it is on, big or mini."""
        d = self._dialog
        on = d.visualiserBackground.isChecked() or d.visualiserMiniBackground.isChecked()
        for widget in (
            d.visualiserBackgroundBlurLabel,
            d.visualiserBackgroundBlur,
            d.visualiserBackgroundDarkenLabel,
            d.visualiserBackgroundDarken,
            d.visualiserBackgroundFitLabel,
            d.visualiserBackgroundFit,
        ):
            widget.setEnabled(on)

    # ----- the colour lists and the flags -----

    def _fill_colour_lists(self) -> None:
        """The colours set in settings.ui, then a flag for each flag file."""
        flags = all_flags()
        cover_icon = QIcon(str(theme.asset_path("image.svg")))
        for combo, basics in (
            (self._dialog.visualiserColours, BASIC_COLOURS),
            (self._dialog.visualiserMiniColours, MINI_BASIC_COLOURS),
        ):
            while combo.count() > len(basics):
                combo.removeItem(combo.count() - 1)
            for index, colours in enumerate(basics):
                combo.setItemData(index, colours)
                if colours == COVER:
                    combo.setItemIcon(index, cover_icon)
                elif colours == RAINBOW:
                    combo.setItemIcon(index, flag_icon(RAINBOW))
            for flag in flags.values():
                combo.addItem(flag_icon(flag.id), flag.name, flag.id)

    def reload_flags(self) -> None:
        """Read the flag files again, and show them in the colour lists."""
        reload_flags()
        current = load_visualiser_settings(self._settings)  # a deleted flag falls back
        self._fill_colour_lists()
        self._show(current)

    def _open_flags_folder(self) -> None:
        try:
            folder = prepare_user_flags_dir()
        except OSError as error:
            QMessageBox.warning(self._dialog, "Custom flags", f"Cannot make the folder: {error}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _colours_chosen(self) -> None:
        self._fit_bars_to_flag(self._bars, self._dialog.visualiserColours.currentData())
        self._changed()

    def _mini_colours_chosen(self) -> None:
        colours = self._dialog.visualiserMiniColours.currentData()
        if colours == SAME:
            colours = self._dialog.visualiserColours.currentData()
        self._fit_bars_to_flag(self._mini_bars, colours)
        self._changed()

    @staticmethod
    def _fit_bars_to_flag(bars: NumberField, colours: str) -> None:
        """A flag is a preset: the bars change so that every stripe gets as many."""
        wanted = bars_for_flag(colours, bars.value())
        bars.setValue(wanted)
        if bars.value() != wanted:  # outside the field's limits: one bar per stripe
            step = len(flag_stripes(colours))  # less, or more
            bars.setValue(wanted - step if bars.value() < wanted else wanted + step)

    def _reset(self) -> None:
        self._show(VisualiserSettings())
        self._changed()

    # ----- presets -----

    def _fill_presets(self) -> None:
        combo = self._dialog.visualiserPreset
        combo.clear()
        combo.addItems(list(BUILT_IN_PRESETS))
        user_presets = sorted(_load_user_presets(self._settings), key=str.casefold)
        if user_presets:
            combo.insertSeparator(combo.count())
            combo.addItems(user_presets)
        self._show_preset_name()

    def _show_preset_name(self) -> None:
        """Select the preset the settings match; none if they match no preset."""
        d = self._dialog
        style = self._entered_style()
        presets = load_visualiser_presets(self._settings)
        name = next((name for name, preset in presets.items() if preset == style), "")
        d.visualiserPreset.setCurrentIndex(d.visualiserPreset.findText(name) if name else -1)
        d.visualiserDeletePreset.setEnabled(bool(name) and name not in BUILT_IN_PRESETS)

    def _preset_chosen(self) -> None:
        name = self._dialog.visualiserPreset.currentText()
        preset = load_visualiser_presets(self._settings).get(name)
        if preset is not None:
            self._show(preset)
            self._changed()

    def _save_preset(self) -> None:
        d = self._dialog
        presets = _load_user_presets(self._settings)
        current = d.visualiserPreset.currentText() if d.visualiserPreset.currentIndex() >= 0 else ""
        suggestion = current if current in presets else ""
        name, accepted = QInputDialog.getText(d, "Save preset", "Preset name:", text=suggestion)
        name = name.strip()
        if not accepted or not name:
            return
        if name in BUILT_IN_PRESETS:
            QMessageBox.warning(d, "Save preset", "A built-in preset already has that name.")
            return
        if name in presets and name != suggestion:
            answer = QMessageBox.question(d, "Save preset", f"Replace the preset '{name}'?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        presets[name] = self._entered_style()
        save_visualiser_presets(self._settings, presets)
        self._fill_presets()

    def _delete_preset(self) -> None:
        d = self._dialog
        name = d.visualiserPreset.currentText()
        presets = _load_user_presets(self._settings)
        if d.visualiserPreset.currentIndex() < 0 or name not in presets:
            return
        answer = QMessageBox.question(d, "Delete preset", f"Delete the preset '{name}'?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        del presets[name]
        save_visualiser_presets(self._settings, presets)
        self._fill_presets()

    # ----- changes -----

    def _changed(self) -> None:
        if self._showing:
            return
        self._show_background_options()
        style = self._entered_style()
        save_visualiser_settings(self._settings, style)
        self._show_preset_name()
        self._on_change(style)

    def _entered_style(self) -> VisualiserSettings:
        """The settings as the widgets show them."""
        d = self._dialog
        return VisualiserSettings(
            bars=self._bars.value(),
            gap=self._gap.value(),
            opacity=self._opacity.value(),
            fall_speed=d.visualiserFallSpeed.currentIndex(),
            colours=d.visualiserColours.currentData() or HIGHLIGHT,
            mini_colours=d.visualiserMiniColours.currentData() or SAME,
            mini_bars=self._mini_bars.value(),
            mini_gap=self._mini_gap.value(),
            background=d.visualiserBackground.isChecked(),
            background_blur=self._blur.value(),
            background_darken=self._darken.value(),
            background_fit=d.visualiserBackgroundFit.currentIndex(),
            mini_background=d.visualiserMiniBackground.isChecked(),
            glow=d.visualiserGlow.isChecked(),
            smooth=d.visualiserSmooth.isChecked(),
        )
