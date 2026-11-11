#!/bin/bash
# Runs inside a Debian 12 container (see build-appimage.sh). Puts Python, PySide6,
# PyGObject, GStreamer and MusicHive into an AppDir and packs it into an AppImage.
# Usage: bundle.sh <source folder> <output file>
set -euo pipefail

src=$1
out=$2
app=/tmp/MusicHive.AppDir
lib=/usr/lib/x86_64-linux-gnu
python=python3.11
site="$app/usr/lib/python3/dist-packages"
export DEBIAN_FRONTEND=noninteractive

echo "Installing what the AppImage carries..."
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-pip python3-gi file wget ca-certificates \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-pulseaudio \
    libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-render-util0 libxcb-xinerama0 libxcb-shape0 libxcb-randr0 libxcb-xkb1 \
    libxkbcommon0 libegl1 libgl1 libfontconfig1 libdbus-1-3 libwayland-cursor0 \
    libwayland-egl1 >/dev/null

echo "Copying Python..."
mkdir -p "$app/usr/bin" "$app/usr/lib" "$app/usr/libexec" "$site"
cp -L "/usr/bin/$python" "$app/usr/bin/python3"
cp -a "/usr/lib/$python" "$app/usr/lib/"
(cd "$app/usr/lib/$python" && rm -rf test idlelib tkinter turtledemo ensurepip lib2to3 \
    sitecustomize.py lib-dynload/_tkinter*.so)
find "$app/usr/lib/$python" -name __pycache__ -prune -exec rm -rf {} +
cp -a /usr/lib/python3/dist-packages/gi "$site/"

echo "Installing PySide6 and Mutagen..."
python3 -m pip install -q --break-system-packages --no-compile --target "$site" \
    "PySide6>=6.9" "mutagen>=1.47"
rm -rf "$site/bin"
# Leave out the parts of Qt MusicHive doesn't use. Qt WebEngine alone is a few
# hundred megabytes.
(
    cd "$site/PySide6"
    rm -rf Qt/qml Qt/libexec Qt/resources Qt/translations/qtwebengine_locales \
        include typesystems glue scripts
    rm -rf Qt3D* QtWebEngine* QtPdf* QtCharts* QtDataVisualization* QtGraphs* QtQuick3D* \
        Qt/lib/libQt63D* Qt/lib/libQt6WebEngine* Qt/lib/libQt6Pdf* Qt/lib/libQt6Charts* \
        Qt/lib/libQt6DataVisualization* Qt/lib/libQt6Graphs* Qt/lib/libQt6Quick3D*
    find . -maxdepth 1 -type f -perm -u+x ! -name '*.so*' -delete
    find . -name '*.pyi' -delete
)

echo "Copying GStreamer..."
cp -a "$lib/girepository-1.0" "$app/usr/lib/"
cp -a "$lib/gstreamer-1.0" "$app/usr/lib/"
cp -L "$lib/gstreamer1.0/gstreamer-1.0/gst-plugin-scanner" "$app/usr/libexec/"
cp "$src/packaging/appimage/"gst-plugin-scanner-wrapper "$app/usr/libexec/"
# The typelibs name these libraries but nothing links to them, so list them here.
for name in libgirepository-1.0.so.1 libgstreamer-1.0.so.0 libgstbase-1.0.so.0 \
    libgstapp-1.0.so.0 libgobject-2.0.so.0 libglib-2.0.so.0 libgio-2.0.so.0 \
    libgmodule-2.0.so.0; do
    cp -L "$lib/$name" "$app/usr/lib/"
done

echo "Copying MusicHive..."
mkdir -p "$app/usr/share/musichive" "$app/usr/lib/musichive-appimage"
(cd "$src" && cp -a main.py days.json ./*.svg ./*.png src ui assets "$app/usr/share/musichive/")
cp "$src/packaging/appimage/"AppRun "$app/"
cp "$src/packaging/appimage/"start.py "$app/usr/lib/musichive-appimage/"
cp "$src/packaging/appimage/"musichive-restart "$app/usr/bin/"
# The file name must match setDesktopFileName in main.py.
install -Dm644 "$src/packaging/musichive.desktop" "$app/usr/share/applications/musichive.desktop"
install -Dm644 "$src/musichive_colour.svg" "$app/usr/share/icons/hicolor/scalable/apps/musichive.svg"
cp "$src/packaging/musichive.desktop" "$app/"
cp "$src/musichive_colour.svg" "$app/musichive.svg"
ln -s musichive.svg "$app/.DirIcon"

echo "Copying the libraries all of that needs..."
# These come from the system the AppImage runs on: the C library, graphics
# drivers, the display server and things that must match the system's setup.
excluded='^(ld-linux|libc|libm|libmvec|libdl|libpthread|librt|libresolv|libutil|libanl|libnsl|libnss_.*|libthread_db|libBrokenLocale|libgcc_s|libstdc\+\+|libGL|libGLX|libGLdispatch|libOpenGL|libEGL|libGLESv2|libgbm|libdrm|libvulkan|libX11|libX11-xcb|libxcb|libxcb-dri2|libxcb-dri3|libxcb-glx|libxcb-present|libxcb-sync|libxshmfence|libwayland-client|libwayland-server|libwayland-egl|libwayland-cursor|libasound|libfontconfig|libfreetype|libexpat|libz|libdbus-1|libudev|libgpg-error|libcom_err)\.so'
missing=$(mktemp)
find "$app" -type f \( -name '*.so*' -o -perm -u+x \) -print0 |
    while IFS= read -r -d '' file; do
        file -b "$file" | grep -q '^ELF' || continue
        ldd "$file" 2>/dev/null | awk -v f="$file" -v m="$missing" '
            /=> not found/ { print $1 " (for " f ")" >> m; next }
            /=> \// { print $3 }'
    done | sort -u | while read -r path; do
        name=$(basename "$path")
        [[ $path == "$app"/* || $name =~ $excluded ]] && continue
        [ -e "$app/usr/lib/$name" ] || cp -L "$path" "$app/usr/lib/"
    done
if [ -s "$missing" ]; then
    echo "Libraries that were not found (fine if they belong to unused parts):"
    sort -u "$missing" | sed 's/^/  /'
fi

echo "Compiling the Python files..."
python3 -m compileall -q -j0 "$app/usr/lib/$python" "$site" "$app/usr/share/musichive" \
    >/dev/null || true

echo "Packing the AppImage..."
wget -q -O /tmp/appimagetool \
    https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x /tmp/appimagetool
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 /tmp/appimagetool --no-appstream "$app" "$out"
