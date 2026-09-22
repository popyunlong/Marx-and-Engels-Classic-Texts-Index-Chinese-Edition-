#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

if [ "$#" -ne 3 ]; then
  echo "usage: rollback_release.sh APP_ROOT EXPECTED_CURRENT TARGET_RELEASE" >&2
  exit 2
fi
APP_ROOT="$1"
EXPECTED_CURRENT="$2"
TARGET_RELEASE="$3"
LOCK_FILE="/run/lock/marx-search-release.lock"
LEDGER="$APP_ROOT/release-ledger.jsonl"
TARGET="$APP_ROOT/releases/$TARGET_RELEASE"
MAIN_SERVICE="${MARX_MAIN_SERVICE:-marx-search.service}"
MANAGED_SUPPORT_UNITS=(
  marx-corpus-repair.service marx-corpus-repair.timer
  marx-corpus-repair-promote.service marx-corpus-repair-promote.timer
  marx-search-backup.service marx-search-backup.timer
  marx-search-cfip-sync.service marx-search-cfip-sync.timer
  marx-search-citation-agent-test-worker.service marx-search-citation-worker.service
  marx-search-journal-alerts.service marx-search-journal-alerts.timer
  marx-search-journal-process.service marx-search-journal-process.timer
  marx-search-journal-send.service marx-search-journal-send.timer
  marx-search-watchdog.service marx-search-watchdog.timer
)
OPTIONAL_PRODUCTION_UNITS=(
  marx-ocr-guard.service marx-ocr.service marx-search-ai-sync.service
  marx-search-corpus-ocr-audit.service marx-search-philosophy-ocr.service
)

for value in "$EXPECTED_CURRENT" "$TARGET_RELEASE"; do
  [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._:+-]{0,191}$ ]] || { echo "unsafe release id" >&2; exit 2; }
done
[ "$(id -u)" -eq 0 ] || { echo "rollback must run as root" >&2; exit 2; }
case "$APP_ROOT" in /) echo "APP_ROOT must not be the filesystem root" >&2; exit 2;; /*) ;; *) echo "APP_ROOT must be absolute" >&2; exit 2;; esac
[ -f "$TARGET/release.json" ] || { echo "target release does not exist" >&2; exit 3; }
[ -f "$TARGET/app/deploy/marx-search.service" ] || { echo "target canonical service is missing" >&2; exit 3; }
for unit in "${MANAGED_SUPPORT_UNITS[@]}"; do
  [ -f "$TARGET/app/deploy/$unit" ] || { echo "target release missing managed unit $unit" >&2; exit 3; }
done
for unit in "${OPTIONAL_PRODUCTION_UNITS[@]}"; do
  [ -f "$TARGET/app/deploy/production_units/$unit" ] || { echo "target release missing optional unit $unit" >&2; exit 3; }
done
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "another production release or rollback owns $LOCK_FILE" >&2; exit 75; }

CURRENT="$(python3 - "$APP_ROOT/current/release.json" <<'PY'
import json, pathlib, sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["release_id"])
PY
)"
[ "$CURRENT" = "$EXPECTED_CURRENT" ] || { echo "stale rollback rejected: live is $CURRENT" >&2; exit 73; }
[ "$TARGET_RELEASE" != "$CURRENT" ] || { echo "target is already current" >&2; exit 3; }
TARGET_META_ID="$(python3 - "$TARGET/release.json" <<'PY'
import json, pathlib, re, sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("schema_version") != 1:
    raise SystemExit("unsupported target release schema")
for key, pattern in {
    "release_id": r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
    "git_sha": r"[0-9a-f]{40}",
    "source_tree_sha256": r"[0-9a-f]{64}",
}.items():
    if not re.fullmatch(pattern, str(payload.get(key) or "")):
        raise SystemExit("invalid target release metadata: " + key)
print(payload["release_id"])
PY
)"
[ "$TARGET_META_ID" = "$TARGET_RELEASE" ] || { echo "target release id mismatch" >&2; exit 3; }
python3 "$TARGET/app/scripts/build_release_manifest.py" verify \
  --source-dir "$TARGET/app" --metadata "$TARGET/release.json" --release-id "$TARGET_RELEASE"

OLD_REAL="$(readlink -f "$APP_ROOT/current")"
SERVICE_BACKUP="$(mktemp -d /run/marx-search-rollback.XXXXXX)"
trap 'rm -rf -- "$SERVICE_BACKUP"' EXIT
tar -czf "$SERVICE_BACKUP/systemd.tar.gz" -C /etc/systemd/system \
  --ignore-failed-read marx-search.service marx-search.service.d \
  "${MANAGED_SUPPORT_UNITS[@]}" "${OPTIONAL_PRODUCTION_UNITS[@]}" 2>/dev/null || true
OPTIONAL_UNITS_PRESENT=()
for unit in "${OPTIONAL_PRODUCTION_UNITS[@]}"; do
  if [ -e "/etc/systemd/system/$unit" ] || [ -L "/etc/systemd/system/$unit" ]; then
    OPTIONAL_UNITS_PRESENT+=("$unit")
  fi
done
ACTIVE_CITATION_WORKERS=()
for unit in marx-search-citation-worker.service marx-search-citation-agent-test-worker.service; do
  if systemctl is-active --quiet "$unit"; then
    ACTIVE_CITATION_WORKERS+=("$unit")
  fi
done

restart_active_citation_workers() {
  local unit
  for unit in "${ACTIVE_CITATION_WORKERS[@]}"; do
    echo "gracefully restarting $unit for the selected release"
    systemctl restart "$unit" || return 1
  done
}

restore_predecessor() {
  trap - ERR
  set +e
  ln -s -- "$OLD_REAL" "$APP_ROOT/.current-rollback-restore"
  mv -Tf -- "$APP_ROOT/.current-rollback-restore" "$APP_ROOT/current"
  rm -f -- /etc/systemd/system/marx-search.service
  rm -rf -- /etc/systemd/system/marx-search.service.d
  for unit in "${MANAGED_SUPPORT_UNITS[@]}" "${OPTIONAL_PRODUCTION_UNITS[@]}"; do
    rm -f -- "/etc/systemd/system/$unit"
  done
  if [ -s "$SERVICE_BACKUP/systemd.tar.gz" ]; then
    tar -xzf "$SERVICE_BACKUP/systemd.tar.gz" -C /etc/systemd/system
  fi
  systemctl daemon-reload
  systemctl restart "$MAIN_SERVICE" || true
  restart_active_citation_workers || true
}

target_healthy() {
  local runtime_json
  runtime_json="$(curl -fsS --max-time 6 http://127.0.0.1:8000/api/runtime)" || return 1
  printf '%s' "$runtime_json" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
raise SystemExit(0 if ((payload.get("app_release") or {}).get("id") or "") == sys.argv[1] else 1)
' "$TARGET_RELEASE" || return 1
  curl -fsS --max-time 10 http://127.0.0.1:8000/ >/dev/null \
    && curl -fsS --max-time 10 http://127.0.0.1:8000/pricing >/dev/null \
    && curl -fsS --max-time 10 http://127.0.0.1:8000/ai >/dev/null \
    && curl -fsS --max-time 10 http://127.0.0.1:8000/v2/read >/dev/null
}

trap 'restore_predecessor; exit 4' ERR
ln -s -- "$TARGET" "$APP_ROOT/.current-rollback-$TARGET_RELEASE"
mv -Tf -- "$APP_ROOT/.current-rollback-$TARGET_RELEASE" "$APP_ROOT/current"
install -o root -g root -m 0644 "$TARGET/app/deploy/marx-search.service" /etc/systemd/system/marx-search.service
rm -rf -- /etc/systemd/system/marx-search.service.d
for unit in "${MANAGED_SUPPORT_UNITS[@]}"; do
  install -o root -g root -m 0644 "$TARGET/app/deploy/$unit" "/etc/systemd/system/$unit"
done
for unit in "${OPTIONAL_UNITS_PRESENT[@]}"; do
  install -o root -g root -m 0644 "$TARGET/app/deploy/production_units/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
if ! systemctl restart "$MAIN_SERVICE"; then
  restore_predecessor
  exit 4
fi
for _ in $(seq 1 45); do
  if target_healthy; then
    break
  fi
  sleep 2
done
if ! target_healthy; then
  restore_predecessor
  echo "target failed health checks; current release restored" >&2
  exit 5
fi
if ! restart_active_citation_workers; then
  restore_predecessor
  echo "an active citation worker failed to restart; current release restored" >&2
  exit 6
fi
systemctl disable --now marx-corpus-repair-promote.timer >/dev/null 2>&1 || true
systemctl stop marx-corpus-repair-promote.service >/dev/null 2>&1 || true
ln -sfn -- "$OLD_REAL" "$APP_ROOT/previous"
printf '%s\n' "$TARGET_RELEASE" > "$APP_ROOT/DEPLOYED_SHA.tmp"
mv -f -- "$APP_ROOT/DEPLOYED_SHA.tmp" "$APP_ROOT/DEPLOYED_SHA"
python3 - "$LEDGER" "$CURRENT" "$TARGET_RELEASE" <<'PY'
import datetime, json, pathlib, sys
entry = {
    "event": "rollback",
    "from_release_id": sys.argv[2],
    "target_release_id": sys.argv[3],
    "at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
}
with pathlib.Path(sys.argv[1]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
PY
trap - ERR
echo "RELEASE_ROLLED_BACK=$TARGET_RELEASE"
