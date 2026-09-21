#!/usr/bin/env bash
set -euo pipefail
echo "Incremental release overlays are disabled." >&2
echo "Use deploy/release.ps1 to build a complete archive from a committed production revision." >&2
exit 64
