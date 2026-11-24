#!/bin/bash
# Prints the release notes: the %changelog entries in musichive.spec that are
# newer than the given version (the previous release), as Markdown. Without a
# version, or if it is not in the changelog, only the newest entry.
# Usage: release-notes.sh [previous version]
set -euo pipefail

cd "$(dirname "$0")/.."
spec=packaging/musichive.spec
previous=${1:-}
if [ -z "$previous" ] || ! grep -q "^\* .* - $previous-" "$spec"; then
    previous=""  # stop after the newest entry
fi
awk -v previous="$previous" '
    /^%changelog/ { inside = 1; next }
    !inside { next }
    /^\* / {
        if (previous != "" && index($0, "- " previous "-") > 0) exit
        if (previous == "" && entries == 1) exit
        entries++
        sub(/^\* [A-Za-z]+ [A-Za-z]+ [0-9]+ [0-9]+ [^-]*- /, "")
        print (entries > 1 ? "\n" : "") "**" $0 "**"
        next
    }
    /^- / { print }
' "$spec"
