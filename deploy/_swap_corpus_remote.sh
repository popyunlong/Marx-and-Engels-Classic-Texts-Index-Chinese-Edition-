#!/usr/bin/env bash
# Runs ON the server. Verify uploaded corpus.sqlite, backup current, atomic swap,
# restart marx-search, health-check, auto-rollback DB on failure.
set -uo pipefail
RD=/opt/marx-search
DB="$RD/data/corpus.sqlite"
SHA="$DB.sha256"
UP=/tmp/corpus.sqlite.upload
UPSHA=/tmp/corpus.sqlite.upload.sha256
RT="http://127.0.0.1:8000/api/runtime"
S=$(date +%Y%m%d-%H%M%S)
BK="$DB.bak-$S"

wait_health() {
  for i in $(seq 1 20); do
    if curl -fsS --max-time 5 "$RT" >/dev/null 2>&1; then return 0; fi
    sleep 3
  done
  return 1
}

# 1) verify uploaded sha256
calc=$(sha256sum "$UP" | awk '{print $1}')
want=$(awk '{print $1}' "$UPSHA")
if [ "$calc" != "$want" ]; then echo "SHA_MISMATCH calc=$calc want=$want"; exit 1; fi
echo "SHA_OK $calc"

# 2) backup current DB + sidecar
cp -a "$DB" "$BK" && cp -a "$SHA" "$BK.sha256" && echo "BACKUP $BK"
owner=$(stat -c '%U:%G' "$DB")

# 3) atomic swap + chown
mv "$UP" "$DB" && mv "$UPSHA" "$SHA" && chown "$owner" "$DB" "$SHA" && echo "SWAPPED owner=$owner"

# 4) restart + health
echo "Restarting marx-search ..."
systemctl restart marx-search
if wait_health && systemctl is-active --quiet marx-search; then
  echo "HEALTH_OK"
  echo "--- /api/runtime ---"
  curl -fsS --max-time 5 "$RT"; echo
  exit 0
fi

# 5) rollback
echo "HEALTH_FAIL — rolling back DB from $BK"
journalctl -u marx-search -n 25 --no-pager
cp -a "$BK" "$DB" && cp -a "$BK.sha256" "$SHA" && chown "$owner" "$DB" "$SHA"
systemctl restart marx-search
if wait_health; then echo "ROLLBACK_HEALTH_OK"; else echo "ROLLBACK_HEALTH_FAIL"; fi
exit 1
