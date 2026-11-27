"""What a TV shows while MusicHive casts with pictures (cast_screen.ui).

The screen is a window that is never put on the computer's screen. While a
video is cast, it is drawn into a picture FRAMES_PER_SECOND times a second and
handed to the video (cast_video.py). It has its own visualiser and lyrics, fed
the same way as those of the Now Playing tab.
"""

from collections.abc import Callable
from dataclasses import replace

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QWidget

from src.cast_video import FRAMES_PER_SECOND, HEIGHT, WIDTH
from src.now_playing import LyricsView, Visualizer
from src.visualiser_settings import VisualiserSettings

BACKGROUND = QColor("#151416")  # under everything, should the stylesheet draw nothing


class CastScreen(QObject):
    def __init__(self, screen: QWidget, app_style_sheet: str) -> None:
        super().__init__(screen)
        self.screen = screen
        # It has no parent, so it is given the main window's stylesheet, then its own.
        screen.setStyleSheet(app_style_sheet + "\n" + screen.styleSheet())
        screen.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        screen.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        screen.setFixedSize(WIDTH, HEIGHT)
        self.visualizer = Visualizer(screen.castVisualizer)
        self.lyrics = LyricsView(screen.castLyrics)
        screen.castAddedBy.hide()
        screen.castParty.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)  # its card colour
        screen.castParty.hide()  # until Party Mode shows its QR code on the TV
        self._deliver: Callable[[QImage], None] | None = None
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(round(1000 / FRAMES_PER_SECOND))
        self._timer.timeout.connect(self._draw)

    def start(self, deliver: Callable[[QImage], None]) -> None:
        """Draw the screen from now on, handing each picture to `deliver`."""
        self._deliver = deliver
        self.screen.show()  # not on the computer's screen, but its parts come alive
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._deliver = None
        self.screen.hide()  # the visualiser rests

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    def set_details(self, title: str, artist: str, added_by: str = "") -> None:
        self.screen.castTitle.setText(title)
        self.screen.castArtist.setText(artist)
        self.screen.castArtist.setVisible(bool(artist))
        self.screen.castAddedBy.setText(added_by)
        self.screen.castAddedBy.setVisible(bool(added_by))

    def set_party(self, code: QImage | None, address: str = "") -> None:
        """Show the Party Mode QR code in the corner (None hides it)."""
        screen = self.screen
        screen.castParty.setVisible(code is not None)
        if code is not None:
            screen.castPartyQr.setPixmap(QPixmap.fromImage(code))
            screen.castPartyAddress.setText(address)

    def set_visualiser_style(self, style: VisualiserSettings) -> None:
        # The TV gets FRAMES_PER_SECOND pictures, so more bar frames would be lost.
        self.visualizer.set_style(replace(style, smooth=False))

    def _draw(self) -> None:
        if self._deliver is None:
            return
        image = QImage(WIDTH, HEIGHT, QImage.Format.Format_RGB32)
        image.fill(BACKGROUND)
        self.screen.render(image)
        self._deliver(image)
