#!/bin/bash
# Builds dist/MusicHive-<version>-x86_64.AppImage from the last commit, so
# uncommitted changes are never shipped. Run it from anywhere. The version comes
# from musichive.spec. It builds inside a Debian 12 container with podman, so the
# AppImage runs on distributions at least as new as that (glibc 2.36 or later).
set -euo pipefail

cd "$(dirname "$0")/.."
command -v podman >/dev/null || {
    echo "Needs podman." >&2
    exit 1
}
version=$(awk '/^Version:/ {print $2}' packaging/musichive.spec)
top="$PWD/build/appimage"
name="MusicHive-$version-x86_64.AppImage"

rm -rf "$top"
mkdir -p "$top/src" "$top/out" dist
git archive --format=tar HEAD | tar -x -C "$top/src"

podman run --rm -v "$top:/build:Z" docker.io/library/debian:12 \
    bash /build/src/packaging/appimage/bundle.sh /build/src "/build/out/$name"

cp "$top/out/$name" dist/
ls -l "dist/$name"
