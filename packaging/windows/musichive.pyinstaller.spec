# PyInstaller recipe for the Windows build. build.sh runs it from an MSYS2
# UCRT64 shell, where Python, PySide6, PyGObject and GStreamer all come from MSYS2.
import os
from pathlib import Path

here = Path(SPECPATH)
root = here.parent.parent
prefix = Path(os.environ["MINGW_PREFIX"])  # where MSYS2 keeps the UCRT64 libraries

# main.py and src/ find these next to themselves, as in the source folder.
datas = [(str(root / "ui"), "ui"), (str(root / "assets"), "assets"), (str(root / "days.json"), ".")]
datas.append((str(root / "LICENSE"), "."))
datas += [(str(path), ".") for path in (*root.glob("*.svg"), *root.glob("*.png"))]
# Inter, the interface font, downloaded by build.sh; Windows does not have it.
datas.append((str(here / "fonts"), "bundled_fonts"))

# GStreamer's HTTP plugin opens libsoup itself while running, so PyInstaller
# cannot see that it is needed. Its own libraries are then found as usual.
binaries = [(str(path), ".") for path in (prefix / "bin").glob("libsoup-3.0-*.dll")]

# Only the GStreamer plugins MusicHive uses, instead of all few hundred.
GSTREAMER_PLUGINS = [
    # playing: playbin, typefinding, the filters and the speakers
    "coreelements", "typefindfunctions", "playback", "app", "pbtypes", "audioconvert",
    "audioresample", "volume", "equalizer", "audiofx", "spectrum", "replaygain",
    # Not wasapi2: its wasapi2sink tells Windows not to remember MusicHive's volume
    # in the volume mixer (AUDCLNT_STREAMFLAGS_NOPERSIST); wasapisink does not.
    "audiobuffersplit", "autodetect", "wasapi", "directsound",
    # MP3, FLAC, Ogg Vorbis and Opus, WAV, and the AAC/MP4 of podcasts and radio
    "audioparsers", "mpg123", "id3demux", "apetag", "flac", "ogg", "vorbis", "opus",
    "wavparse", "isomp4", "faad", "fdkaac",
    # podcasts and radio over the internet
    "soup", "icydemux", "adaptivedemux2",
    # converting files: MP3 (lamemp3enc, xingmux); Ogg, Opus and FLAC are above
    "lame", "xingmux",
    # casting with pictures: WebM for Chromecasts, MPEG-TS with H.264 for DLNA TVs
    "audiotestsrc", "audiomixer", "videoconvertscale", "vpx", "matroska",
    "x264", "openh264", "videoparsersbad", "mpegtsmux",
]

a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    # Gio brings GLib's TLS module along, which HTTPS radio streams and podcasts need.
    hiddenimports=["gi.repository.Gio"],
    runtime_hooks=[str(here / "runtime_environment.py")],
    hookspath=[str(here / "hooks")],  # leaves Qt's media backends out
    hooksconfig={"gstreamer": {"include_plugins": GSTREAMER_PLUGINS}},
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MusicHive",
    console=False,
    icon=str(here / "musichive.ico"),
)
# The same program with a console window, for finding out why it does not start:
# everything is printed there, even what happens before main.py runs.
debug_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MusicHive-debug",
    console=True,
    icon=str(here / "musichive.ico"),
)
coll = COLLECT(exe, debug_exe, a.binaries, a.datas, name="MusicHive")
