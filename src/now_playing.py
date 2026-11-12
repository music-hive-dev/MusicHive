"""The Now Playing tab: a spectrum visualiser and lyrics that follow the song."""

import bisect
import math
import time
from array import array
from collections import deque

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtMultimedia import QAudioBuffer, QAudioFormat
from PySide6.QtWidgets import (
    QGraphicsBlurEffect,
    QGraphicsDropShadowEffect,
    QGraphicsScene,
    QListWidget,
    QListWidgetItem,
    QWidget,
)

from src import theme
from src.lyrics import parse_lyrics
from src.cover_colours import CoverColours, cover_colours
from src.lyrics_settings import LyricsSettings
from src.visualiser_settings import (
    COVER,
    FALL_PER_FRAME,
    FILL,
    FIT,
    HIGHLIGHT,
    RAINBOW,
    VisualiserSettings,
    bar_colours,
)

FFT_SIZE = 1024
FRAME_MS = 33  # about 30 pictures a second
LOWEST_FREQUENCY = 40
HIGHEST_FREQUENCY = 16_000
QUIET_DB, LOUD_DB = -55.0, 5.0  # what counts as an empty bar and a full one
TILT_DB_PER_OCTAVE = 3.0  # music has less energy up high; this evens the bars out
AUDIO_GONE_SECONDS = 0.25  # without new sound for this long, the bars sink
# How much darker the foot and lighter the top of a bar are, in percent. Flags get
# a flatter gradient, so their colours stay true.
GRADIENT = (130, 140)
FLAG_GRADIENT = (110, 115)

_ARRAY_TYPES = {
    QAudioFormat.SampleFormat.UInt8: ("B", 1 / 128, -1.0),
    QAudioFormat.SampleFormat.Int16: ("h", 1 / 32768, 0.0),
    QAudioFormat.SampleFormat.Int32: ("i", 1 / 2147483648, 0.0),
    QAudioFormat.SampleFormat.Float: ("f", 1.0, 0.0),
}

NO_LYRICS_TEXT = "No lyrics for this song"
INSTRUMENTAL_LINE = "♪"
DIM_ALPHA = 120
CURRENT_LINE_EXTRA_POINTS = 20
# "Grow with the window": the lyrics' size at this view size (a normal window) is
# the size set in player.ui; a bigger view makes them bigger, up to this many times.
SCALE_REFERENCE = (700, 420)  # pixels
MAX_LYRICS_SCALE = 4.0
WHEEL_NOTCH = 120  # what one click of a mouse wheel reports
SCROLL_HOLD_MS = 5000  # after scrolling, the lyrics wait this long before following the song
BACKGROUND_WORK_SCALE = 0.25  # the cover is blurred at a quarter size: much faster


def _light_text() -> bool:
    """Dark mode: light text, so shadows are dark. Light mode: the other way round."""
    return QColor(theme.colour(theme.TEXT)).lightness() > 128


def set_text_shadow(widget: QWidget, style: LyricsSettings) -> None:
    """Give a see-through widget's text a soft shadow, or take it away.

    Only what is drawn casts it, so on a see-through widget that is the text.
    Light text gets a dark shadow; dark text (light mode) a light glow.
    """
    if not style.shadow:
        widget.setGraphicsEffect(None)  # also deletes the effect
        return
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(style.shadow_blur)
    effect.setOffset(QPointF(style.shadow_distance / 2, style.shadow_distance))
    color = QColor(Qt.GlobalColor.black if _light_text() else Qt.GlobalColor.white)
    color.setAlphaF(style.shadow_strength / 100)
    effect.setColor(color)
    widget.setGraphicsEffect(effect)


def blurred_cover(cover: QImage, width: int, height: int, style: VisualiserSettings) -> QPixmap:
    """The cover, sized to width x height as the settings say, blurred and dimmed."""
    work = BACKGROUND_WORK_SCALE if style.background_blur >= 8 else 1.0
    w, h = max(1, round(width * work)), max(1, round(height * work))
    radius = style.background_blur * work
    # Filling past the edges by the blur keeps them from fading to see-through.
    margin = 0 if style.background_fit == FIT else math.ceil(radius * 2)
    mode = {
        FILL: Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        FIT: Qt.AspectRatioMode.KeepAspectRatio,
    }.get(style.background_fit, Qt.AspectRatioMode.IgnoreAspectRatio)
    scaled = cover.scaled(
        w + 2 * margin, h + 2 * margin, mode, Qt.TransformationMode.SmoothTransformation
    )

    scene = QGraphicsScene()
    item = scene.addPixmap(QPixmap.fromImage(scaled))
    item.setPos((w - scaled.width()) / 2, (h - scaled.height()) / 2)
    if radius > 0:
        blur = QGraphicsBlurEffect()
        blur.setBlurRadius(radius)
        blur.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
        item.setGraphicsEffect(blur)
    result = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    scene.render(painter, QRectF(0, 0, w, h), QRectF(0, 0, w, h))
    # Dim it towards the window: darker in dark mode, lighter in light mode.
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
    dim = QColor(Qt.GlobalColor.black if _light_text() else Qt.GlobalColor.white)
    dim.setAlphaF(style.background_darken / 100)
    painter.fillRect(result.rect(), dim)
    painter.end()
    return QPixmap.fromImage(
        result.scaled(
            width,
            height,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    )


class Visualizer(QObject):
    """Draws a live spectrum in a plain QWidget. It only works while it is visible."""

    def __init__(self, widget: QWidget, corner_radius: int = 0) -> None:
        super().__init__(widget)
        self._widget = widget
        self._corner_radius = corner_radius  # for a widget with rounded corners
        self._cover: QImage | None = None  # the playing song's, for the background
        self._cover_colours: CoverColours | None = None  # picked from it, for "From the cover"
        self._background: QPixmap | None = None  # the cover as drawn, kept until it changes
        self._background_key: tuple | None = None
        self.enabled = True
        self._style = VisualiserSettings()
        self._samples: deque[float] = deque(maxlen=FFT_SIZE)
        self._levels = [0.0] * self._style.bars
        self._last_audio = 0.0
        self._sample_rate = 0
        self._bands: list[tuple[int, int, float]] = []  # first bin, end bin, tilt in dB

        self._window = [
            0.5 - 0.5 * math.cos(2 * math.pi * i / (FFT_SIZE - 1)) for i in range(FFT_SIZE)
        ]
        bits = int(math.log2(FFT_SIZE))
        self._reversed = [int(f"{i:0{bits}b}"[::-1], 2) for i in range(FFT_SIZE)]
        self._twiddles = [
            (math.cos(2 * math.pi * i / FFT_SIZE), -math.sin(2 * math.pi * i / FFT_SIZE))
            for i in range(FFT_SIZE // 2)
        ]

        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._next_frame)
        widget.installEventFilter(self)

    def set_enabled(self, enabled: bool) -> None:
        """Switch the bars on or off. While off, this does no work at all."""
        self.enabled = enabled
        if not enabled:
            self._timer.stop()
            self._levels = [0.0] * self._style.bars
            self._widget.update()
        elif self._widget.isVisible():
            self._timer.start()

    def set_cover(self, cover: QImage | None) -> None:
        """The playing song's cover, for the album art background and the colours
        taken from it (None: no cover)."""
        self._cover = cover
        self._cover_colours = cover_colours(cover, dark_mode=_light_text())
        self._background = None
        self._background_key = None
        self._widget.update()

    def _shows_background(self) -> bool:
        style = self._style
        return style.background and self._cover is not None

    def _background_for(self, width: int, height: int) -> QPixmap:
        """The blurred cover for this size, made again only when something changed."""
        style = self._style
        key = (
            self._cover.cacheKey(), width, height,
            style.background_blur, style.background_darken, style.background_fit, _light_text(),
        )
        if key != self._background_key:
            self._background = blurred_cover(self._cover, width, height, style)
            self._background_key = key
        return self._background

    def set_style(self, style: VisualiserSettings) -> None:
        """How the bars look: their number, gap, opacity, colours and fall speed."""
        bars_changed = style.bars != self._style.bars
        self._style = style
        if bars_changed:
            self._levels = [0.0] * style.bars
            if self._sample_rate:
                self._bands = self._make_bands()
        self._widget.update()

    def add_buffer(self, buffer: QAudioBuffer) -> None:
        """New sound from the player."""
        if not self._timer.isActive() or not buffer.isValid():
            return
        audio_format = buffer.format()
        kind = _ARRAY_TYPES.get(audio_format.sampleFormat())
        channels = audio_format.channelCount()
        if kind is None or channels < 1:
            return
        type_code, scale, shift = kind
        values = array(type_code)
        data = bytes(buffer.constData())
        values.frombytes(data[: len(data) - len(data) % values.itemsize])
        # The last moments of the first channel are all a picture needs.
        for value in values[-FFT_SIZE * channels :: channels]:
            self._samples.append(value * scale + shift)
        self._last_audio = time.monotonic()
        if audio_format.sampleRate() != self._sample_rate:
            self._sample_rate = audio_format.sampleRate()
            self._bands = self._make_bands()

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Show:
            if self.enabled:
                self._timer.start()
        elif kind == QEvent.Type.Hide:
            self._timer.stop()
            self._levels = [0.0] * self._style.bars
        elif kind == QEvent.Type.Paint:
            self._paint()
            return True
        return False

    def _make_bands(self) -> list[tuple[int, int, float]]:
        """Which FFT bins each bar covers, spaced as the ear hears pitch."""
        nyquist = self._sample_rate / 2
        top = min(HIGHEST_FREQUENCY, nyquist)
        hz_per_bin = self._sample_rate / FFT_SIZE
        bar_count = self._style.bars
        ratio = (top / LOWEST_FREQUENCY) ** (1 / bar_count)
        bands = []
        for bar in range(bar_count):
            low = LOWEST_FREQUENCY * ratio**bar
            first = max(1, int(low / hz_per_bin))
            end = max(first + 1, int(low * ratio / hz_per_bin))
            tilt = TILT_DB_PER_OCTAVE * math.log2(low / 1000)
            bands.append((first, min(end, FFT_SIZE // 2), tilt))
        return bands

    def _spectrum(self) -> list[float]:
        """The strength of each bar, from 0 to 1, for the latest sound."""
        samples = list(self._samples)
        real = [samples[i] * self._window[i] for i in self._reversed]
        imaginary = [0.0] * FFT_SIZE
        twiddles = self._twiddles
        size = 2
        while size <= FFT_SIZE:
            half = size // 2
            step = FFT_SIZE // size
            for start in range(0, FFT_SIZE, size):
                k = 0
                for low in range(start, start + half):
                    high = low + half
                    w_real, w_imaginary = twiddles[k]
                    t_real = real[high] * w_real - imaginary[high] * w_imaginary
                    t_imaginary = real[high] * w_imaginary + imaginary[high] * w_real
                    real[high] = real[low] - t_real
                    imaginary[high] = imaginary[low] - t_imaginary
                    real[low] += t_real
                    imaginary[low] += t_imaginary
                    k += step
            size *= 2

        levels = []
        for first, end, tilt in self._bands:
            power = max(real[b] * real[b] + imaginary[b] * imaginary[b] for b in range(first, end))
            amplitude = 4 * math.sqrt(power) / FFT_SIZE  # 4: two sides, and the window's loss
            decibels = 20 * math.log10(amplitude) + tilt if amplitude > 0 else QUIET_DB
            levels.append(max(0.0, min(1.0, (decibels - QUIET_DB) / (LOUD_DB - QUIET_DB))))
        return levels

    def _next_frame(self) -> None:
        recent = time.monotonic() - self._last_audio < AUDIO_GONE_SECONDS
        if recent and len(self._samples) == FFT_SIZE and self._bands:
            targets = self._spectrum()
        else:
            targets = [0.0] * self._style.bars
        fall = FALL_PER_FRAME[self._style.fall_speed]
        moved = False
        for bar, target in enumerate(targets[: len(self._levels)]):
            level = max(target, self._levels[bar] * fall)
            if level < 0.003:
                level = 0.0
            moved = moved or level != self._levels[bar]
            self._levels[bar] = level
        if moved:
            self._widget.update()

    def _paint(self) -> None:
        widget = self._widget
        width, height = widget.width(), widget.height()
        style = self._style
        bar_count = len(self._levels)
        accent = QColor(theme.active_highlight())
        opacity = round(255 * style.opacity / 100)
        from_cover = style.colours == COVER and self._cover_colours is not None
        gradient = GRADIENT if style.colours in (HIGHLIGHT, RAINBOW, COVER) else FLAG_GRADIENT
        if from_cover:
            colours = [self._cover_colours.bars] * bar_count
        else:  # (without a cover, "From the cover" uses the highlight colour)
            colours = bar_colours(style.colours, bar_count, accent)
        brushes = [self._bar_brush(color, height, opacity, gradient) for color in colours]
        painter = QPainter(widget)
        if self._corner_radius:  # inside the widget's 1px outline and rounded corners
            inside = QPainterPath()
            inside.addRoundedRect(
                QRectF(widget.rect()).adjusted(1, 1, -1, -1),
                self._corner_radius,
                self._corner_radius,
            )
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipPath(inside)
        if self._shows_background() and width > 0 and height > 0:
            painter.drawPixmap(0, 0, self._background_for(width, height))
        elif from_cover:  # the cover's darker colour behind the bars
            painter.fillRect(widget.rect(), self._cover_colours.background)
        bar_width = width / bar_count
        gap = style.gap
        for bar, level in enumerate(self._levels):
            bar_height = level * height
            if bar_height >= 1:
                rect = QRectF(
                    bar * bar_width + gap / 2,
                    height - bar_height,
                    max(1.0, bar_width - gap),
                    bar_height,
                )
                painter.fillRect(rect, brushes[bar])
        painter.end()

    @staticmethod
    def _bar_brush(
        color: QColor, height: int, opacity: int, shading: tuple[int, int]
    ) -> QBrush:
        """A bar's fill: darker at the bottom, lighter at the top, see-through."""
        color = QColor(color)
        color.setAlpha(opacity)
        gradient = QLinearGradient(QPointF(0, height), QPointF(0, 0))
        gradient.setColorAt(0.0, color.darker(shading[0]))
        gradient.setColorAt(1.0, color.lighter(shading[1]))
        return QBrush(gradient)


class LyricsView(QObject):
    """Shows lyrics in a list; timed lyrics follow the song."""

    def __init__(self, view: QListWidget) -> None:
        super().__init__(view)
        self._view = view
        self._times: list[int] = []
        self._current = -1
        self._leftover_wheel = 0  # touchpads report fractions of a notch
        self._scroll_hold = QTimer(self)
        self._scroll_hold.setSingleShot(True)
        self._scroll_hold.setInterval(SCROLL_HOLD_MS)
        self._scroll_hold.timeout.connect(self._scroll_to_current)
        # "No lyrics" shows for a moment, then fades away (each time the tab is shown).
        self._style = LyricsSettings()
        self._no_lyrics = False
        self._fade_wait = QTimer(self)
        self._fade_wait.setSingleShot(True)
        self._fade_wait.setInterval(round(self._style.no_lyrics_hold * 1000))
        self._fade = QVariantAnimation(self)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.setDuration(round(self._style.no_lyrics_fade * 1000))
        self._fade.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade.valueChanged.connect(self._show_no_lyrics)
        self._fade_wait.timeout.connect(self._fade.start)
        self._base_font = QFont(view.font())  # the size set in player.ui
        self._font = QFont(self._base_font)  # the size used, perhaps grown with the window
        self._scale = 1.0
        self.set_lyrics("")
        view.installEventFilter(self)
        view.viewport().installEventFilter(self)  # wheel events arrive there

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Wheel:
            self._leftover_wheel += event.angleDelta().y()
            notches = int(self._leftover_wheel / WHEEL_NOTCH)
            self._leftover_wheel -= notches * WHEEL_NOTCH
            if notches:
                self._scroll_lines(-notches)  # a positive angle means scrolling up
            event.accept()
            return True
        if event.type() in (QEvent.Type.Show, QEvent.Type.Resize):
            if watched is self._view:
                self._apply_scale()
            # Wait for the view to get its final size, or the centring is off.
            QTimer.singleShot(0, self._recentre)
        if event.type() == QEvent.Type.Show and watched is self._view and self._no_lyrics:
            self._start_no_lyrics_fade()  # say it again, then clear the way
        return False

    def _start_no_lyrics_fade(self) -> None:
        self._fade.stop()
        self._show_no_lyrics(1.0)
        if self._style.show_no_lyrics and self._style.fade_no_lyrics:
            self._fade_wait.start()

    def _stop_no_lyrics_fade(self) -> None:
        self._fade_wait.stop()
        self._fade.stop()

    def _show_no_lyrics(self, opacity: float) -> None:
        """Draw "No lyrics" this opaque (1 is fully, 0 not at all)."""
        if not self._no_lyrics or self._view.count() == 0:
            return
        self._view.item(0).setHidden(not self._style.show_no_lyrics)
        color = QColor(theme.colour(theme.TEXT))
        color.setAlphaF(color.alphaF() * opacity)
        self._view.item(0).setForeground(QBrush(color))

    def set_style(self, style: LyricsSettings) -> None:
        """The text shadow, and whether and how "No lyrics" fades away."""
        self._style = style
        set_text_shadow(self._view.viewport(), style)
        self._fade_wait.setInterval(round(style.no_lyrics_hold * 1000))
        self._fade.setDuration(max(1, round(style.no_lyrics_fade * 1000)))
        self._apply_scale()
        if self._no_lyrics:
            self._stop_no_lyrics_fade()
            self._start_no_lyrics_fade()  # so the change can be seen at once

    def _apply_scale(self) -> None:
        """With "Grow with the window", size the lyrics to the view: bigger than set
        in player.ui when the view is bigger than a normal window, never smaller."""
        view = self._view
        scale = 1.0
        if self._style.scale_with_window:
            width, height = SCALE_REFERENCE
            scale = max(1.0, min(MAX_LYRICS_SCALE, view.width() / width, view.height() / height))
        if abs(scale - self._scale) < 0.03:
            return  # a small change is not worth laying the lyrics out again
        self._scale = scale
        self._font = QFont(self._base_font)
        if self._base_font.pointSizeF() > 0:
            self._font.setPointSizeF(self._base_font.pointSizeF() * scale)
        for row in range(view.count()):
            view.item(row).setFont(self._font)
        if self._times:
            for line in range(view.count() - 2):  # between the two gap rows
                self._style_line(line, line == self._current)
        QTimer.singleShot(0, self._recentre)

    def _scroll_lines(self, lines: int) -> None:
        """Move this many lyric lines down (or up, if negative)."""
        view = self._view
        area = view.viewport().rect()
        if self._times:  # the line being read is in the middle, between the two gap rows
            anchor = view.indexAt(area.center()).row()
            first, last = 1, view.count() - 2
            hint = QListWidget.ScrollHint.PositionAtCenter
        else:
            anchor = view.indexAt(QPoint(area.center().x(), 0)).row()
            first, last = 0, view.count() - 1
            hint = QListWidget.ScrollHint.PositionAtTop
        row = max(first, min(last, max(anchor, first) + lines))
        view.scrollToItem(view.item(row), hint)
        self._scroll_hold.start()

    def set_lyrics(self, text: str) -> None:
        """Show these lyrics (see lyrics.py). Empty text says there are none."""
        view = self._view
        self._scroll_hold.stop()  # a new song starts following at once
        self._stop_no_lyrics_fade()
        view.clear()
        lyrics = parse_lyrics(text)
        self._times = lyrics.times_ms
        self._current = -1
        self.text = "\n".join(lyrics.texts)  # for copying; empty if there are no lyrics
        lines = lyrics.texts or [NO_LYRICS_TEXT]
        if self._times:
            view.addItem(self._make_gap())
        for line in lines:
            item = QListWidgetItem(line or (INSTRUMENTAL_LINE if self._times else " "))
            item.setFont(self._font)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            view.addItem(item)
        if self._times:
            view.addItem(self._make_gap())
            self._size_gaps()
            for line in range(len(lines)):
                self._style_line(line, False)
        view.scrollToTop()
        self._no_lyrics = not lyrics.texts
        if self._no_lyrics:
            self._start_no_lyrics_fade()

    def clear(self) -> None:
        """Show nothing at all, not even "No lyrics" (for a podcast episode)."""
        self._scroll_hold.stop()
        self._stop_no_lyrics_fade()
        self._view.clear()
        self._times = []
        self._current = -1
        self._no_lyrics = False
        self.text = ""

    def set_position(self, position_ms: int) -> None:
        """Move the highlight to the line being sung."""
        if not self._times:
            return
        line = bisect.bisect_right(self._times, position_ms) - 1
        if line == self._current:
            return
        if self._current >= 0:
            self._style_line(self._current, False)
        self._current = line
        if line >= 0:
            self._style_line(line, True)
            self._scroll_to_current()
        elif not self._scroll_hold.isActive():
            self._view.scrollToTop()

    @staticmethod
    def _make_gap() -> QListWidgetItem:
        """An empty row that gives the first or last line room to reach the centre."""
        item = QListWidgetItem()
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        return item

    def _size_gaps(self) -> None:
        if self._times:
            view = self._view
            gap = QSize(0, view.height() // 2)
            view.item(0).setSizeHint(gap)
            view.item(view.count() - 1).setSizeHint(gap)

    def _recentre(self) -> None:
        self._size_gaps()
        self._scroll_to_current()

    def _line_item(self, line: int) -> QListWidgetItem:
        return self._view.item(line + 1)  # timed lyrics start after the top gap

    def _scroll_to_current(self) -> None:
        view = self._view
        if not self._times or not view.isVisible() or self._scroll_hold.isActive():
            return
        # The current line is taller than the others; centre it at its new size.
        view.doItemsLayout()
        if self._current >= 0:
            view.scrollToItem(
                self._line_item(self._current), QListWidget.ScrollHint.PositionAtCenter
            )

    def _style_line(self, line: int, current: bool) -> None:
        item = self._line_item(line)
        font = QFont(self._font)
        font.setBold(current)
        if current and font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() + CURRENT_LINE_EXTRA_POINTS * self._scale)
        item.setFont(font)
        color = QColor(theme.colour(theme.TEXT))
        if not current:
            color.setAlpha(DIM_ALPHA)
        item.setForeground(QBrush(color))
