#!/bin/bash
# Builds dist/musichive-<version>-<release>.noarch.rpm from the last commit, so
# uncommitted changes are never shipped. Run it from anywhere.
set -euo pipefail

cd "$(dirname "$0")/.."
spec=packaging/musichive.spec
version=$(awk '/^Version:/ {print $2}' "$spec")
top="$PWD/build/rpm"

rm -rf "$top"
mkdir -p "$top/SOURCES" dist
git archive --format=tar.gz --prefix="musichive-$version/" \
    --output="$top/SOURCES/musichive-$version.tar.gz" HEAD
rpmbuild -bb --quiet --define "_topdir $top" "$spec"
cp "$top"/RPMS/noarch/musichive-*.rpm dist/
ls -l dist/musichive-*.rpm
