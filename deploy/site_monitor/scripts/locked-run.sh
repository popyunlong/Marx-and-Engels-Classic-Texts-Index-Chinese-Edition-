#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
mkdir -p "${MONITOR_REPORT_DIR:-reports}"
exec 9>"${MONITOR_REPORT_DIR:-reports}/run.lock"
flock -n -E 75 9 || exit 75
export MONITOR_LOCK_HELD=1
# timeout kills the worker process group, including its browser, on a hard deadline.
exec timeout --signal=TERM --kill-after=5s 250s node src/worker.js "$@"
