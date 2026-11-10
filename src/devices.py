"""Finding the storage devices that are plugged in, such as an iPod or a USB drive."""

import getpass
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QRunnable, QStorageInfo, QThreadPool, Signal
from PySide6.QtWidgets import QWidget

from src.formatting import format_size

BATTERY_NOT_REPORTED = "Not reported by the device"
SCSI_VENDOR_LENGTH = 8  # a name that fills this field runs on into the model field
BATTERY_CHECK_SECONDS = 60
BATTERY_RETRY_SECONDS = 300  # for a device that has not been answering


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


@dataclass(frozen=True)
class DeviceDetails:
    """What stays the same while a device is plugged in. "" means it is not known."""

    model: str
    firmware: str
    storage: str
    connection: str
    serial: str
    file_system: str
    usb_folder: Path | None  # the device's folder in /sys, where its battery is found


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:  # a value the device has not reported reads as an error
        return ""


def _read_rockbox_info(folder: str) -> dict[str, str]:
    info = {}
    for line in _read_text(Path(folder) / ".rockbox" / "rockbox-info.txt").splitlines():
        key, _, value = line.partition(":")
        info[key.strip()] = value.strip()
    return info


def _usb_connection(usb_folder: Path) -> str:
    try:
        version = f"USB {float(_read_text(usb_folder / 'version')):g}"
    except ValueError:
        version = "USB"
    speed = _read_text(usb_folder / "speed")
    return f"{version}, {speed} Mbit/s" if speed else version


def read_device_details(folder: str) -> DeviceDetails:
    storage = QStorageInfo(folder)
    node = bytes(storage.device()).decode().rsplit("/", 1)[-1]
    block = Path("/sys/class/block", node).resolve()
    parents = [block, *block.parents]
    drive = next((path for path in parents if (path / "model").is_file()), None)
    usb = next((path for path in parents if (path / "idVendor").is_file()), None)

    hardware = ""
    if drive is not None:
        vendor, model = _read_text(drive / "vendor"), _read_text(drive / "model")
        hardware = f"{vendor}{'' if len(vendor) == SCSI_VENDOR_LENGTH else ' '}{model}".strip()
    product = ""
    if usb is not None:
        product = f"{_read_text(usb / 'manufacturer')} {_read_text(usb / 'product')}".strip()
    rockbox = _read_rockbox_info(folder)
    version = rockbox.get("Version", "")
    return DeviceDetails(
        model=rockbox.get("Target") or product or hardware,
        firmware=f"Rockbox {version}" if version else "",
        storage=hardware,
        connection=_usb_connection(usb) if usb is not None else "",
        serial=_read_text(usb / "serial") if usb is not None else "",
        file_system=bytes(storage.fileSystemType()).decode(),
        usb_folder=usb,
    )


def read_battery(usb_folder: Path | None) -> str:
    """The device's battery as text; "" if it does not have one the system can see.

    The system asks the device itself, which takes seconds if the device does not
    answer. So this must never run on the interface's thread.
    """
    if usb_folder is None:
        return ""
    for supply in Path("/sys/class/power_supply").glob("*"):
        if str((supply / "device").resolve()).startswith(f"{usb_folder}/"):
            capacity = _read_text(supply / "capacity")
            if not capacity:
                return BATTERY_NOT_REPORTED
            status = _read_text(supply / "status")
            return f"{capacity}% ({status})" if status not in ("", "Unknown") else f"{capacity}%"
    return ""


class _BatterySignals(QObject):
    read = Signal(str, str)  # the device's folder, its battery as text


class _BatteryTask(QRunnable):
    def __init__(self, folder: str, usb_folder: Path | None, signals: _BatterySignals) -> None:
        super().__init__()
        self._folder = folder
        self._usb_folder = usb_folder
        self._signals = signals

    def run(self) -> None:
        self._signals.read.emit(self._folder, read_battery(self._usb_folder))


class DevicesTab:
    """Connects the Devices tab of player.ui."""

    def __init__(self, window: QWidget, on_selected: Callable[[str, str], None]) -> None:
        self._window = window
        self._on_selected = on_selected  # told the device's folder and a lasting id
        self._details: DeviceDetails | None = None
        self._battery_pool = QThreadPool(window)
        self._battery_pool.setMaxThreadCount(1)
        self._battery_signals = _BatterySignals(window)
        self._battery_signals.read.connect(self._on_battery_read)
        self._reading_battery = False
        self._next_battery_check = 0.0
        window.deviceCombo.currentIndexChanged.connect(lambda _index: self._show_selected())
        self._show_selected()

    def set_devices(self, devices: list[tuple[str, str]]) -> None:
        """Show these plugged-in devices, staying on the selected one if it is still here."""
        combo = self._window.deviceCombo
        selected = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for name, folder in devices:
            combo.addItem(name, folder)
        index = combo.findData(selected) if selected else -1
        combo.setCurrentIndex(index if index >= 0 else (0 if devices else -1))
        combo.blockSignals(False)
        self._show_selected()

    def select(self, folder: str) -> None:
        combo = self._window.deviceCombo
        index = combo.findData(folder)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _set_row(self, name: str, text: str) -> None:
        window = self._window
        getattr(window, f"deviceInfo{name}").setText(text)
        getattr(window, f"deviceInfo{name}").setVisible(bool(text))
        getattr(window, f"deviceInfo{name}Label").setVisible(bool(text))

    def _show_selected(self) -> None:
        window = self._window
        folder = window.deviceCombo.currentData()
        window.deviceInfoPanel.setVisible(bool(folder))
        if not folder:
            self._details = None
            self._on_selected("", "")
            return
        self._details = details = read_device_details(folder)
        self._on_selected(folder, details.serial or window.deviceCombo.currentText())
        self._next_battery_check = 0.0  # this device's battery has not been read yet
        self._set_row("Battery", "")
        self._set_row("Name", window.deviceCombo.currentText())
        self._set_row("Model", details.model)
        self._set_row("Firmware", details.firmware)
        self._set_row("FileSystem", details.file_system)
        self._set_row("Storage", details.storage)
        self._set_row("Connection", details.connection)
        self._set_row("Serial", details.serial)
        self._set_row("Location", folder)
        self.refresh_levels()

    def refresh_levels(self) -> None:
        """Update what changes while a device is plugged in: its battery and free space."""
        window = self._window
        folder = window.deviceCombo.currentData()
        if not folder or self._details is None:
            return
        self._start_battery_read(folder)
        storage = QStorageInfo(folder)
        total, free = storage.bytesTotal(), storage.bytesAvailable()
        known = total > 0
        self._set_row("Capacity", format_size(total) if known else "")
        self._set_row("Free", format_size(free) if known else "")
        window.deviceSpaceBar.setVisible(known)
        window.deviceInfoSpaceLabel.setVisible(known)
        if known:
            window.deviceSpaceBar.setValue(round((total - free) * 100 / total))

    def _start_battery_read(self, folder: str) -> None:
        if not self._reading_battery and time.monotonic() >= self._next_battery_check:
            self._reading_battery = True
            task = _BatteryTask(folder, self._details.usb_folder, self._battery_signals)
            self._battery_pool.start(task)

    def _on_battery_read(self, folder: str, battery: str) -> None:
        self._reading_battery = False
        if folder != self._window.deviceCombo.currentData():
            self.refresh_levels()  # another device was selected in the meantime
            return
        self._set_row("Battery", battery)
        wait = BATTERY_RETRY_SECONDS if battery == BATTERY_NOT_REPORTED else BATTERY_CHECK_SECONDS
        self._next_battery_check = time.monotonic() + wait
