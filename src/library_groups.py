"""Grouping the library's songs (by artist, album, genre, ...) for the side list."""

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Qt
from PySide6.QtGui import QImage

from src.track_list import TrackListModel
from src.track_metadata import TrackInfo

VARIOUS_ARTISTS = "Various Artists"


@dataclass
class TrackGroup:
    """One entry of the side list: its songs, as library rows in playing order."""

    key: str  # identifies the group, also after the library is reloaded
    name: str
    rows: list[int]
    subtitle: str = ""  # the second line in the side list
    artist: str = ""  # albums only
    year: str = ""  # albums only


def songs_text(count: int) -> str:
    return f"{count} song{'' if count == 1 else 's'}"


def _album_order(info: TrackInfo) -> tuple:
    return (info.disc_number or 0, info.track_number or 0, info.title.casefold())


def _browse_order(info: TrackInfo) -> tuple:
    owner = info.album_artist or info.artist
    return (owner.casefold(), info.album.casefold(), _album_order(info))


def _genres(info: TrackInfo) -> list[str]:
    """A song's genres: tags can hold several, as in "Rock, Pop" or "Rock; Pop"."""
    return [name for name in re.split(r"\s*[;,]\s*", info.genre.strip()) if name]


def _decade(info: TrackInfo) -> str:
    """For example "1990s", or "" if the song has no usable year."""
    return f"{info.year[:3]}0s" if len(info.year) == 4 and info.year.isdigit() else ""


def group_by_name(
    infos: Sequence[TrackInfo], names_of: Callable[[TrackInfo], Iterable[str]]
) -> list[TrackGroup]:
    """One group per name, ignoring case. `infos` is indexed by library row.

    `names_of` gives the names a song belongs to; a song without any is left out.
    """
    rows_by_key: dict[str, list[int]] = {}
    names: dict[str, str] = {}
    for row, info in enumerate(infos):
        for name in names_of(info):
            if name:
                key = name.casefold()
                names.setdefault(key, name)
                rows = rows_by_key.setdefault(key, [])
                if not rows or rows[-1] != row:  # "Rock, rock" counts once
                    rows.append(row)

    groups = []
    for key, rows in rows_by_key.items():
        rows.sort(key=lambda row: _browse_order(infos[row]))
        groups.append(TrackGroup(key, names[key], rows, subtitle=songs_text(len(rows))))
    groups.sort(key=lambda group: group.name.casefold())
    return groups


def _album_key(info: TrackInfo, folder: Path) -> str:
    owner = info.album_artist.casefold() or str(folder)
    return f"{info.album.casefold()}\n{owner}"


def group_by_album(infos: Sequence[TrackInfo], folders: Sequence[Path]) -> list[TrackGroup]:
    """One group per album. `infos` and `folders` are indexed by library row.

    Songs belong together if they share the album tag and the album artist.
    Without an album artist they must share a folder instead, so that a
    compilation stays whole and two albums with the same name stay apart.
    """
    rows_by_key: dict[str, list[int]] = {}
    for row, info in enumerate(infos):
        if info.album:
            rows_by_key.setdefault(_album_key(info, folders[row]), []).append(row)

    groups = []
    for key, rows in rows_by_key.items():
        rows.sort(key=lambda row: _album_order(infos[row]))
        artist = _album_artist([infos[row] for row in rows])
        year = next((infos[row].year for row in rows if infos[row].year), "")
        subtitle = " · ".join(part for part in (artist, year) if part)
        groups.append(
            TrackGroup(key, infos[rows[0]].album, rows, subtitle, artist=artist, year=year)
        )
    groups.sort(key=lambda group: (group.name.casefold(), group.artist.casefold()))
    return groups


def _album_artist(infos: list[TrackInfo]) -> str:
    """The album artist tag, else the artist all the songs share."""
    if infos[0].album_artist:
        return infos[0].album_artist
    artists = {info.artist.casefold() for info in infos}
    return infos[0].artist if len(artists) == 1 else VARIOUS_ARTISTS


# The names a song belongs to, for each kind of list that goes by name.
_NAMES_OF: dict[str, Callable[[TrackInfo], list[str]]] = {
    "artist": lambda info: [info.artist],
    "album_artist": lambda info: [info.album_artist or info.artist],
    "genre": _genres,
    "year": lambda info: [info.year],
    "decade": lambda info: [_decade(info)],
}


def _grouper(names_of: Callable[[TrackInfo], list[str]]) -> Callable:
    return lambda infos, _folders: group_by_name(infos, names_of)


# How each kind of list is built, from the songs and their folders.
GROUPERS: dict[str, Callable[[Sequence[TrackInfo], Sequence[Path]], list[TrackGroup]]] = {
    "album": group_by_album,
    **{kind: _grouper(names_of) for kind, names_of in _NAMES_OF.items()},
}


def group_key(kind: str, info: TrackInfo, folder: Path) -> str:
    """The key of the group a song is in ("" if none). With several genres, the first."""
    if kind == "album":
        return _album_key(info, folder) if info.album else ""
    names = [name for name in _NAMES_OF[kind](info) if name]
    return names[0].casefold() if names else ""


class GroupListModel(QAbstractListModel):
    """The side list for artists or albums.

    It answers the same roles as the library model, so the side list's row
    painter can draw it. Covers come from `cover_for`, which is only called
    for rows on screen.
    """

    def __init__(
        self, cover_for: Callable[[TrackGroup], QImage | None], parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._cover_for = cover_for
        self._groups: list[TrackGroup] = []
        self._rows: dict[str, int] = {}  # group key -> row

    def set_groups(self, groups: list[TrackGroup]) -> None:
        self.beginResetModel()
        self._groups = groups
        self._rows = {group.key: row for row, group in enumerate(groups)}
        self.endResetModel()

    def group_at(self, row: int) -> TrackGroup | None:
        return self._groups[row] if 0 <= row < len(self._groups) else None

    def group(self, key: str) -> TrackGroup | None:
        return self.group_at(self.row_of(key))

    def row_of(self, key: str) -> int:
        return self._rows.get(key, -1)

    def refresh(self, key: str) -> None:
        """Repaint one group's row (its cover has changed)."""
        row = self.row_of(key)
        if row >= 0:
            self.dataChanged.emit(self.index(row), self.index(row))

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._groups)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._groups):
            return None
        group = self._groups[index.row()]
        if role in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.ToolTipRole,
            TrackListModel.TitleRole,
        ):
            return group.name
        if role == TrackListModel.SubtitleRole:
            return group.subtitle
        if role == TrackListModel.CoverRole:
            return self._cover_for(group)
        return None
