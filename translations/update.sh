#!/bin/bash
# Brings the translations up to date with the code and the .ui files, then
# compiles them into the .qm files MusicHive loads. Run it from anywhere:
#
#   translations/update.sh          update every translations/musichive_<code>.ts
#   translations/update.sh de fr    the same, and start German and French
#
# Translate a .ts file in Qt Linguist (pyside6-linguist), then run this again.
# A language shows up in Settings > Interface once its .qm file is there.
set -euo pipefail

cd "$(dirname "$0")/.."

find_tool() {
    for name in "$@"; do
        if command -v "$name" >/dev/null; then echo "$name"; return; fi
        if [ -x ".venv/bin/$name" ]; then echo ".venv/bin/$name"; return; fi
    done
    echo "Cannot find $1. Install PySide6 (pip install pyside6)." >&2
    exit 1
}
lupdate=$(find_tool pyside6-lupdate lupdate-qt6 lupdate)
lrelease=$(find_tool pyside6-lrelease lrelease-qt6 lrelease)

targets=()
for code in "$@"; do
    targets+=("translations/musichive_$code.ts")
done
for file in translations/musichive_*.ts; do
    [ -e "$file" ] && targets+=("$file")
done
if [ ${#targets[@]} -eq 0 ]; then
    echo "No translations yet. Start one with: $0 <language code>, for example $0 de" >&2
    exit 1
fi
# Each file once, even if it was also named on the command line.
mapfile -t targets < <(printf '%s\n' "${targets[@]}" | sort -u)

"$lupdate" -source-language en_US -no-obsolete -locations relative \
    main.py src/*.py ui/*.ui -ts "${targets[@]}"
for file in "${targets[@]}"; do
    "$lrelease" -silent "$file" -qm "${file%.ts}.qm"
done
