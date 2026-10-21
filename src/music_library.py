"""Discovering supported audio files in a folder."""

import os
from pathlib import Path

# Extensions we offer to play. Whether each one actually plays depends on
# the codecs in the Qt Multimedia backend.
SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".ogg", ".oga", ".wav"}


def find_audio_files(directory: str | Path, recursive: bool = True) -> list[Path]:
    """Return supported audio files inside `directory`.

    With recursive=True (the default) subfolders are searched too; with
    recursive=False only the folder's immediate contents are scanned.

    - Results are sorted by folder and file name, case-insensitively, so
      files from the same folder stay together.
    - Hidden files and folders (names starting with ".") are skipped.
    - Symbolic links to folders are not followed, which avoids loops.
    - Subfolders that cannot be read are skipped silently.
    - Returns an empty list if `directory` does not exist.
    - Raises OSError if the top-level folder itself cannot be read.
    """
    root = Path(directory)
    if not root.is_dir():
        return []

    def on_error(error: OSError) -> None:
        # Skip unreadable subfolders, but report an unreadable top folder.
        if error.filename is not None and Path(error.filename) == root:
            raise error

    files: list[Path] = []
    for current, subdirs, filenames in os.walk(root, onerror=on_error):
        subdirs[:] = [name for name in subdirs if not name.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(Path(current) / name)
        if not recursive:
            break

    return sorted(
        files,
        key=lambda path: [part.lower() for part in path.relative_to(root).parts],
    )