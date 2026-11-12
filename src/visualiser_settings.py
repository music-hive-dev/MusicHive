"""The visualiser part of the Now Playing tab of the settings window, and its settings."""

from collections.abc import Callable
from dataclasses import dataclass, replace

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QDialog

from src import theme
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

# How the cover fills the space, for each entry of visualiserBackgroundFit.
FILL, FIT, STRETCH = range(3)

# How much of its height a bar keeps each frame, for each entry of
# visualiserFallSpeed in settings.ui: slow, normal, fast.
FALL_PER_FRAME = (0.93, 0.86, 0.75)

HIGHLIGHT = "highlight"  # every bar in the highlight colour
RAINBOW = "rainbow"  # a smooth rainbow from red to violet
COVER = "cover"  # the playing cover's colours: the bars, and the background behind them
RAINBOW_HUES = 300  # the rainbow runs from red (0) to violet (300) across the bars

# The stripes of each flag, top to bottom; they become the bars, left to right.
# A colour listed twice is a stripe twice as wide (the bisexual flag is 2:1:2).
FLAGS = {
    "pride": ("#E40303", "#FF8C00", "#FFED00", "#008026", "#24408E", "#732982"),
    # The chevron cannot be bars, so its colours come first, then the rainbow.
    "progress": (
        "#000000", "#784F17", "#5BCEFA", "#F5A9B8", "#FFFFFF",
        "#E40303", "#FF8C00", "#FFED00", "#008026", "#24408E", "#732982",
    ),
    "gay": ("#078D70", "#26CEAA", "#98E8C1", "#FFFFFF", "#7BADE2", "#5049CC", "#3D1A78"),
    "lesbian": ("#D52D00", "#EF7627", "#FF9A56", "#FFFFFF", "#D162A4", "#B55690", "#A30262"),
    "bisexual": ("#D60270", "#D60270", "#9B4F96", "#0038A8", "#0038A8"),
    "pansexual": ("#FF218C", "#FFD800", "#21B1FF"),
    "transgender": ("#5BCEFA", "#F5A9B8", "#FFFFFF", "#F5A9B8", "#5BCEFA"),
    "nonbinary": ("#FCF434", "#FFFFFF", "#9C59D1", "#2C2C2C"),
    "genderfluid": ("#FF76A4", "#FFFFFF", "#C011D7", "#000000", "#2F3CBE"),
    "genderqueer": ("#B57EDC", "#FFFFFF", "#4A8123"),
    "agender": ("#000000", "#BCC4C7", "#FFFFFF", "#B7F684", "#FFFFFF", "#BCC4C7", "#000000"),
    "asexual": ("#000000", "#A3A3A3", "#FFFFFF", "#800080"),
    "aromantic": ("#3DA542", "#A7D379", "#FFFFFF", "#A9A9A9", "#000000"),
    "intersex": ("#FFD800", "#7902AA", "#FFD800"),
}

# What each entry of visualiserColours stands for, in the order settings.ui lists them.
COLOURS = (HIGHLIGHT, RAINBOW, COVER, *FLAGS)
SAME = "same"  # the mini player's bars look like the big visualiser's
# What each entry of visualiserMiniColours stands for.
MINI_COLOURS = (SAME, *COLOURS)

DARKEST_FLAG_LIGHTNESS = 45  # out of 255: black stripes would vanish on a dark window
FLAG_ICON_SIZE = (24, 16)


@dataclass(frozen=True)
class VisualiserSettings:
    bars: int = 12
    gap: int = 8  # pixels between bars
    opacity: int = 100  # percent; the lyrics are drawn on top of the bars
    fall_speed: int = 1  # an index into FALL_PER_FRAME
    colours: str = HIGHLIGHT  # one of COLOURS
    mini_colours: str = SAME  # one of MINI_COLOURS
    mini_bars: int = 12
    mini_gap: int = 50  # percent of `gap` the mini player uses; it is small
    background: bool = False  # the playing song's cover, blurred, behind the big bars
    background_blur: int = 100  # pixels
    background_darken: int = 50  # percent
    background_fit: int = FILL  # FILL, FIT or STRETCH
    mini_background: bool = False  # the cover behind the mini player's bars


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
    stripes = FLAGS.get(colours)
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
    stripes = FLAGS.get(colours)
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
    stripes = FLAGS.get(colours) or [c.name() for c in bar_colours(RAINBOW, 6, QColor())]
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
        colours=colours if colours in COLOURS else d.colours,
        mini_colours=mini_colours if mini_colours in MINI_COLOURS else d.mini_colours,
        mini_bars=number(MINI_BARS_KEY, d.mini_bars, 1, 200),
        mini_gap=number(MINI_GAP_KEY, d.mini_gap, 0, 100),
        background=flag(BACKGROUND_KEY, d.background),
        background_blur=number(BACKGROUND_BLUR_KEY, d.background_blur, 0, 200),
        background_darken=number(BACKGROUND_DARKEN_KEY, d.background_darken, 0, 100),
        background_fit=number(BACKGROUND_FIT_KEY, d.background_fit, FILL, STRETCH),
        mini_background=flag(MINI_BACKGROUND_KEY, d.mini_background),
    )


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
        for combo, choices in (
            (d.visualiserColours, COLOURS),
            (d.visualiserMiniColours, MINI_COLOURS),
        ):
            for index, colours in enumerate(choices[: combo.count()]):
                if colours == COVER:
                    combo.setItemIcon(index, QIcon(str(theme.asset_path("image.svg"))))
                elif colours not in (HIGHLIGHT, SAME):
                    combo.setItemIcon(index, flag_icon(colours))
        self._show(load_visualiser_settings(settings))

        for field in (
            self._bars, self._gap, self._opacity,
            self._mini_bars, self._mini_gap, self._blur, self._darken,
        ):
            field.valueChanged.connect(lambda _value: self._changed())
        for combo in (d.visualiserFallSpeed, d.visualiserBackgroundFit):
            combo.activated.connect(lambda _index: self._changed())
        for checkbox in (d.visualiserBackground, d.visualiserMiniBackground):
            checkbox.toggled.connect(lambda _on: self._changed())
        d.visualiserColours.activated.connect(lambda _index: self._colours_chosen())
        d.visualiserMiniColours.activated.connect(lambda _index: self._mini_colours_chosen())
        d.visualiserReset.clicked.connect(lambda _checked=False: self._reset())

    def _show(self, style: VisualiserSettings) -> None:
        d = self._dialog
        self._showing = True
        self._bars.setValue(style.bars)
        self._gap.setValue(style.gap)
        self._opacity.setValue(style.opacity)
        d.visualiserFallSpeed.setCurrentIndex(style.fall_speed)
        d.visualiserColours.setCurrentIndex(COLOURS.index(style.colours))
        d.visualiserMiniColours.setCurrentIndex(MINI_COLOURS.index(style.mini_colours))
        self._mini_bars.setValue(style.mini_bars)
        self._mini_gap.setValue(style.mini_gap)
        d.visualiserBackground.setChecked(style.background)
        self._blur.setValue(style.background_blur)
        self._darken.setValue(style.background_darken)
        d.visualiserBackgroundFit.setCurrentIndex(style.background_fit)
        d.visualiserMiniBackground.setChecked(style.mini_background)
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

    def _colours_chosen(self) -> None:
        self._fit_bars_to_flag(self._bars, COLOURS[self._dialog.visualiserColours.currentIndex()])
        self._changed()

    def _mini_colours_chosen(self) -> None:
        colours = MINI_COLOURS[self._dialog.visualiserMiniColours.currentIndex()]
        if colours == SAME:
            colours = COLOURS[self._dialog.visualiserColours.currentIndex()]
        self._fit_bars_to_flag(self._mini_bars, colours)
        self._changed()

    @staticmethod
    def _fit_bars_to_flag(bars: NumberField, colours: str) -> None:
        """A flag is a preset: the bars change so that every stripe gets as many."""
        wanted = bars_for_flag(colours, bars.value())
        bars.setValue(wanted)
        if bars.value() != wanted:  # outside the field's limits: one bar per stripe
            step = len(FLAGS[colours])  # less, or more
            bars.setValue(wanted - step if bars.value() < wanted else wanted + step)

    def _reset(self) -> None:
        self._show(VisualiserSettings())
        self._changed()

    def _changed(self) -> None:
        if self._showing:
            return
        d = self._dialog
        self._show_background_options()
        style = VisualiserSettings(
            bars=self._bars.value(),
            gap=self._gap.value(),
            opacity=self._opacity.value(),
            fall_speed=d.visualiserFallSpeed.currentIndex(),
            colours=COLOURS[d.visualiserColours.currentIndex()],
            mini_colours=MINI_COLOURS[d.visualiserMiniColours.currentIndex()],
            mini_bars=self._mini_bars.value(),
            mini_gap=self._mini_gap.value(),
            background=d.visualiserBackground.isChecked(),
            background_blur=self._blur.value(),
            background_darken=self._darken.value(),
            background_fit=d.visualiserBackgroundFit.currentIndex(),
            mini_background=d.visualiserMiniBackground.isChecked(),
        )
        save_visualiser_settings(self._settings, style)
        self._on_change(style)
