#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'EOF'
This legacy corpus-repair cutover is disabled under the immutable release layout.

It assembled a second mutable source tree and changed Caddy directly. Do not
run it against production. Apply reviewed repair outputs through a dedicated
data-only transaction that owns /run/lock/marx-search-release.lock, or commit
the corresponding configuration and use deploy/release.ps1.
EOF
exit 64
