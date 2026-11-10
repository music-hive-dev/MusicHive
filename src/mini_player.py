"""The mini player: a small window with the basic controls that stays on top."""

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSettings, QStandardPaths, Qt, Slot
from PySide6.QtDBus import QDBusConnection, QDBusMessage
from PySide6.QtGui import QAction, QGuiApplication, QIcon
from PySide6.QtWidgets import QSlider, QWidget

from src.elided_label import ElidedLabel

POSITION_KEY = "miniPlayerPosition"  # saved as "x,y"
SCRIPT_NUMBER_KEY = "miniPlayerScriptNumber"
KWIN_SCRIPT = Path(__file__).parent.parent / "assets" / "mini_player_kwin.js"
KWIN_SCRIPT_NAME = "musichive_mini_player"
DBUS_SERVICE = "org.musichive.MiniPlayer"  # also in the script


class _PositionReceiver(QObject):
    """KWin calls this over D-Bus when the window has been dragged somewhere."""

    def __init__(self, parent: QObject, save: Callable[[str], None]) -> None:
        super().__init__(parent)
        self._save = save

    @Slot(str)
    def savePosition(self, position: str) -> None:
        self._save(position)


class MiniPlayer(QObject):
    """Shows what the main window shows, in the widgets of mini_player.ui.

    The window has no parent, so that it stays open while the main window is
    hidden or minimized. That also means it must be given the stylesheet itself.
    """

    def __init__(self, window: QWidget, app_style_sheet: str, settings: QSettings) -> None:
        super().__init__(window)
        self.window = window
        self.slider = window.miniProgressSlider
        self._settings = settings
        self._action: QAction | None = None
        self._nothing_playing = window.miniTitle.text()
        self._title = ElidedLabel(window.miniTitle, max_lines=1)
        self._artist = ElidedLabel(window.miniArtist, max_lines=1)
        self._wayland = QGuiApplication.platformName().startswith("wayland")

        window.setStyleSheet(f"{app_style_sheet}\n{window.styleSheet()}")
        window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        window.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        window.setWindowFlag(Qt.WindowType.Tool)  # keeps it out of the taskbar on X11
        # The corners outside the window's rounded edge must show what is behind it.
        window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # An open mini player must not keep the program running after the main window closes.
        window.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.show_track("", "")

        if self._wayland:
            self._receiver = _PositionReceiver(self, self._save_position)
            bus = QDBusConnection.sessionBus()
            bus.registerService(DBUS_SERVICE)
            bus.registerObject("/", self._receiver, QDBusConnection.RegisterOption.ExportAllSlots)

    def connect_action(self, action: QAction) -> None:
        """Let this checkable action open and close the window."""
        self._action = action
        action.toggled.connect(self.window.setVisible)
        self.window.installEventFilter(self)

    def follow(self, source: QSlider) -> None:
        """Keep the seek bar at the same place as this slider."""
        source.rangeChanged.connect(self.slider.setRange)
        source.valueChanged.connect(self._show_position)
        self.slider.setRange(source.minimum(), source.maximum())
        self.slider.setValue(source.value())

    def show_track(self, title: str, artist: str) -> None:
        self._title.set_text(title or self._nothing_playing)
        self._artist.set_text(artist)

    def show_playing(self, icon: QIcon, tooltip: str) -> None:
        self.window.miniPlayButton.setIcon(icon)
        self.window.miniPlayButton.setToolTip(tooltip)

    def show_volume(self, icon: QIcon, tooltip: str) -> None:
        self.window.miniMuteButton.setIcon(icon)
        self.window.miniMuteButton.setToolTip(tooltip)

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Show:
            if self._wayland:
                self._run_kwin_script()
            elif (position := self._saved_position()) is not None:
                self.window.move(*position)
        elif kind == QEvent.Type.Move and not self._wayland and self.window.isVisible():
            self._save_position(f"{self.window.x()},{self.window.y()}")
        elif kind == QEvent.Type.Close and event.spontaneous():
            # Closed by the desktop (Alt+F4). Not when the program quits, so that
            # it is still remembered as open for the next start.
            self._action.setChecked(False)
        elif kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            # Without a title bar, the window is moved by dragging its background.
            self.window.windowHandle().startSystemMove()
            return True
        return False

    def _show_position(self, position: int) -> None:
        if not self.slider.isSliderDown():  # the user is dragging; don't fight them
            self.slider.setValue(position)

    def _save_position(self, position: str) -> None:
        self._settings.setValue(POSITION_KEY, position)

    def _saved_position(self) -> tuple[int, int] | None:
        try:
            x, y = map(int, self._settings.value(POSITION_KEY, "", type=str).split(","))
        except ValueError:
            return None
        return x, y

    def _run_kwin_script(self) -> None:
        """Have KDE's window manager keep the window on top and put it back in its place.

        Other desktops don't have this service and ignore the messages.
        """
        position = self._saved_position()
        saved = f"{{x: {position[0]}, y: {position[1]}}}" if position else "null"
        text = KWIN_SCRIPT.read_text(encoding="utf-8").replace("__SAVED_POSITION__", saved)
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation)
        script = Path(folder) / f"{KWIN_SCRIPT_NAME}.js"
        script.write_text(text, encoding="utf-8")

        # Every run gets a new name: KWin still knows an unloaded script for a
        # moment, and refuses to load another one with the same name.
        number = self._settings.value(SCRIPT_NUMBER_KEY, 0, type=int)
        self._settings.setValue(SCRIPT_NUMBER_KEY, number + 1)
        previous_name = f"{KWIN_SCRIPT_NAME}{number or ''}"
        name = f"{KWIN_SCRIPT_NAME}{number + 1}"

        bus = QDBusConnection.sessionBus()
        for method, arguments in (
            ("unloadScript", [previous_name]),
            ("loadScript", [str(script), name]),
            ("start", []),
        ):
            message = QDBusMessage.createMethodCall(
                "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", method
            )
            message.setArguments(arguments)
            bus.send(message)
