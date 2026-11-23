#!/bin/bash
# Builds the Windows version from the last commit, so uncommitted changes are
# never shipped:
#   dist/MusicHive-<version>-windows-x64-setup.exe  (installer, needs Inno Setup 6)
#   dist/MusicHive-<version>-windows-x64.zip        (portable folder)
# Runs in an MSYS2 UCRT64 shell on Windows. GitHub Actions does this for every
# release (.github/workflows/windows.yml). The version comes from musichive.spec.
set -euo pipefail

cd "$(dirname "$0")/../.."
[ "${MSYSTEM:-}" = UCRT64 ] || {
    echo "Run this in an MSYS2 UCRT64 shell." >&2
    exit 1
}
version=$(awk '/^Version:/ {print $2}' packaging/musichive.spec)
top="$PWD/build/windows"
name="MusicHive-$version-windows-x64"
p=$MINGW_PACKAGE_PREFIX

echo "Installing Python, Qt and GStreamer..."
# PySide6 only suggests the Qt parts beyond the basics, so those are named here.
pacman -S --needed --noconfirm \
    "$p-python" "$p-python-pip" "$p-pyside6" "$p-python-gobject" "$p-python-mutagen" \
    "$p-qt6-multimedia" "$p-qt6-tools" "$p-qt6-svg" "$p-qt6-imageformats" \
    "$p-python-zeroconf" "$p-python-protobuf" "$p-python-requests" \
    "$p-gstreamer" "$p-gst-plugins-base" "$p-gst-plugins-good" "$p-gst-plugins-bad" \
    "$p-gst-plugins-ugly" "$p-gst-libav" "$p-libsoup3" "$p-glib-networking" \
    "$p-pyinstaller" "$p-pyinstaller-hooks-contrib" git unzip zip
# MSYS2 does not package these two; both are plain Python.
python -m pip install -q --no-deps --break-system-packages "PyChromecast>=14" casttube

echo "Checking that everything loads..."
python - <<'EOF'
import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstApp, GstVideo  # noqa: F401
from PySide6 import QtDBus, QtMultimedia, QtNetwork, QtUiTools  # noqa: F401
import mutagen, pychromecast  # noqa: F401, E401
Gst.init(None)
missing = [name for name in ("playbin", "autoaudiosink", "equalizer-nbands", "spectrum",
                             "level", "rganalysis", "souphttpsrc", "appsrc", "x264enc")
           if Gst.ElementFactory.find(name) is None]
if missing:
    raise SystemExit(f"GStreamer elements missing: {', '.join(missing)}")
EOF

rm -rf "$top"
mkdir -p "$top/src" dist
git archive --format=tar HEAD | tar -x -C "$top/src"

echo "Downloading the Inter font..."
fonts="$top/src/packaging/windows/fonts"  # the PyInstaller recipe looks here
curl -sSfL -o "$top/inter.zip" https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip
unzip -q -j "$top/inter.zip" 'extras/ttf/Inter-*.ttf' LICENSE.txt -d "$fonts"
mv "$fonts/LICENSE.txt" "$fonts/Inter.LICENSE.txt"

echo "Bundling with PyInstaller..."
python -m PyInstaller --noconfirm --clean \
    --distpath "$top/dist" --workpath "$top/work" \
    "$top/src/packaging/windows/musichive.pyinstaller.spec"

echo "Packing the portable zip..."
rm -f "dist/$name.zip"
(cd "$top/dist" && zip -qr9 "$top/$name.zip" MusicHive)
cp "$top/$name.zip" dist/

iscc="/c/Program Files (x86)/Inno Setup 6/ISCC.exe"
if [ -x "$iscc" ]; then
    echo "Making the installer..."
    # Stop MSYS2 from rewriting the /D options as if they were paths.
    MSYS2_ARG_CONV_EXCL='*' "$iscc" -Q \
        "/DAppVersion=$version" \
        "/DSourceDir=$(cygpath -w "$top/dist/MusicHive")" \
        "/DOutputDir=$(cygpath -w "$PWD/dist")" \
        "$(cygpath -w "$top/src/packaging/windows/musichive.iss")"
else
    echo "Inno Setup 6 is not installed, so there is no installer; the zip is ready."
fi
ls -l dist/"$name"*
