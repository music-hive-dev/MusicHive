"""Hiding the main window to the system tray."""

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon


class TrayMinimizer(QObject):
    """While enabled, shows a tray icon and hides the window to it.

    Minimizing hides the window where the system reports it. KDE on Wayland
    does not tell programs about minimizing, so closing the window hides it too.
    """

    def __init__(
        self,
        window: QMainWindow,
        dark_mode_icon: QIcon,
        light_mode_icon: QIcon,
        menu: QMenu,
        restore: Callable[[], None],
    ) -> None:
        super().__init__(window)
        self._window = window
        self._restore = restore
        self._dark_mode_icon = dark_mode_icon
        self._light_mode_icon = light_mode_icon
        self._enabled = False
        self._tray = QSystemTrayIcon(self)
        self._update_icon()
        self._tray.setToolTip("MusicHive")
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_activated)
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._update_icon)
        window.installEventFilter(self)

    def _update_icon(self, _scheme=None) -> None:
        """Use the icon that suits the desktop's dark or light mode."""
        scheme = QGuiApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Unknown:  # judge by the colours in use
            dark = QGuiApplication.palette().window().color().lightness() < 128
        else:
            dark = scheme == Qt.ColorScheme.Dark
        self._tray.setIcon(self._dark_mode_icon if dark else self._light_mode_icon)

    @staticmethod
    def is_available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled and self.is_available()
        self._tray.setVisible(self._enabled)
        # With the window hidden, only an explicit Quit should end the program.
        QApplication.setQuitOnLastWindowClosed(not self._enabled)

    def eventFilter(self, watched, event) -> bool:
        if not self._enabled:
            return False
        if event.type() == QEvent.Type.Close:
            event.ignore()
            self._window.hide()
            return True
        if event.type() == QEvent.Type.WindowStateChange and self._window.isMinimized():
            # Not while the window is still handling the change.
            QTimer.singleShot(0, self._window.hide)
        return False

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self._window.isVisible():
                self._window.hide()
            else:
                self._restore()
