# PyInstaller recipe for the Windows build. build.sh runs it from an MSYS2
# UCRT64 shell, where Python, PySide6, PyGObject and GStreamer all come from MSYS2.
import os
from pathlib import Path

here = Path(SPECPATH)
root = here.parent.parent
prefix = Path(os.environ["MINGW_PREFIX"])  # where MSYS2 keeps the UCRT64 libraries

# main.py and src/ find these next to themselves, as in the source folder.
datas = [(str(root / "ui"), "ui"), (str(root / "assets"), "assets"), (str(root / "days.json"), ".")]
datas += [(str(path), ".") for path in (*root.glob("*.svg"), *root.glob("*.png"))]
# Inter, the interface font, downloaded by build.sh; Windows does not have it.
datas.append((str(here / "fonts"), "bundled_fonts"))

# GStreamer's HTTP plugin opens libsoup itself while running, so PyInstaller
# cannot see that it is needed. Its own libraries are then found as usual.
binaries = [(str(path), ".") for path in (prefix / "bin").glob("libsoup-3.0-*.dll")]

a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    # Gio brings GLib's TLS module along, which HTTPS radio streams and podcasts need.
    hiddenimports=["gi.repository.Gio"],
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
coll = COLLECT(exe, a.binaries, a.datas, name="MusicHive")
