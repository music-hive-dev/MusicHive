#!/bin/bash
# Builds dist/musichive_<version>-<release>_all.deb from the last commit, so
# uncommitted changes are never shipped. Run it from anywhere. The version comes
# from musichive.spec. Needs dpkg-deb (on Fedora: sudo dnf install dpkg).
set -euo pipefail

cd "$(dirname "$0")/.."
command -v dpkg-deb >/dev/null || {
    echo "dpkg-deb is missing. Install dpkg first." >&2
    exit 1
}
spec=packaging/musichive.spec
version=$(awk '/^Version:/ {print $2}' "$spec")
release=$(awk '/^Release:/ {print $2}' "$spec" | sed 's/%.*//')
top="$PWD/build/deb"
root="$top/musichive"
app="$root/usr/share/musichive"
umask 022

rm -rf "$top"
mkdir -p "$top/src" "$app" "$root/usr/bin" "$root/DEBIAN" dist
git archive --format=tar HEAD | tar -x -C "$top/src"

cd "$top/src"
cp -a main.py days.json ./*.svg ./*.png src ui assets "$app/"
install -Dm644 musichive_colour.svg \
    "$root/usr/share/icons/hicolor/scalable/apps/musichive.svg"
# The file name must match setDesktopFileName in main.py.
install -Dm644 packaging/musichive.desktop \
    "$root/usr/share/applications/musichive.desktop"
cd - >/dev/null

cat > "$root/usr/bin/musichive" <<'SH'
#!/bin/sh
exec /usr/bin/python3 /usr/share/musichive/main.py "$@"
SH
chmod 755 "$root/usr/bin/musichive"

cat > "$root/DEBIAN/control" <<CONTROL
Package: musichive
Version: $version-$release
Architecture: all
Maintainer: MusicHive
Installed-Size: $(du -sk "$root/usr" | cut -f1)
Section: sound
Priority: optional
Depends: python3, python3-pyside6.qtcore, python3-pyside6.qtgui,
 python3-pyside6.qtwidgets, python3-pyside6.qtdbus,
 python3-pyside6.qtmultimedia, python3-pyside6.qtnetwork,
 python3-pyside6.qtuitools, qt6-svg-plugins, python3-mutagen, python3-gi,
 gir1.2-gstreamer-1.0, gir1.2-gst-plugins-base-1.0,
 gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, curl, fonts-inter
Recommends: gstreamer1.0-plugins-bad, gstreamer1.0-libav,
 gstreamer1.0-plugins-ugly, udisks2, dolphin
Description: Simple local music player
 MusicHive is a local music player with playlists, an equaliser, lyrics,
 Last.fm scrobbling and a mini player.
CONTROL

# Compile the Python files on the machine it's installed on, so they match its
# Python version, and clean them up again on removal.
cat > "$root/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
python3 -m compileall -q /usr/share/musichive >/dev/null 2>&1 || true
SH
cat > "$root/DEBIAN/prerm" <<'SH'
#!/bin/sh
set -e
find /usr/share/musichive -type d -name __pycache__ -prune -exec rm -rf {} +
SH
chmod 755 "$root/DEBIAN/postinst" "$root/DEBIAN/prerm"

out="dist/musichive_${version}-${release}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$root" "$out" >/dev/null
ls -l "$out"
