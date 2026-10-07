#!/usr/bin/env bash
# SSH observes the coordinator. systemd owns it, so losing the observer cannot
# interrupt candidate cleanup or leave the watchdog paused.
set -Eeuo pipefail
[ "$#" -eq 7 ] || [ "$#" -eq 8 ] || { echo 'expected seven or eight release arguments' >&2; exit 2; }
[[ "$4" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || exit 2
UNIT="marx-search-release-$4"
command -v systemd-run >/dev/null
command -v journalctl >/dev/null
WAITER=''
FOLLOWER=''
cleanup_observers() {
  # Stopping these clients does not stop the systemd-owned transaction.
  [ -z "$FOLLOWER" ] || kill "$FOLLOWER" 2>/dev/null || true
  [ -z "$WAITER" ] || kill "$WAITER" 2>/dev/null || true
}
trap cleanup_observers EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
systemd-run --unit="$UNIT" --wait --collect \
  --property=Type=exec --property=StandardOutput=journal --property=StandardError=journal \
  /bin/bash -c 'set -o pipefail; tar -xOf "$2" app/deploy/promote_release.sh | nice -n 15 ionice -c 3 bash -s -- "$@"' -- "$@" &
WAITER=$!
journalctl --follow --unit="$UNIT.service" --no-pager --output=cat &
FOLLOWER=$!
RESULT=0
wait "$WAITER" || RESULT=$?
WAITER=''
exit "$RESULT"
