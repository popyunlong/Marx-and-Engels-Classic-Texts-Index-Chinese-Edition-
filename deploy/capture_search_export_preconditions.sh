#!/usr/bin/env bash
# Capture the exact live-file hashes that a feature-only search-export release
# is allowed to replace.  Run on the production host immediately before the
# candidate tree is assembled.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: capture_search_export_preconditions.sh APP_DIR MANIFEST" >&2
  exit 2
fi

app_dir="$(readlink -f "$1")"
manifest="$(readlink -f "$2")"
[ -d "$app_dir" ] && [ -f "$manifest" ] || exit 2

while IFS= read -r relative; do
  case "$relative" in
    ''|'#'*) continue ;;
    /*|*'..'*) echo "unsafe manifest path: $relative" >&2; exit 3 ;;
  esac
  target="$app_dir/$relative"
  if [ -f "$target" ]; then
    printf '%s  %s\n' "$(sha256sum "$target" | awk '{print $1}')" "$relative"
  else
    printf 'MISSING  %s\n' "$relative"
  fi
done < "$manifest"
