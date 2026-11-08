"""Playlists: one small file per playlist, in the library's data folder.

A playlist called "Road trip" is stored as "Road trip.json" (its songs, as
paths relative to the library folder) and, if the user chose one,
"Road trip.png" (its icon), side by side in the library's playlists folder.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

MAX_NAME_LENGTH = 100


@dataclass
class Playlist:
    name: str
    tracks: list[str] = field(default_factory=list)  # relative to the library folder


class PlaylistStore:
    """Reads and writes the playlists in one folder."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    # ----- names and files -----

    def names(self) -> list[str]:
        """All playlist names, sorted."""
        try:
            files = [path for path in self.folder.glob("*.json") if path.is_file()]
        except OSError:
            return []
        return sorted((path.stem for path in files), key=str.casefold)

    def check_name(self, name: str, ignore: str | None = None) -> str | None:
        """Why `name` cannot be used for a playlist, or None if it can.

        `ignore` is a name that does not count as taken (used when renaming).
        """
        name = name.strip()
        if not name:
            return "The name cannot be empty."
        if len(name) > MAX_NAME_LENGTH:
            return f"The name can be at most {MAX_NAME_LENGTH} characters long."
        if "/" in name or "\0" in name or name.startswith("."):
            return "The name cannot contain a slash or start with a dot."
        taken = {other.casefold() for other in self.names() if other != ignore}
        if name.casefold() in taken:
            return "A playlist with that name already exists."
        return None

    def _file(self, name: str) -> Path:
        return self.folder / f"{name}.json"

    def icon_file(self, name: str) -> Path:
        return self.folder / f"{name}.png"

    # ----- reading and writing -----

    def load(self, name: str) -> Playlist:
        try:
            data = json.loads(self._file(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Playlist(name)
        tracks = data.get("tracks", []) if isinstance(data, dict) else []
        return Playlist(name, [track for track in tracks if isinstance(track, str)])

    def save(self, playlist: Playlist) -> None:
        """Write a playlist (this also creates it). Raises OSError on failure."""
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self._file(playlist.name)
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps({"version": 1, "tracks": playlist.tracks}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(temp, path)

    def rename(self, old: str, new: str) -> None:
        """Rename a playlist and its icon. Raises OSError on failure."""
        os.replace(self._file(old), self._file(new))
        if self.icon_file(old).exists():
            os.replace(self.icon_file(old), self.icon_file(new))

    def delete(self, name: str) -> None:
        """Delete a playlist and its icon (the songs themselves are untouched)."""
        self._file(name).unlink(missing_ok=True)
        self.icon_file(name).unlink(missing_ok=True)

    # ----- songs -----

    def add_track(self, name: str, rel_path: str) -> bool:
        """Add a song at the end. Returns False if it was already in the playlist."""
        playlist = self.load(name)
        if rel_path in playlist.tracks:
            return False
        playlist.tracks.append(rel_path)
        self.save(playlist)
        return True

    def remove_track(self, name: str, rel_path: str) -> bool:
        """Remove a song. Returns False if it was not in the playlist."""
        playlist = self.load(name)
        kept = [track for track in playlist.tracks if track != rel_path]
        if len(kept) == len(playlist.tracks):
            return False
        playlist.tracks = kept
        self.save(playlist)
        return True

    def remove_track_everywhere(self, rel_path: str) -> None:
        """Remove a song from every playlist (used when its file is deleted)."""
        for name in self.names():
            self.remove_track(name, rel_path)