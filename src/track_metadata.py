"""Reading tags and cover art from audio files.

This module has no GUI code. Tags are read with mutagen, and every failure
falls back to the file name, so a broken file never breaks the library.
"""

import base64
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.flac import Picture
from mutagen.id3 import APIC, COMM, ID3, POPM, TXXX, USLT, Frames

# Image files looked for next to the music when a track has no embedded art.
COVER_FILE_STEMS = ("cover", "folder", "front", "album", "albumart")
COVER_FILE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

# What ID3 (MP3, WAV) calls its frames, in the names Vorbis comments (FLAC, Ogg)
# use, so that one tag has one name whatever the file format.
_ID3_TAG_NAMES = {
    "TALB": "album",
    "TBPM": "bpm",
    "TCMP": "compilation",
    "TCOM": "composer",
    "TCON": "genre",
    "TCOP": "copyright",
    "TDEN": "encodingdate",
    "TDLY": "playlistdelay",
    "TDOR": "originaldate",
    "TDRC": "date",
    "TDRL": "releasedate",
    "TDTG": "taggingdate",
    "TENC": "encodedby",
    "TEXT": "lyricist",
    "TFLT": "filetype",
    "TIT1": "grouping",
    "TIT2": "title",
    "TIT3": "subtitle",
    "TKEY": "initialkey",
    "TLAN": "language",
    "TLEN": "length",
    "TMED": "media",
    "TMOO": "mood",
    "TOAL": "originalalbum",
    "TOFN": "originalfilename",
    "TOLY": "originallyricist",
    "TOPE": "originalartist",
    "TOWN": "owner",
    "TPE1": "artist",
    "TPE2": "albumartist",
    "TPE3": "conductor",
    "TPE4": "remixer",
    "TPOS": "discnumber",
    "TPRO": "producednotice",
    "TPUB": "publisher",
    "TRCK": "tracknumber",
    "TRSN": "radiostation",
    "TRSO": "radiostationowner",
    "TSO2": "albumartistsort",
    "TSOA": "albumsort",
    "TSOC": "composersort",
    "TSOP": "artistsort",
    "TSOT": "titlesort",
    "TSRC": "isrc",
    "TSSE": "encoder",
    "TSST": "discsubtitle",
    "WCOM": "commercialurl",
    "WCOP": "copyrighturl",
    "WOAF": "fileurl",
    "WOAR": "artisturl",
    "WOAS": "sourceurl",
    "WORS": "radiourl",
    "WPAY": "paymenturl",
    "WPUB": "publisherurl",
}
# Different spellings of the same tag.
_TAG_ALIASES = {
    "disc": "discnumber",
    "totaldiscs": "disctotal",
    "totaltracks": "tracktotal",
    "id3v1comment": "comment",
    "key": "initialkey",
    "label": "publisher",
    "organization": "publisher",
    "tempo": "bpm",
    "track": "tracknumber",
    "unsyncedlyrics": "lyrics",
    "year": "date",
}
_ID3_FRAME_IDS = {name: frame_id for frame_id, name in _ID3_TAG_NAMES.items()}
# Vorbis comment names that are not simply the tag name in capitals.
_VORBIS_NAMES = {"publisher": "ORGANIZATION"}
# Free text: a ";" in these does not separate values.
_SINGLE_VALUE_TAGS = {"comment", "lyrics"}
# Tags that are not edited as text: the star rating and what players keep up to date.
PROTECTED_TAGS = {"length", "playcount", "rating"}
# Track and disc numbers: ID3 stores "3/12" in one frame, Vorbis comments two tags.
_NUMBER_TAGS = (("tracknumber", "tracktotal", "TRCK"), ("discnumber", "disctotal", "TPOS"))

# Pictures stored as text, and iTunes' internal playback data.
_IGNORED_TAGS = {
    "coverart",
    "coverartmime",
    "itunnorm",
    "itunpgap",
    "itunsmpb",
    "metadatablockpicture",
}


_POPM_EMAIL = "Windows Media Player 9 Series"
_POPM_RATINGS = (0, 1, 64, 128, 196, 255)  # by number of stars


def _with_year(text: str, year: str) -> str:
    """`text` followed by "(Year)", or whichever of the two exists."""
    if not year:
        return text
    return f"{text} ({year})" if text else f"({year})"


@dataclass
class TrackInfo:
    title: str
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    year: str = ""
    genre: str = ""
    play_count: int = 0
    track_number: int | None = None
    disc_number: int | None = None
    duration_ms: int | None = None
    # Identifies the cover image; tracks with the same cover share one key.
    cover_key: str | None = None
    file_size: int | None = None  # in bytes
    added_at: float | None = None  # when it first joined the library, in seconds since 1970
    last_played: float | None = None  # when a play last counted, in seconds since 1970
    rating: int = 0  # stars, from 0 to 5
    # Text tags: all of them during a scan (read_all_tags), later only those with a column.
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def subtitle(self) -> str:
        """The second line of a library row: "Artist – Album (Year)"."""
        line = " – ".join(part for part in (self.artist, self.album) if part)
        return _with_year(line, self.year)

    @property
    def album_line(self) -> str:
        """"Album (Year)", or whichever of the two exists."""
        return _with_year(self.album, self.year)


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
        info.genre = _genre_text(tags)
        info.track_number = _leading_number(_tag_text(tags, "TRCK", "tracknumber"))
        info.disc_number = _leading_number(_tag_text(tags, "TPOS", "discnumber"))
        try:
            info.tags = read_all_tags(tags)
        except Exception:
            info.tags = {}
        info.rating = read_rating(tags)

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


def read_rating(tags) -> int:
    """The stars (1 to 5) stored in a file's tags, or 0 if it has no rating."""
    try:
        if isinstance(tags, ID3):
            ratings = [frame.rating for frame in tags.getall("POPM") if frame.rating]
            return _stars_from_popm(ratings[0]) if ratings else 0
        values = tags.get("rating") if tags is not None else None
        return _stars_from_text(values[0]) if values else 0
    except (ValueError, TypeError, AttributeError):
        return 0


def _stars_from_popm(rating: int) -> int:
    """POPM is 1 to 255; programs use different steps, so go by ranges."""
    for stars, upper_limit in enumerate((31, 95, 159, 223), start=1):
        if rating <= upper_limit:
            return stars
    return 5


def _stars_from_text(text: str) -> int:
    """A RATING tag: 0 to 100 (KDE, MusicHive), 1 to 5, or a fraction such as 0.8."""
    value = float(text)
    if value <= 0:
        return 0
    if value < 1:
        return max(1, round(value * 5))
    if value <= 5:
        return round(value)
    return max(1, min(5, round(value / 20)))


@dataclass
class TagChanges:
    """What to change in a file's tags. Whatever is not mentioned stays as it is."""

    tags: dict[str, str] = field(default_factory=dict)  # new text; "" removes the tag
    cover: bytes | None = None
    cover_changed: bool = False  # with `cover` None, this removes the picture
    rating: int | None = None  # stars; None leaves the rating alone

    @property
    def is_empty(self) -> bool:
        return not self.tags and not self.cover_changed and self.rating is None


def write_rating(path: Path, stars: int) -> None:
    """Store a rating of 0 to 5 stars in a file's tags. Raises on failure."""
    write_tag_changes(path, TagChanges(rating=stars))


def write_tag_changes(path: Path, changes: TagChanges) -> None:
    """Apply changes to a file's tags. Raises on failure."""
    audio = MutagenFile(str(path))
    if audio is None:
        raise ValueError("unknown file format")
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if isinstance(tags, ID3):
        _write_id3_tags(tags, changes.tags)
    else:
        _write_vorbis_tags(tags, changes.tags)
    if changes.rating is not None:
        _set_rating(tags, changes.rating)
    if changes.cover_changed:
        _set_cover(audio, changes.cover)
    if isinstance(tags, ID3):
        # Keep the file's ID3 version. No separator means several values stay separate,
        # instead of being merged into text like "Rock/Pop".
        audio.save(v2_version=3 if tags.version[1] == 3 else 4, v23_sep=None)
    else:
        audio.save()


def _tag_values(name: str, text: str) -> list[str]:
    """The separate values typed for a tag, as in "Rock; Pop"."""
    if name in _SINGLE_VALUE_TAGS:
        return [text]
    return [value for value in re.split(r"\s*;\s*", text.strip()) if value]


def _write_vorbis_tags(tags, changes: dict[str, str]) -> None:
    for name, text in changes.items():
        for key in list(tags.keys()):
            if clean_tag_name(key) == name:
                del tags[key]
        if text.strip():
            tags[_VORBIS_NAMES.get(name, name.upper())] = _tag_values(name, text)


def _write_id3_tags(tags: ID3, changes: dict[str, str]) -> None:
    changes = dict(changes)
    for number_tag, total_tag, frame_id in _NUMBER_TAGS:
        if number_tag in changes or total_tag in changes:
            old = tags.get(frame_id)
            number, _, total = (str(old.text[0]) if old and old.text else "").partition("/")
            number = changes.pop(number_tag, number).strip()
            total = changes.pop(total_tag, total).strip()
            tags.delall(frame_id)
            if number:
                text = f"{number}/{total}" if total else number
                tags.add(Frames[frame_id](encoding=3, text=[text]))
    for name, text in changes.items():
        _remove_id3_tag(tags, name)
        if text.strip():
            tags.add(_new_id3_frame(name, text))


def _remove_id3_tag(tags: ID3, name: str) -> None:
    """Remove a tag, whichever kind of frame it is stored in."""
    for frame in list(tags.values()):
        if not any(clean_tag_name(entry[0]) == name for entry in _id3_frame_entries(frame)):
            continue
        if frame.FrameID == "TIPL":  # holds other people's roles too
            frame.people = [pair for pair in frame.people if clean_tag_name(pair[0]) != name]
            if frame.people:
                continue
        del tags[frame.HashKey]


def _new_id3_frame(name: str, text: str):
    if name == "comment":
        return COMM(encoding=3, lang="eng", desc="", text=[text])
    if name == "lyrics":
        return USLT(encoding=3, lang="eng", desc="", text=text)
    frame_id = _ID3_FRAME_IDS.get(name)
    if frame_id is None:
        return TXXX(encoding=3, desc=name.upper(), text=_tag_values(name, text))
    if frame_id.startswith("W"):
        return Frames[frame_id](url=text)
    return Frames[frame_id](encoding=3, text=_tag_values(name, text))


def _set_rating(tags, stars: int) -> None:
    """These are the conventions most players and KDE's Dolphin understand: an ID3
    POPM frame on the Windows Media Player scale, RATING from 0 to 100 otherwise."""
    if isinstance(tags, ID3):
        # Only one rating, so that no player reads an old one. Its play count is kept.
        count = max((getattr(frame, "count", 0) for frame in tags.getall("POPM")), default=0)
        tags.delall("POPM")
        if stars or count:
            tags.add(POPM(email=_POPM_EMAIL, rating=_POPM_RATINGS[stars], count=count))
    elif stars:
        tags["rating"] = str(stars * 20)
    elif "rating" in tags:
        del tags["rating"]


def _set_cover(audio, data: bytes | None) -> None:
    """Replace a file's pictures with this front cover (None removes them)."""
    tags = audio.tags
    mime = "image/png" if data and data.startswith(b"\x89PNG") else "image/jpeg"
    if isinstance(tags, ID3):
        tags.delall("APIC")
        if data:
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="", data=data))
        return
    picture = Picture()
    picture.type = 3
    picture.mime = mime
    picture.data = data or b""
    if hasattr(audio, "clear_pictures"):  # FLAC keeps pictures in their own blocks
        audio.clear_pictures()
        if data:
            audio.add_picture(picture)
        return
    for key in ("metadata_block_picture", "coverart", "coverartmime"):
        if key in tags:
            del tags[key]
    if data:
        tags["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]


@dataclass
class EditableFile:
    """What the tag editor shows about one file."""

    tags: dict[str, str] = field(default_factory=dict)
    cover: bytes | None = None  # only a picture inside the file, not a cover.jpg next to it
    rating: int = 0
    lrc_text: str | None = None  # the .lrc file next to the song; None if there is none


def _read_lrc(path: Path) -> str | None:
    try:
        data = path.with_suffix(".lrc").read_bytes()
    except OSError:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def read_editable_file(path: Path) -> EditableFile:
    """Read a file for the tag editor. A file that can't be read comes back empty."""
    result = EditableFile(lrc_text=_read_lrc(path))
    try:
        audio = MutagenFile(str(path))
    except Exception:
        return result
    if audio is None:
        return result
    result.tags = read_all_tags(audio.tags)
    for name in PROTECTED_TAGS:
        result.tags.pop(name, None)
    for number_tag, total_tag, _frame_id in _NUMBER_TAGS:
        number, _, total = result.tags.get(number_tag, "").partition("/")
        if total:
            result.tags[number_tag] = number
            result.tags.setdefault(total_tag, total)
    result.rating = read_rating(audio.tags)
    try:
        result.cover = _embedded_cover(audio)
    except Exception:
        result.cover = None
    return result


def clean_tag_name(name) -> str:
    """The one name a tag goes by: lower case, no spaces, spelling variants merged."""
    cleaned = re.sub(r"[\s_-]+", "", str(name).casefold())
    return _TAG_ALIASES.get(cleaned, cleaned)


def read_all_tags(tags) -> dict[str, str]:
    """Every text tag in a file, under one name per tag whatever the format.

    Names are lower case without spaces, such as "composer" or "albumartist".
    A tag with several values has them joined by "; ". Pictures and other
    binary data are left out.
    """
    found: dict[str, list[str]] = {}
    if isinstance(tags, ID3):
        entries = (entry for frame in tags.values() for entry in _id3_frame_entries(frame))
    elif hasattr(tags, "as_dict"):  # Vorbis comments
        entries = tags.as_dict().items()
    else:
        return {}

    for name, values in entries:
        clean_name = clean_tag_name(name)
        if clean_name and clean_name not in _IGNORED_TAGS:
            kept = found.setdefault(clean_name, [])
            kept.extend(value for value in _texts(values) if value not in kept)
    return {name: "; ".join(values) for name, values in found.items() if values}


def _texts(values) -> list[str]:
    """Non-empty text values from a value or a list of them."""
    if not isinstance(values, (list, tuple)):
        values = [values]
    return [str(value).strip() for value in values if str(value).strip()]


def _id3_frame_entries(frame) -> list[tuple[str, object]]:
    """The (tag name, values) that one ID3 frame holds. None for binary frames."""
    frame_id = frame.FrameID
    if frame_id == "TXXX":
        return [(frame.desc, frame.text)]
    if frame_id == "TCON":
        return [("genre", frame.genres)]
    if frame_id == "COMM":
        return [(frame.desc or "comment", frame.text)]
    if frame_id == "USLT":
        return [("lyrics", frame.text)]
    if frame_id == "WXXX":
        return [(frame.desc or "url", frame.url)]
    if frame_id == "TIPL":  # roles, such as producer or engineer
        return [(role, person) for role, person in frame.people]
    if frame_id == "TMCL":  # musicians and their instruments
        return [("performer", f"{person} ({role})") for role, person in frame.people]
    if frame_id == "POPM":
        return [("rating", frame.rating)]
    if frame_id == "PCNT":
        return [("playcount", frame.count)]
    if frame_id.startswith("T"):
        return [(_ID3_TAG_NAMES.get(frame_id, frame_id), frame.text)]
    if frame_id.startswith("W"):
        return [(_ID3_TAG_NAMES.get(frame_id, frame_id), frame.url)]
    return []


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


def _genre_text(tags) -> str:
    """The genre(s) as text.

    ID3 files store them as "TCON" (which can hold numeric codes that mutagen
    translates); Vorbis-comment files (FLAC, Ogg) use "genre".
    """
    if tags is None:
        return ""
    try:
        frame = tags.get("TCON")
        if frame is not None and hasattr(frame, "genres"):
            names = [str(genre).strip() for genre in frame.genres]
            names = [name for name in names if name]
            if names:
                return ", ".join(names)
    except Exception:
        pass
    try:
        values = tags.get("genre")
    except Exception:
        values = None
    if values:
        if not isinstance(values, (list, tuple)):
            values = [values]
        names = [str(value).strip() for value in values if str(value).strip()]
        return ", ".join(names)
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
    rating: int = 0  # stars stored in the file, from 0 to 5
    lyrics: str = ""  # timed (LRC) or plain, from a .lrc file or the song itself

    @property
    def artist_line(self) -> str:
        """Line two of the panel: artist (album artist), or whichever exists."""
        if self.artist and self.album_artist:
            return f"{self.artist} ({self.album_artist})"
        return self.artist or self.album_artist

    @property
    def album_line(self) -> str:
        """Line three of the panel: album (year), or whichever exists."""
        return _with_year(self.album, self.year)


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
        details.rating = read_rating(tags)
        try:
            details.cover = _embedded_cover(audio)
        except Exception:
            details.cover = None

    if details.cover is None:
        details.cover = _folder_cover(path.parent)

    try:
        details.lyrics = _song_lyrics(audio, path)
    except Exception:
        details.lyrics = ""
    return details


def _song_lyrics(audio, path: Path) -> str:
    """A .lrc file next to the song wins; then timed lyrics in it, then plain ones."""
    from_file = _read_lrc(path)
    if from_file and from_file.strip():
        return from_file
    tags = audio.tags if audio is not None else None
    if isinstance(tags, ID3):
        for frame in tags.getall("SYLT"):
            if frame.format == 2:  # the times are in milliseconds
                return "\n".join(
                    f"[{ms // 60000:02d}:{ms % 60000 / 1000:05.2f}]{text.strip()}"
                    for text, ms in frame.text
                )
    return read_all_tags(tags).get("lyrics", "")


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