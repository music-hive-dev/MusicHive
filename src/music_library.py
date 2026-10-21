"""Discovering supported audio files in a folder."""

from pathlib import Path

# Extensions we offer to play. Whether each one actually plays depends on
# the codecs in the Qt Multimedia backend (we test this in Stage 5).
SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".ogg", ".oga", ".wav"}


def find_audio_files(directory: str | Path) -> list[Path]:
    """Return supported audio files directly inside `directory`, sorted by name.

    Only the folder's immediate contents are scanned (no subfolders).
    Returns an empty list if the folder does not exist.
    Raises OSError (e.g. PermissionError) if the folder cannot be read.
    """
    folder = Path(directory)
    if not folder.is_dir():
        return []

    files = [
        entry
        for entry in folder.iterdir()
        if entry.is_file() and entry.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    return sorted(files, key=lambda path: path.name.lower())