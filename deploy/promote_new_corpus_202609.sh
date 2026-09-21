#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'EOF'
This historical corpus cutover is disabled.

It copied mutable application source, rewrote tracked configuration beside the
running service, and changed Caddy outside the immutable release transaction.
Commit reviewed corpus configuration to main, advance production, and use
deploy/release.ps1. A future data-only transaction must use
/run/lock/marx-search-release.lock and may replace only explicitly shared data.
EOF
exit 64
