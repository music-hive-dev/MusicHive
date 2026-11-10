"""Saving the app's settings to a file, and reading them back."""

from pathlib import Path

from PySide6.QtCore import QSettings

MARKER_KEY = "musicHiveSettingsExport"  # tells our files apart from any other .ini file

# Left out of the file: the Last.fm login, this computer's data folder,
# and links that only matter for a few days.
PRIVATE_KEYS = ("lastfm/username", "lastfm/sessionKey", "dataFolder", "dataFolderPending")
PRIVATE_PREFIXES = ("discordCoverLinks",)


def _is_transferable(key: str) -> bool:
    return key not in PRIVATE_KEYS and not key.startswith(PRIVATE_PREFIXES)


def export_settings(settings: QSettings, path: Path) -> None:
    """Write the settings to `path`. Raises OSError if that fails."""
    target = QSettings(str(path), QSettings.Format.IniFormat)
    target.clear()
    target.setValue(MARKER_KEY, 1)
    for key in settings.allKeys():
        if _is_transferable(key):
            target.setValue(key, settings.value(key))
    target.sync()
    if target.status() != QSettings.Status.NoError:
        raise OSError(f"Could not write {path}")


def import_settings(settings: QSettings, path: Path) -> int:
    """Copy the settings in `path` over the current ones and return how many there were.

    Raises ValueError if `path` is not a file made by export_settings.
    """
    source = QSettings(str(path), QSettings.Format.IniFormat)
    if source.status() != QSettings.Status.NoError or not source.contains(MARKER_KEY):
        raise ValueError("This is not a MusicHive settings file.")
    keys = [key for key in source.allKeys() if key != MARKER_KEY and _is_transferable(key)]
    for key in keys:
        settings.setValue(key, source.value(key))
    settings.sync()
    return len(keys)
