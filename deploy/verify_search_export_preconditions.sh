#!/usr/bin/env bash
# Refuse a search-export release if any live file changed since its production
# baseline was captured.  This runs before archive extraction or blue/green
# candidate startup, so a mismatch leaves the live site untouched.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: verify_search_export_preconditions.sh APP_DIR HASH_FILE" >&2
  exit 2
fi

app_dir="$(readlink -f "$1")"
hash_file="$(readlink -f "$2")"
[ -d "$app_dir" ] && [ -f "$hash_file" ] || exit 2

while read -r expected relative; do
  [ -n "${expected:-}" ] && [ -n "${relative:-}" ] || continue
  case "$relative" in
    /*|*'..'*) echo "unsafe precondition path: $relative" >&2; exit 3 ;;
  esac
  target="$app_dir/$relative"
  if [ "$expected" = "MISSING" ]; then
    if [ -e "$target" ]; then
      echo "precondition failed (expected missing): $relative" >&2
      exit 4
    fi
    continue
  fi
  if [ ! -f "$target" ]; then
    echo "precondition failed (now missing): $relative" >&2
    exit 4
  fi
  actual="$(sha256sum "$target" | awk '{print $1}')"
  if [ "$actual" != "$expected" ]; then
    echo "precondition failed (hash changed): $relative" >&2
    exit 4
  fi
done < "$hash_file"

echo "SEARCH_EXPORT_PRECONDITIONS_OK"
