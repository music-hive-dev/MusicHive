"""The Now Playing tab: a spectrum visualiser and lyrics that follow the song."""

import bisect
import math
import time
from array import array
from collections import deque

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter
from PySide6.QtMultimedia import QAudioBuffer, QAudioFormat
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QWidget

from src.lyrics import parse_lyrics

FFT_SIZE = 1024
BAR_COUNT = 12
FRAME_MS = 33  # about 30 pictures a second
FALL_PER_FRAME = 0.86  # bars jump up at once and sink slowly
LOWEST_FREQUENCY = 40
HIGHEST_FREQUENCY = 16_000
QUIET_DB, LOUD_DB = -55.0, 5.0  # what counts as an empty bar and a full one
TILT_DB_PER_OCTAVE = 3.0  # music has less energy up high; this evens the bars out
AUDIO_GONE_SECONDS = 0.25  # without new sound for this long, the bars sink
BAR_GAP = 8  # pixels
BAR_OPACITY = 150  # out of 255; the lyrics are drawn on top of the bars

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


class Visualizer(QObject):
    """Draws a live spectrum in a plain QWidget. It only works while it is visible."""

    def __init__(self, widget: QWidget) -> None:
        super().__init__(widget)
        self._widget = widget
        self._samples: deque[float] = deque(maxlen=FFT_SIZE)
        self._levels = [0.0] * BAR_COUNT
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
            self._timer.start()
        elif kind == QEvent.Type.Hide:
            self._timer.stop()
            self._levels = [0.0] * BAR_COUNT
        elif kind == QEvent.Type.Paint:
            self._paint()
            return True
        return False

    def _make_bands(self) -> list[tuple[int, int, float]]:
        """Which FFT bins each bar covers, spaced as the ear hears pitch."""
        nyquist = self._sample_rate / 2
        top = min(HIGHEST_FREQUENCY, nyquist)
        hz_per_bin = self._sample_rate / FFT_SIZE
        ratio = (top / LOWEST_FREQUENCY) ** (1 / BAR_COUNT)
        bands = []
        for bar in range(BAR_COUNT):
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
            targets = [0.0] * BAR_COUNT
        moved = False
        for bar, target in enumerate(targets):
            level = max(target, self._levels[bar] * FALL_PER_FRAME)
            if level < 0.003:
                level = 0.0
            moved = moved or level != self._levels[bar]
            self._levels[bar] = level
        if moved:
            self._widget.update()

    def _paint(self) -> None:
        widget = self._widget
        width, height = widget.width(), widget.height()
        accent = widget.palette().color(widget.palette().ColorRole.Highlight)
        gradient = QLinearGradient(QPointF(0, height), QPointF(0, 0))
        accent.setAlpha(BAR_OPACITY)
        gradient.setColorAt(0.0, accent.darker(130))
        gradient.setColorAt(1.0, accent.lighter(140))
        brush = QBrush(gradient)
        painter = QPainter(widget)
        bar_width = width / BAR_COUNT
        for bar, level in enumerate(self._levels):
            bar_height = level * height
            if bar_height >= 1:
                rect = QRectF(
                    bar * bar_width + BAR_GAP / 2,
                    height - bar_height,
                    max(1.0, bar_width - BAR_GAP),
                    bar_height,
                )
                painter.fillRect(rect, brush)
        painter.end()


class LyricsView(QObject):
    """Shows lyrics in a list; timed lyrics follow the song."""

    def __init__(self, view: QListWidget) -> None:
        super().__init__(view)
        self._view = view
        self._times: list[int] = []
        self._current = -1
        self.set_lyrics("")
        view.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (QEvent.Type.Show, QEvent.Type.Resize):
            # Wait for the view to get its final size, or the centring is off.
            QTimer.singleShot(0, self._recentre)
        return False

    def set_lyrics(self, text: str) -> None:
        """Show these lyrics (see lyrics.py). Empty text says there are none."""
        view = self._view
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
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            view.addItem(item)
        if self._times:
            view.addItem(self._make_gap())
            self._size_gaps()
            for line in range(len(lines)):
                self._style_line(line, False)
        view.scrollToTop()

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
        else:
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
        if not self._times or not view.isVisible():
            return
        # The current line is taller than the others; centre it at its new size.
        view.doItemsLayout()
        if self._current >= 0:
            view.scrollToItem(
                self._line_item(self._current), QListWidget.ScrollHint.PositionAtCenter
            )

    def _style_line(self, line: int, current: bool) -> None:
        item = self._line_item(line)
        font = QFont(self._view.font())
        font.setBold(current)
        if current and font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() + CURRENT_LINE_EXTRA_POINTS)
        item.setFont(font)
        color = QColor(self._view.palette().color(self._view.palette().ColorRole.Text))
        if not current:
            color.setAlpha(DIM_ALPHA)
        item.setForeground(QBrush(color))
