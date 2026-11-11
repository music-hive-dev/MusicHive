#!/bin/bash
# Builds dist/musichive-<version>-<release>-any.pkg.tar.zst from the last commit,
# so uncommitted changes are never shipped. Run it from anywhere. The version
# comes from musichive.spec. On Arch it uses makepkg directly; anywhere else it
# builds inside an Arch Linux container with podman.
set -euo pipefail

cd "$(dirname "$0")/.."
spec=packaging/musichive.spec
version=$(awk '/^Version:/ {print $2}' "$spec")
release=$(awk '/^Release:/ {print $2}' "$spec" | sed 's/%.*//')
top="$PWD/build/arch"

rm -rf "$top"
mkdir -p "$top/out" dist
git archive --format=tar.gz --prefix="musichive-$version/" \
    --output="$top/musichive-$version.tar.gz" HEAD
sed -e "s/^pkgver=.*/pkgver=$version/" -e "s/^pkgrel=.*/pkgrel=$release/" \
    packaging/PKGBUILD > "$top/PKGBUILD"

if command -v makepkg >/dev/null; then
    (cd "$top" && PKGDEST="$top/out" makepkg --force --nodeps --clean)
elif command -v podman >/dev/null; then
    # makepkg refuses to run as root, so it builds as a normal user in the
    # container and root copies the result out, which keeps it owned by you.
    podman run --rm -v "$top:/build:Z" docker.io/library/archlinux:latest bash -euc '
        pacman -Sy --noconfirm --needed base-devel python >/dev/null
        useradd -m builder
        cp -r /build /home/builder/pkg
        chown -R builder /home/builder/pkg
        su builder -c "cd ~/pkg && makepkg --nodeps --clean"
        cp /home/builder/pkg/*.pkg.tar.zst /build/out/
    '
else
    echo "Needs makepkg (on Arch) or podman (anywhere else)." >&2
    exit 1
fi

cp "$top"/out/musichive-*.pkg.tar.zst dist/
ls -l dist/musichive-*.pkg.tar.zst
