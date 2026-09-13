#!/usr/bin/env bash
# Rewrite the key labels in .env files from `label=key` to `label:key`.
#
#   ./convert-key-labels.sh path/to/.env [more .env files ...]
#   ./convert-key-labels.sh            # every .env under this folder
#
# XTRACTING_API_KEYS and XTRACTING_PROJECT_KEYS hold comma-separated keys,
# each optionally with a label in front. The separator between the label and
# the key is a colon:
#
#   XTRACTING_API_KEYS=plant-docs:ab12cd34.xxxx,market:ef56gh78.yyyy
#
# This script takes the value of those two variables, splits it at the commas,
# and in every entry turns the FIRST `=` into a `:`. The `=` after the variable
# name stays. An entry that already uses a colon, or has no label, is left as
# it is. Nothing else in the file is touched, and no value is ever printed -
# the file holds secrets, and this script has no reason to show them.
#
# A backup of each changed file is written beside it as `.env.bak-labels`.

set -euo pipefail

convert_one() {
    local file=$1
    [ -f "$file" ] || { echo "skip  $file (not a file)"; return; }
    local tmp
    tmp=$(mktemp)
    awk '
        BEGIN { FS = "" }
        /^[[:space:]]*(export[[:space:]]+)?(XTRACTING_API_KEYS|XTRACTING_PROJECT_KEYS)=/ {
            eq = index($0, "=")
            name = substr($0, 1, eq)
            value = substr($0, eq + 1)
            n = split(value, parts, ",")
            out = ""
            for (i = 1; i <= n; i++) {
                p = parts[i]
                if (index(p, ":") == 0) {
                    e = index(p, "=")
                    if (e > 0) p = substr(p, 1, e - 1) ":" substr(p, e + 1)
                }
                out = out (i > 1 ? "," : "") p
            }
            print name out
            next
        }
        { print }
    ' "$file" > "$tmp"
    if cmp -s "$file" "$tmp"; then
        rm -f "$tmp"
        echo "same  $file"
    else
        cp -p "$file" "$file.bak-labels"
        cat "$tmp" > "$file"       # keeps the file's owner and mode
        rm -f "$tmp"
        echo "done  $file (backup: $file.bak-labels)"
    fi
}

if [ $# -eq 0 ]; then
    here="$(cd "$(dirname "$0")" && pwd)"
    while IFS= read -r f; do convert_one "$f"; done < <(find "$here" -name .env -type f | sort)
else
    for f in "$@"; do convert_one "$f"; done
fi
