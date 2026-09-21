#!/usr/bin/env bash
set -euo pipefail
echo "Mutable-tree restart/restore is disabled." >&2
echo "Use deploy/release.ps1 for promotion or deploy/rollback_release.ps1 for an audited rollback." >&2
exit 64
