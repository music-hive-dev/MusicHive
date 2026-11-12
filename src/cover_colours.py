"""Picking a cover's important colours, for the visualiser's "From the cover" colours
and the lyrics' colour.

The cover is shrunk, its colours are put in buckets, and each bucket is weighed
by how much of the cover it fills, with colourful buckets counting extra: a grey
or black background is often the biggest part of a cover, yet it is the bright
colours that make it recognisable. The darker of the two chosen colours becomes
the background and the brighter one the bars. Only colours that fill a fair
part of the cover can become the bars.

The lyrics get a third colour: the most striking one that differs from both,
however little of the cover it fills, such as red lips on a black-and-white
photo. Without one, the lyrics keep the normal text colour.

Working this out takes a moment, so the result is kept in a file in the data
folder, under a fingerprint of the cover's pixels: a cover is only looked at
again when its picture changes.
"""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QColor, QImage

SAMPLE_SIDE = 48  # pixels; enough to see the colours, quick to count
BUCKET_SHIFT = 4  # 16 steps per channel
CANDIDATES = 8  # buckets that are compared for the two colours
LYRICS_CANDIDATES = 8  # the most striking buckets kept for the lyrics' colour
MIN_DISTANCE = 90  # how different (in RGB) the two colours must be
COLOURFUL_WEIGHT = 2.5  # how much more a fully saturated colour counts
BARS_MIN_SHARE = 0.03  # a colour must fill this much of the cover to colour the bars
LYRICS_MIN_PIXELS = 4  # of the shrunk cover: smaller specks are noise
LYRICS_MIN_SATURATION = 0.3  # greys never stand out enough for the lyrics
# Lightness (HSL, 0–255) the colours are pushed to, so the bars and lyrics stand out.
DARK_BACKGROUND_MAX = 60
LIGHT_BACKGROUND_MIN = 225
BARS_ON_DARK_MIN = 150
BARS_ON_LIGHT_MAX = 140
LYRICS_ON_DARK_MIN = 130
LYRICS_ON_LIGHT_MAX = 110

CACHE_FILE_NAME = "cover_colours.json"
CACHE_VERSION = 1  # raised when what is kept changes, so old entries are worked out again


@dataclass(frozen=True)
class CoverColours:
    background: QColor
    bars: QColor
    lyrics: QColor | None  # None: the cover has no colour of its own for the lyrics


@dataclass(frozen=True)
class _Analysis:
    """What is kept of a cover: its important colours with the share of the cover
    each fills (most important first), and its most striking colours (most first)."""

    colours: list[tuple[QColor, float]]
    striking: list[QColor]


def _distance(a: QColor, b: QColor) -> float:
    return ((a.red() - b.red()) ** 2 + (a.green() - b.green()) ** 2 + (a.blue() - b.blue()) ** 2) ** 0.5


def _with_lightness(colour: QColor, lightness: int) -> QColor:
    return QColor.fromHsl(max(0, colour.hslHue()), colour.hslSaturation(), max(0, min(255, lightness)))


def _colourfulness(colour: QColor) -> float:
    return colour.hsvSaturationF() * colour.valueF() if colour.value() > 25 else 0.0


# ----- the kept results -----

_cache: dict[str, dict] | None = None  # fingerprint -> analysis, as saved


def _cache_file() -> Path:
    from src.library_db import data_dir  # here, so that this module loads on its own

    return data_dir() / CACHE_FILE_NAME


def _load_cache() -> dict[str, dict]:
    global _cache
    if _cache is None:
        try:
            data = json.loads(_cache_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            data = {"version": CACHE_VERSION, "covers": {}}
        if not isinstance(data.get("covers"), dict):
            data["covers"] = {}
        _cache = data
    return _cache["covers"]


def _save_cache() -> None:
    path = _cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(_cache, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass  # it is worked out again next time


def _to_saved(analysis: _Analysis) -> dict:
    return {
        "colours": [[colour.name(), round(share, 4)] for colour, share in analysis.colours],
        "striking": [colour.name() for colour in analysis.striking],
    }


def _from_saved(data: object) -> _Analysis | None:
    try:
        colours = [(QColor(name), float(share)) for name, share in data["colours"]]
        striking = [QColor(name) for name in data["striking"]]
    except (KeyError, TypeError, ValueError):
        return None
    if not colours or not all(colour.isValid() for colour, _share in colours):
        return None
    return _Analysis(colours, [colour for colour in striking if colour.isValid()])


# ----- looking at a cover -----


def _analyse(small: QImage) -> _Analysis:
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
    total = max(1, small.width() * small.height())
    weighed = []  # (weight, colour, share of the cover)
    for count, r, g, b in buckets.values():
        colour = QColor(r // count, g // count, b // count)
        weighed.append((count * (1 + COLOURFUL_WEIGHT * _colourfulness(colour)), colour, count / total))
    weighed.sort(key=lambda entry: entry[0], reverse=True)

    chosen: list[list] = []  # [colour, share]; similar buckets count as one colour
    for _weight, colour, share in weighed:
        similar = next((entry for entry in chosen if _distance(colour, entry[0]) < MIN_DISTANCE / 2), None)
        if similar is not None:
            similar[1] += share
        elif len(chosen) < CANDIDATES:
            chosen.append([colour, share])

    striking = sorted(
        (
            colour
            for _weight, colour, share in weighed
            if share * total >= LYRICS_MIN_PIXELS
            and colour.hsvSaturationF() >= LYRICS_MIN_SATURATION
            and colour.value() > 25
        ),
        key=_colourfulness,
        reverse=True,
    )
    picked: list[QColor] = []
    for colour in striking:  # one of each kind of colour
        if all(_distance(colour, other) >= MIN_DISTANCE / 2 for other in picked):
            picked.append(colour)
        if len(picked) >= LYRICS_CANDIDATES:
            break
    return _Analysis([(colour, share) for colour, share in chosen], picked)


def _analysis_of(image: QImage) -> _Analysis | None:
    """The cover's colours, from the kept results when this picture was seen before."""
    small = image.scaled(
        SAMPLE_SIDE, SAMPLE_SIDE,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_RGB32)
    pixels = QByteArray()
    buffer = QBuffer(pixels)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    small.save(buffer, "BMP")  # a plain copy of the pixels, to take a fingerprint of
    buffer.close()
    fingerprint = hashlib.sha1(pixels.data()).hexdigest()
    cache = _load_cache()
    analysis = _from_saved(cache.get(fingerprint))
    if analysis is None:
        analysis = _analyse(small)
        if not analysis.colours:
            return None
        cache[fingerprint] = _to_saved(analysis)
        _save_cache()
    return analysis


def important_colours(image: QImage) -> list[QColor]:
    """The cover's most important colours, most important first."""
    analysis = _analysis_of(image)
    return [colour for colour, _share in analysis.colours] if analysis else []


def cover_colours(image: QImage | None, dark_mode: bool) -> CoverColours | None:
    """The background, bar and lyrics colours for this cover (None without a cover)."""
    if image is None or image.isNull():
        return None
    analysis = _analysis_of(image)
    if analysis is None:
        return None
    main = analysis.colours[0][0]
    # The most striking of the others that is clearly different from the main one
    # and fills enough of the cover; small specks are left for the lyrics.
    others = [
        colour
        for colour, share in analysis.colours[1:]
        if share >= BARS_MIN_SHARE and _distance(colour, main) >= MIN_DISTANCE
    ]
    accent = max(others, key=_colourfulness, default=None)
    if accent is None:  # a cover of one colour: lighter and darker shades of it
        accent = main
    darker, brighter = sorted((main, accent), key=lambda c: c.lightness())
    lyrics = next(
        (
            colour
            for colour in analysis.striking
            if _distance(colour, main) >= MIN_DISTANCE and _distance(colour, accent) >= MIN_DISTANCE
        ),
        None,
    )
    if dark_mode:
        background = _with_lightness(darker, min(darker.lightness(), DARK_BACKGROUND_MAX))
        bars = _with_lightness(brighter, max(brighter.lightness(), BARS_ON_DARK_MIN))
        if lyrics is not None:
            lyrics = _with_lightness(lyrics, max(lyrics.lightness(), LYRICS_ON_DARK_MIN))
    else:
        # Light mode: a light tint behind the lyrics' dark text, the stronger colour on top.
        background = _with_lightness(brighter, max(brighter.lightness(), LIGHT_BACKGROUND_MIN))
        bars = _with_lightness(darker, min(darker.lightness(), BARS_ON_LIGHT_MAX))
        if lyrics is not None:
            lyrics = _with_lightness(lyrics, min(lyrics.lightness(), LYRICS_ON_LIGHT_MAX))
    return CoverColours(background, bars, lyrics)
