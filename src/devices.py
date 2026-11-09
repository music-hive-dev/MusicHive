"""Finding the storage devices that are plugged in, such as an iPod or a USB drive."""

import getpass
from collections.abc import Callable

from PySide6.QtCore import QObject, QProcess, QStorageInfo, Signal


def connected_devices() -> list[tuple[str, str]]:
    """Every plugged-in storage device as (name, folder), sorted by name.

    The desktop mounts plugged-in devices under these folders. Internal drives
    live elsewhere, so they are not listed.
    """
    user = getpass.getuser()
    mount_folders = (f"/run/media/{user}/", f"/media/{user}/")
    devices = [
        (volume.displayName(), volume.rootPath())
        for volume in QStorageInfo.mountedVolumes()
        if volume.isValid() and volume.isReady() and volume.rootPath().startswith(mount_folders)
    ]
    return sorted(devices, key=lambda device: device[0].casefold())


def open_in_dolphin(folder: str) -> bool:
    """Open a folder in Dolphin. Returns False if Dolphin could not be started."""
    result = QProcess.startDetached("dolphin", [folder])
    return result[0] if isinstance(result, tuple) else bool(result)


class DeviceEjector(QObject):
    """Safely removes a device: unmounts it, which writes out everything, then powers it off.

    This runs in the background, because unmounting can take a while after big copies.
    """

    finished = Signal(str)  # what to tell the user

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._name = ""
        self._node = ""
        self._busy = False

    def eject(self, name: str, folder: str) -> None:
        if self._busy:
            self.finished.emit(f"Still removing {self._name}")
            return
        self._busy = True
        self._name = name
        self._node = bytes(QStorageInfo(folder).device()).decode()
        self._run(["unmount", "--block-device", self._node], self._on_unmounted)

    def _run(self, arguments: list[str], then: Callable[[bool, str], None]) -> None:
        process = QProcess(self)

        def on_finished(exit_code: int, exit_status: QProcess.ExitStatus) -> None:
            error = bytes(process.readAllStandardError()).decode(errors="replace").strip()
            process.deleteLater()
            then(exit_status == QProcess.ExitStatus.NormalExit and exit_code == 0, error)

        def on_error(error: QProcess.ProcessError) -> None:
            if error == QProcess.ProcessError.FailedToStart:  # finished is not sent then
                process.deleteLater()
                then(False, "udisksctl could not be started")

        process.finished.connect(on_finished)
        process.errorOccurred.connect(on_error)
        process.start("udisksctl", arguments)

    def _on_unmounted(self, succeeded: bool, error: str) -> None:
        if succeeded:
            self._run(["power-off", "--block-device", self._node], self._on_powered_off)
        else:
            self._finish(f"Could not safely remove {self._name}: {error}")

    def _on_powered_off(self, succeeded: bool, _error: str) -> None:
        # Once unmounted, everything is written out, so unplugging is safe either way.
        self._finish(f"{self._name} can now be unplugged")

    def _finish(self, message: str) -> None:
        self._busy = False
        self.finished.emit(message)
