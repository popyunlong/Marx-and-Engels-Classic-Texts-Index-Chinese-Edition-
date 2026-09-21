#!/usr/bin/env bash
set -euo pipefail
echo "This legacy cutover entry point is disabled." >&2
echo "Build with deploy/release.ps1; promotion must pass deploy/promote_release.sh under the global release lock." >&2
exit 64
