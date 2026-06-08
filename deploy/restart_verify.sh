#!/usr/bin/env bash
# Runs ON the server. Restart marx-search to load new code + rebuilt corpus,
# verify health with retries, and auto-rollback code from backup if unhealthy.
set -uo pipefail
BACKUP="/opt/marx-search.cloud-backup.20260606-101614"
RT="http://127.0.0.1:8000/api/runtime"

wait_health() {
  for i in $(seq 1 20); do
    if curl -fsS --max-time 5 "$RT" >/dev/null 2>&1; then return 0; fi
    sleep 3
  done
  return 1
}

echo "Restarting marx-search ..."
systemctl restart marx-search

if wait_health && systemctl is-active --quiet marx-search; then
  echo "HEALTH_OK"
  echo "--- /api/runtime ---"
  curl -fsS --max-time 5 "$RT"; echo
  exit 0
fi

echo "HEALTH_FAIL — attempting code rollback from $BACKUP"
echo "--- journal (pre-rollback) ---"
journalctl -u marx-search -n 25 --no-pager
if [ -d "$BACKUP" ]; then
  cp -a "$BACKUP/." /opt/marx-search/ && systemctl restart marx-search
  if wait_health && systemctl is-active --quiet marx-search; then
    echo "ROLLBACK_HEALTH_OK"
    exit 2
  fi
  echo "ROLLBACK_HEALTH_FAIL"
  journalctl -u marx-search -n 25 --no-pager
fi
exit 1
