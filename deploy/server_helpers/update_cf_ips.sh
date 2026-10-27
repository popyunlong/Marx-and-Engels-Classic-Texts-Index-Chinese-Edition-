#!/usr/bin/env bash
# Compatibility launcher. The reviewed immutable release is authoritative.
set -euo pipefail
exec /bin/bash /opt/marx-search/current/app/deploy/update_cf_ips.sh "$@"
