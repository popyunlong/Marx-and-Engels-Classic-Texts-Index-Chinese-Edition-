#!/usr/bin/env bash
# Compatibility guard: managed units are installed by the immutable release
# transaction so their source paths and rollback state cannot drift.
set -euo pipefail

echo "This installer is retired. Publish deploy units with deploy/release.ps1." >&2
echo "After a successful release, explicitly enable only marx-corpus-repair.timer if required." >&2
echo "The legacy corpus-promotion timer must remain disabled." >&2
exit 64
