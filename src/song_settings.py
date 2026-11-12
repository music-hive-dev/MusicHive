"""Equaliser and visualiser presets of a song or playlist.

A song or playlist can name an equaliser preset and a visualiser preset that
are used while it plays. "" means none: the equaliser and visualiser stay as
they are set. When a song from a playlist plays and both name a preset, the
playlist's wins, unless the song is set to override playlists.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SoundPresets:
    equaliser: str = ""  # an equaliser preset's name; "" for none
    visualiser: str = ""  # a visualiser preset's name; "" for none
    over_playlist: bool = False  # only songs: used even when a playlist names its own

    @property
    def is_empty(self) -> bool:
        return not self.equaliser and not self.visualiser and not self.over_playlist


def chosen_presets(song: SoundPresets | None, playlist: SoundPresets | None) -> SoundPresets:
    """The presets to use for a song playing from a playlist (either may be None)."""
    song = song or SoundPresets()
    playlist = playlist or SoundPresets()

    def pick(song_value: str, playlist_value: str) -> str:
        if song_value and (song.over_playlist or not playlist_value):
            return song_value
        return playlist_value

    return SoundPresets(
        pick(song.equaliser, playlist.equaliser),
        pick(song.visualiser, playlist.visualiser),
    )
