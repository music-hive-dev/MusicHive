"""Reading and writing .m3u and .m3u8 playlist files. No GUI code lives here."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]+://")


@dataclass
class M3uTrack:
    path: Path
    seconds: int  # -1 if unknown
    title: str  # what a player shows before it has read the file


def write_m3u(file: Path, tracks: list[M3uTrack]) -> None:
    """Write a playlist. Raises OSError on failure.

    Songs inside the playlist's own folder get relative paths, so the folder can
    be moved as a whole; the others get absolute paths. Both file types are
    UTF-8, because that is how file names are stored on Linux.
    """
    lines = ["#EXTM3U"]
    for track in tracks:
        title = " ".join(track.title.split())
        path = track.path
        if path.is_relative_to(file.parent):
            path = path.relative_to(file.parent)
        lines.append(f"#EXTINF:{track.seconds},{title}")
        lines.append(str(path))
    file.write_bytes(("\n".join(lines) + "\n").encode("utf-8", "surrogateescape"))


def read_m3u(file: Path) -> list[Path]:
    """The local files a playlist lists, in order. Raises OSError on failure.

    Web addresses are skipped. Relative paths are relative to the playlist.
    """
    raw = file.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")  # older players wrote .m3u in a legacy encoding

    paths: list[Path] = []
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("file:"):
            entry = unquote(urlsplit(entry).path)
        elif _URL.match(entry):
            continue
        elif "\\" in entry and "/" not in entry:
            entry = entry.replace("\\", "/")  # written on Windows
        paths.append(Path(os.path.normpath(file.parent / entry)))
    return paths
