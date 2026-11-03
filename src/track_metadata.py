"""Reading tags and cover art from audio files.

This module has no GUI code. Tags are read with mutagen, and every failure
falls back to the file name, so a broken file never breaks the library.
"""

import base64
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.flac import Picture
from mutagen.id3 import ID3

# Image files looked for next to the music when a track has no embedded art.
COVER_FILE_STEMS = ("cover", "folder", "front", "album", "albumart")
COVER_FILE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


@dataclass
class TrackInfo:
    title: str
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    year: str = ""
    track_number: int | None = None
    disc_number: int | None = None
    duration_ms: int | None = None
    # Identifies the cover image; tracks with the same cover share one key.
    cover_key: str | None = None

    @property
    def subtitle(self) -> str:
        """The second line of a library row: "Artist – Album (Year)"."""
        line = " – ".join(part for part in (self.artist, self.album) if part)
        if self.year:
            line = f"{line} ({self.year})" if line else f"({self.year})"
        return line


def read_track_info(path: Path) -> tuple[TrackInfo, bytes | None]:
    """Return the track's tags and the raw bytes of its cover image (or None)."""
    info = TrackInfo(title=path.stem)
    cover: bytes | None = None

    try:
        audio = MutagenFile(str(path))
    except Exception:
        audio = None

    if audio is not None:
        tags = audio.tags
        info.title = _tag_text(tags, "TIT2", "title") or path.stem
        info.artist = _tag_text(tags, "TPE1", "artist")
        info.album_artist = _tag_text(tags, "TPE2", "albumartist", "album artist")
        info.album = _tag_text(tags, "TALB", "album")
        year = _tag_text(tags, "TDRC", "TYER", "date", "year")
        info.year = year[:4] if year[:4].isdigit() else ""
        info.track_number = _leading_number(_tag_text(tags, "TRCK", "tracknumber"))
        info.disc_number = _leading_number(_tag_text(tags, "TPOS", "discnumber"))

        length = getattr(getattr(audio, "info", None), "length", None)
        if length:
            info.duration_ms = int(length * 1000)

        try:
            cover = _embedded_cover(audio)
        except Exception:
            cover = None

    if cover is None:
        cover = _folder_cover(path.parent)

    return info, cover


def _leading_number(text: str) -> int | None:
    """Parse "3" or "3/12" (track 3 of 12) into 3."""
    head = text.split("/")[0].strip()
    return int(head) if head.isdigit() else None


def _tag_text(tags, *keys: str) -> str:
    """First non-empty text value among `keys`.

    ID3 files (MP3, some WAV) use frame names like "TIT2"; Vorbis-comment
    files (FLAC, Ogg) use names like "title". Both kinds are tried.
    """
    if tags is None:
        return ""
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:
            continue
        if value is None:
            continue
        if hasattr(value, "text"):  # an ID3 frame
            value = value.text
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        text = str(value).strip()
        if text:
            return text
    return ""


def _embedded_cover(audio) -> bytes | None:
    """Cover image stored inside the file, preferring the "front cover"."""
    tags = audio.tags

    # ID3 (MP3, and WAV files that carry ID3 tags)
    if isinstance(tags, ID3):
        pictures = tags.getall("APIC")
        if pictures:
            front = [p for p in pictures if p.type == 3]
            return (front or pictures)[0].data

    # FLAC keeps pictures in their own blocks
    flac_pictures = getattr(audio, "pictures", None)
    if flac_pictures:
        front = [p for p in flac_pictures if p.type == 3]
        return (front or flac_pictures)[0].data

    # Ogg Vorbis/Opus store a base64-encoded FLAC picture block in a comment
    if tags is not None:
        try:
            encoded_blocks = tags.get("metadata_block_picture") or []
        except Exception:
            encoded_blocks = []
        for encoded in encoded_blocks:
            try:
                return Picture(base64.b64decode(encoded)).data
            except Exception:
                continue

    return None


def _folder_cover(directory: Path) -> bytes | None:
    """Cover image from a file such as cover.jpg in the track's folder."""
    found = _find_folder_cover(str(directory))
    if found is None:
        return None
    try:
        return Path(found).read_bytes()
    except OSError:
        return None


@lru_cache(maxsize=512)
def _find_folder_cover(directory: str) -> str | None:
    """Path of the folder's cover image, cached so each folder is listed once."""
    try:
        entries = list(Path(directory).iterdir())
    except OSError:
        return None
    for stem in COVER_FILE_STEMS:  # earlier stems win
        for entry in entries:
            if entry.stem.lower() == stem and entry.suffix.lower() in COVER_FILE_EXTENSIONS:
                return str(entry)
    return None


# ----- details for the track info panel -----

_FORMAT_NAMES = {
    "MP3": "MP3",
    "FLAC": "FLAC",
    "OggVorbis": "Ogg Vorbis",
    "OggOpus": "Opus",
    "OggFLAC": "Ogg FLAC",
    "WAVE": "WAV",
}


@dataclass
class TrackDetails:
    """Everything the track info panel shows about one track."""

    title: str
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    year: str = ""
    audio_format: str = ""  # e.g. "FLAC 44.1 kHz @ 906k Stereo"
    cover: bytes | None = None  # the full-size cover image, as stored in the file

    @property
    def artist_line(self) -> str:
        """Line two of the panel: artist (album artist), or whichever exists."""
        if self.artist and self.album_artist:
            return f"{self.artist} ({self.album_artist})"
        return self.artist or self.album_artist

    @property
    def album_line(self) -> str:
        """Line three of the panel: album (year), or whichever exists."""
        line = self.album
        if self.year:
            line = f"{line} ({self.year})" if line else f"({self.year})"
        return line


def read_track_details(path: Path) -> TrackDetails:
    """Read what the track info panel needs from one audio file."""
    details = TrackDetails(title=path.stem)

    try:
        audio = MutagenFile(str(path))
    except Exception:
        audio = None

    if audio is not None:
        tags = audio.tags
        details.title = _tag_text(tags, "TIT2", "title") or path.stem
        details.artist = _tag_text(tags, "TPE1", "artist")
        details.album_artist = _tag_text(tags, "TPE2", "albumartist", "album artist")
        details.album = _tag_text(tags, "TALB", "album")
        year = _tag_text(tags, "TDRC", "TYER", "date", "year")
        details.year = year[:4] if year[:4].isdigit() else ""
        details.audio_format = _describe_format(audio, path)
        try:
            details.cover = _embedded_cover(audio)
        except Exception:
            details.cover = None

    if details.cover is None:
        details.cover = _folder_cover(path.parent)

    return details


def _describe_format(audio, path: Path) -> str:
    """For example "FLAC 44.1 kHz @ 906k Stereo". Unknown parts are left out."""
    name = _FORMAT_NAMES.get(type(audio).__name__, path.suffix.lstrip(".").upper())
    info = getattr(audio, "info", None)

    parts = [name]
    sample_rate = getattr(info, "sample_rate", 0)
    if sample_rate:
        parts.append(f"{sample_rate / 1000:g} kHz")
    bitrate = getattr(info, "bitrate", 0)
    if bitrate:
        parts.append(f"@ {round(bitrate / 1000)}k")
    channels = getattr(info, "channels", 0)
    if channels:
        parts.append(_channel_name(channels))
    return " ".join(parts)


def _channel_name(channels: int) -> str:
    return {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(channels, f"{channels} ch")