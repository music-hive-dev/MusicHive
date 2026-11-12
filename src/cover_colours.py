"""Picking a cover's important colours, for the visualiser's "From the cover" colours.

The cover is shrunk, its colours are put in buckets, and each bucket is weighed
by how much of the cover it fills, with colourful buckets counting extra: a grey
or black background is often the biggest part of a cover, yet it is the bright
colours that make it recognisable. The darker of the two chosen colours becomes
the background and the brighter one the bars.
"""

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage

SAMPLE_SIDE = 48  # pixels; enough to see the colours, quick to count
BUCKET_SHIFT = 4  # 16 steps per channel
CANDIDATES = 8  # buckets that are compared for the two colours
MIN_DISTANCE = 90  # how different (in RGB) the two colours must be
COLOURFUL_WEIGHT = 2.5  # how much more a fully saturated colour counts
# Lightness (HSL, 0–255) the two colours are pushed to, so the bars stand out.
DARK_BACKGROUND_MAX = 60
LIGHT_BACKGROUND_MIN = 225
BARS_ON_DARK_MIN = 150
BARS_ON_LIGHT_MAX = 140


@dataclass(frozen=True)
class CoverColours:
    background: QColor
    bars: QColor


def _distance(a: QColor, b: QColor) -> float:
    return ((a.red() - b.red()) ** 2 + (a.green() - b.green()) ** 2 + (a.blue() - b.blue()) ** 2) ** 0.5


def _with_lightness(colour: QColor, lightness: int) -> QColor:
    return QColor.fromHsl(max(0, colour.hslHue()), colour.hslSaturation(), max(0, min(255, lightness)))


def important_colours(image: QImage) -> list[QColor]:
    """The cover's most important colours, most important first."""
    small = image.scaled(
        SAMPLE_SIDE, SAMPLE_SIDE,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_RGB32)
    buckets: dict[tuple[int, int, int], list[int]] = {}  # key -> [count, r sum, g sum, b sum]
    for y in range(small.height()):
        for x in range(small.width()):
            pixel = small.pixel(x, y)
            r, g, b = (pixel >> 16) & 0xFF, (pixel >> 8) & 0xFF, pixel & 0xFF
            key = (r >> BUCKET_SHIFT, g >> BUCKET_SHIFT, b >> BUCKET_SHIFT)
            bucket = buckets.setdefault(key, [0, 0, 0, 0])
            bucket[0] += 1
            bucket[1] += r
            bucket[2] += g
            bucket[3] += b
    weighed = []
    for count, r, g, b in buckets.values():
        colour = QColor(r // count, g // count, b // count)
        saturation = colour.hsvSaturationF() if colour.value() > 25 else 0.0
        weighed.append((count * (1 + COLOURFUL_WEIGHT * saturation), colour))
    weighed.sort(key=lambda pair: pair[0], reverse=True)
    chosen: list[QColor] = []
    for _weight, colour in weighed:  # similar buckets count as one colour
        if all(_distance(colour, other) >= MIN_DISTANCE / 2 for other in chosen):
            chosen.append(colour)
        if len(chosen) >= CANDIDATES:
            break
    return chosen


def cover_colours(image: QImage | None, dark_mode: bool) -> CoverColours | None:
    """The background and bar colours for this cover (None without a cover)."""
    if image is None or image.isNull():
        return None
    colours = important_colours(image)
    if not colours:
        return None
    main = colours[0]
    # The most striking of the others that is clearly different from the main one.
    others = [c for c in colours[1:] if _distance(c, main) >= MIN_DISTANCE]
    accent = max(others, key=lambda c: c.hsvSaturationF() * c.valueF(), default=None)
    if accent is None:  # a cover of one colour: lighter and darker shades of it
        accent = main
    darker, brighter = sorted((main, accent), key=lambda c: c.lightness())
    if dark_mode:
        background = _with_lightness(darker, min(darker.lightness(), DARK_BACKGROUND_MAX))
        bars = _with_lightness(brighter, max(brighter.lightness(), BARS_ON_DARK_MIN))
    else:
        # Light mode: a light tint behind the lyrics' dark text, the stronger colour on top.
        background = _with_lightness(brighter, max(brighter.lightness(), LIGHT_BACKGROUND_MIN))
        bars = _with_lightness(darker, min(darker.lightness(), BARS_ON_LIGHT_MAX))
    return CoverColours(background, bars)
