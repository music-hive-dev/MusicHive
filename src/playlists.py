"""Playlists: one small file per playlist, in the library's data folder.

A playlist called "Road trip" is stored as "Road trip.json" (its songs, as
paths relative to the library folder) and, if the user chose one,
"Road trip.png" (its icon), side by side in the library's playlists folder.
An animated icon also has "Road trip.gif"; the .png then holds its first frame.
"""

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

MAX_NAME_LENGTH = 100


@dataclass
class Playlist:
    name: str
    tracks: list[str] = field(default_factory=list)  # relative to the library folder
    locked: bool = False  # a locked playlist cannot gain songs or be rearranged
    description: str = ""
    filter_kind: str = ""  # the tag the song table is filtered by, e.g. "album_artist"
    filter_value: str = ""  # the chosen value of that tag ("" shows every song)
    folder: str = ""  # the folder it is kept in, in the side list ("" means none)
    rules: list[dict] | None = None  # set for an auto-playlist (see auto_playlist.py)
    match_any: bool = False  # an auto-playlist needs any rule to match, not all of them
    no_duplicates: bool = False  # an auto-playlist keeps one copy of the same song
    limit: dict | None = None  # how much of an auto-playlist to keep (see auto_playlist.py)

    @property
    def is_auto(self) -> bool:
        return self.rules is not None

    @property
    def read_only(self) -> bool:
        """True if songs cannot be added to it or rearranged."""
        return self.locked or self.is_auto


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
        return self._check_name(name, self.names(), "playlist", ignore)

    def check_folder_name(self, name: str, ignore: str | None = None) -> str | None:
        """Why `name` cannot be used for a folder, or None if it can."""
        return self._check_name(name, self.folders(), "folder", ignore)

    @staticmethod
    def _check_name(name: str, existing: list[str], noun: str, ignore: str | None) -> str | None:
        name = name.strip()
        if not name:
            return "The name cannot be empty."
        if len(name) > MAX_NAME_LENGTH:
            return f"The name can be at most {MAX_NAME_LENGTH} characters long."
        if "/" in name or "\0" in name or name.startswith("."):
            return "The name cannot contain a slash or start with a dot."
        taken = {other.casefold() for other in existing if other != ignore}
        if name.casefold() in taken:
            return f"A {noun} with that name already exists."
        return None

    def _file(self, name: str) -> Path:
        return self.folder / f"{name}.json"

    def icon_file(self, name: str) -> Path:
        return self.folder / f"{name}.png"

    def _gif_file(self, name: str) -> Path:
        return self.folder / f"{name}.gif"

    def animated_icon(self, name: str) -> Path | None:
        """The playlist's GIF, if its icon is animated."""
        path = self._gif_file(name)
        return path if path.exists() else None

    def set_animated_icon(self, name: str, source: Path | None) -> None:
        """Copy in a GIF for the playlist's icon (None makes the icon a still one)."""
        target = self._gif_file(name)
        if source is None:
            target.unlink(missing_ok=True)
        elif source != target:
            temp = target.with_suffix(".giftmp")
            shutil.copyfile(source, temp)
            os.replace(temp, target)

    def remove_icon(self, name: str) -> None:
        self.icon_file(name).unlink(missing_ok=True)
        self._gif_file(name).unlink(missing_ok=True)

    # ----- reading and writing -----

    def load(self, name: str) -> Playlist:
        try:
            data = json.loads(self._file(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Playlist(name)
        if not isinstance(data, dict):
            return Playlist(name)
        tracks = [track for track in data.get("tracks", []) if isinstance(track, str)]
        description = data.get("description")
        filter_kind = data.get("filter_kind")
        filter_value = data.get("filter_value")
        folder = data.get("folder")
        rules = data.get("rules")
        limit = data.get("limit")
        return Playlist(
            name,
            tracks,
            data.get("locked") is True,
            description if isinstance(description, str) else "",
            filter_kind if isinstance(filter_kind, str) else "",
            filter_value if isinstance(filter_value, str) else "",
            folder if isinstance(folder, str) else "",
            rules if isinstance(rules, list) else None,
            data.get("match_any") is True,
            data.get("no_duplicates") is True,
            limit if isinstance(limit, dict) else None,
        )

    def save(self, playlist: Playlist) -> None:
        """Write a playlist (this also creates it). Raises OSError on failure."""
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self._file(playlist.name)
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                {
                    "version": 1,
                    "locked": playlist.locked,
                    "description": playlist.description,
                    "filter_kind": playlist.filter_kind,
                    "filter_value": playlist.filter_value,
                    "folder": playlist.folder,
                    "rules": playlist.rules,
                    "match_any": playlist.match_any,
                    "no_duplicates": playlist.no_duplicates,
                    "limit": playlist.limit,
                    "tracks": playlist.tracks,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        os.replace(temp, path)

    def rename(self, old: str, new: str) -> None:
        """Rename a playlist and its icon. Raises OSError on failure."""
        os.replace(self._file(old), self._file(new))
        if self.icon_file(old).exists():
            os.replace(self.icon_file(old), self.icon_file(new))
        if self._gif_file(old).exists():
            os.replace(self._gif_file(old), self._gif_file(new))

    def copy_name(self, name: str) -> str:
        """A free name for a copy: "Name copy", then "Name copy 2" and so on."""
        taken = {other.casefold() for other in self.names()}
        number = 1
        while True:
            suffix = " copy" if number == 1 else f" copy {number}"
            candidate = name[: MAX_NAME_LENGTH - len(suffix)] + suffix
            if candidate.casefold() not in taken:
                return candidate
            number += 1

    def duplicate(self, name: str, new: str) -> None:
        """Copy a playlist and its icon. Raises OSError on failure."""
        playlist = self.load(name)
        playlist.name = new
        self.save(playlist)
        if self.icon_file(name).exists():
            shutil.copyfile(self.icon_file(name), self.icon_file(new))
        self.set_animated_icon(new, self.animated_icon(name))

    def delete(self, name: str) -> None:
        """Delete a playlist and its icon (the songs themselves are untouched)."""
        self._file(name).unlink(missing_ok=True)
        self.remove_icon(name)

    # ----- folders -----

    @property
    def _folders_file(self) -> Path:
        return self.folder / "folders.list"  # not .json: that would be a playlist

    def folders(self) -> list[str]:
        """All folder names, sorted. Folders can be empty, so they have their own file."""
        try:
            data = json.loads(self._folders_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        names = [name for name in data if isinstance(name, str)] if isinstance(data, list) else []
        return sorted(names, key=str.casefold)

    def _save_folders(self, names: list[str]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        temp = self._folders_file.with_suffix(".tmp")
        temp.write_text(json.dumps(names, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temp, self._folders_file)

    def create_folder(self, name: str) -> None:
        self._save_folders([*self.folders(), name])

    def rename_folder(self, old: str, new: str) -> None:
        self._replace_folder(old, new)
        self._save_folders([new if name == old else name for name in self.folders()])

    def delete_folder(self, name: str) -> None:
        """Delete a folder. The playlists in it stay, outside any folder."""
        self._replace_folder(name, "")
        self._save_folders([other for other in self.folders() if other != name])

    def _replace_folder(self, old: str, new: str) -> None:
        for playlist_name in self.names():
            playlist = self.load(playlist_name)
            if playlist.folder == old:
                playlist.folder = new
                self.save(playlist)

    def set_folder(self, name: str, folder: str) -> None:
        playlist = self.load(name)
        playlist.folder = folder
        self.save(playlist)

    # ----- songs -----

    def add_tracks(
        self, name: str, rel_paths: list[str], before: str | None = None
    ) -> int:
        """Add songs, in this order, in front of `before` (None adds them at the end).

        Songs already in the playlist stay where they are. Returns how many were added.
        """
        playlist = self.load(name)
        present = set(playlist.tracks)
        new = [track for track in dict.fromkeys(rel_paths) if track not in present]
        if new:
            at = playlist.tracks.index(before) if before in present else len(playlist.tracks)
            playlist.tracks[at:at] = new
            self.save(playlist)
        return len(new)

    def move_tracks(self, name: str, rel_paths: list[str], before: str | None = None) -> None:
        """Move songs, in this order, in front of `before` (None moves them to the end)."""
        playlist = self.load(name)
        present = set(playlist.tracks)
        moving = [track for track in dict.fromkeys(rel_paths) if track in present]
        moved = set(moving)
        kept = [track for track in playlist.tracks if track not in moved]
        at = kept.index(before) if before in kept else len(kept)
        playlist.tracks = kept[:at] + moving + kept[at:]
        self.save(playlist)

    def remove_tracks(self, name: str, rel_paths: list[str]) -> int:
        """Remove songs. Returns how many were in the playlist."""
        playlist = self.load(name)
        removing = set(rel_paths)
        kept = [track for track in playlist.tracks if track not in removing]
        removed = len(playlist.tracks) - len(kept)
        if removed:
            playlist.tracks = kept
            self.save(playlist)
        return removed

    def remove_track_everywhere(self, rel_path: str) -> None:
        """Remove a song from every playlist (used when its file is deleted)."""
        for name in self.names():
            self.remove_tracks(name, [rel_path])

    def set_description(self, name: str, description: str) -> None:
        playlist = self.load(name)
        playlist.description = description
        self.save(playlist)

    def set_rules(
        self,
        name: str,
        rules: list[dict],
        match_any: bool,
        no_duplicates: bool,
        limit: dict | None,
    ) -> None:
        playlist = self.load(name)
        playlist.rules = rules
        playlist.match_any = match_any
        playlist.no_duplicates = no_duplicates
        playlist.limit = limit
        self.save(playlist)

    def set_filter(self, name: str, kind: str, value: str) -> None:
        playlist = self.load(name)
        playlist.filter_kind = kind
        playlist.filter_value = value
        self.save(playlist)

    def set_locked(self, name: str, locked: bool) -> None:
        playlist = self.load(name)
        playlist.locked = locked
        self.save(playlist)
