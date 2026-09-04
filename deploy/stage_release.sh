#!/usr/bin/env bash
# Build a release tree without changing the currently serving application
# directory. Patched files become real files in RELEASE; untouched files are
# symlinks back to the live tree.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: stage_release.sh APP_DIR RELEASE_DIR PATCH_ARCHIVE" >&2
  exit 2
fi

APP_DIR="$(readlink -f "$1")"
RELEASE_DIR="$2"
PATCH_ARCHIVE="$(readlink -f "$3")"

case "$RELEASE_DIR" in
  "$APP_DIR"|"$APP_DIR"/*)
    echo "Release directory must not be the live application directory or one of its children." >&2
    exit 2
    ;;
esac
if [ ! -d "$APP_DIR" ] || [ ! -f "$PATCH_ARCHIVE" ] || [ -e "$RELEASE_DIR" ]; then
  echo "Invalid or already-existing release inputs." >&2
  exit 2
fi

mkdir -p "$RELEASE_DIR"
PATCH_DIR=""
cleanup_on_error() {
  local status=$?
  if [ -n "$PATCH_DIR" ]; then rm -rf -- "$PATCH_DIR"; fi
  if [ "$status" -ne 0 ]; then rm -rf -- "$RELEASE_DIR"; fi
  exit "$status"
}
trap cleanup_on_error EXIT

# These directories contain patch targets, so make their directory structure
# real while symlinking individual old files. Large untouched trees such as
# pdfs, static_library and .venv remain a single top-level symlink.
overlay_dirs=" templates static deploy scripts config data tests vendor "
while IFS= read -r -d '' source; do
  name="$(basename "$source")"
  destination="$RELEASE_DIR/$name"
  if [ -d "$source" ] && [[ "$overlay_dirs" == *" $name "* ]]; then
    mkdir -p "$destination"
    cp -a -s -- "$source/." "$destination/"
  else
    ln -s -- "$source" "$destination"
  fi
done < <(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print0)

# Extract into a private tree first.  Extracting an archive directly over the
# candidate with --unlink-first also tries to unlink real directory entries
# (for example ./config), which GNU tar rejects.  Copying only non-directory
# entries after creating the directory skeleton replaces the candidate's file
# symlinks without ever following them back into the live tree.
PATCH_DIR="$(mktemp -d)"
tar --extract --gzip --file "$PATCH_ARCHIVE" --directory "$PATCH_DIR"

while IFS= read -r -d '' source; do
  relative="${source#"$PATCH_DIR"/}"
  mkdir -p -- "$RELEASE_DIR/$relative"
done < <(find "$PATCH_DIR" -mindepth 1 -type d -print0)

while IFS= read -r -d '' source; do
  relative="${source#"$PATCH_DIR"/}"
  destination="$RELEASE_DIR/$relative"
  rm -f -- "$destination"
  cp -a -- "$source" "$destination"
done < <(find "$PATCH_DIR" -mindepth 1 ! -type d -print0)

rm -rf -- "$PATCH_DIR"
PATCH_DIR=""

# Bytecode caches copied as symlinks would make py_compile refuse to replace
# them (and must never point writes back into the live tree).  Candidate
# validation recreates fresh caches locally as needed.
find "$RELEASE_DIR" -type d -name __pycache__ -prune -exec rm -rf -- {} +

for required in app.py serve.py membership.py templates/pricing.html static/ai-page/ai-page.js; do
  if [ ! -f "$RELEASE_DIR/$required" ]; then
    echo "Staged release is missing $required" >&2
    exit 3
  fi
done

trap - EXIT
echo "$RELEASE_DIR"
