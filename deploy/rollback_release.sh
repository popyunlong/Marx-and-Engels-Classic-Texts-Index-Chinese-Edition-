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
CADDYFILE="${MARX_CADDYFILE:-/etc/caddy/Caddyfile}"
PRIMARY_PORT="${MARX_PRIMARY_PORT:-8000}"
CANDIDATE_PORT="${MARX_CANDIDATE_PORT:-8001}"
DRAIN_TIMEOUT_SECONDS="${MARX_DEPLOY_DRAIN_TIMEOUT_SECONDS:-720}"
HEALTH_RETRIES="${MARX_DEPLOY_HEALTH_RETRIES:-90}"
CANDIDATE_UNIT="marx-search-rollback-${TARGET_RELEASE//[^A-Za-z0-9_.-]/-}.service"
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
python3 - "$LEDGER" "$CURRENT" "$TARGET_RELEASE" <<'PY'
import datetime, json, pathlib, sys
entry = {
    "event": "rollback_attempt",
    "from_release_id": sys.argv[2],
    "target_release_id": sys.argv[3],
    "at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
}
with pathlib.Path(sys.argv[1]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
PY
TRANSACTION_APP="$(readlink -f "$APP_ROOT/current/app")"
source "$TRANSACTION_APP/deploy/release_traffic.sh"
python3 "$TRANSACTION_APP/scripts/catalog_deploy.py" rollback --root "$APP_ROOT" --app "$TARGET/app"
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
  # After a failed primary restart, send new traffic to the healthy rollback
  # candidate and wait for every accepted primary request to finish first.
  if grep -Eq "reverse_proxy[[:space:]]+127\\.0\\.0\\.1:${PRIMARY_PORT}([[:space:]]|$)" "$CADDYFILE"; then
    if ! switch_caddy "$PRIMARY_PORT" "$CANDIDATE_PORT" || ! drain_port "$PRIMARY_PORT" "failed rollback primary"; then
      echo "CRITICAL: cannot safely drain failed rollback primary; preserving both instances" >&2
      return 1
    fi
  fi
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
  if wait_runtime "$PRIMARY_PORT" "$OLD_REAL/release.json" && switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT"; then
    retire_candidate_if_drained "failed rollback candidate" || true
  else
    echo "CRITICAL: predecessor failed; preserving candidate and traffic route" >&2
  fi
}

runtime_healthy() {
  local port="$1" metadata="$2" runtime_json
  runtime_json="$(curl -fsS --max-time 6 "http://127.0.0.1:$port/api/runtime")" || return 1
  printf '%s' "$runtime_json" | python3 "$TRANSACTION_APP/scripts/catalog_deploy.py" health --metadata "$metadata" || return 1
  local endpoint
  for endpoint in / /pricing /ai /v2/read; do
    curl -fsS --max-time 10 "http://127.0.0.1:$port$endpoint" >/dev/null || return 1
  done
}

wait_runtime() {
  local i
  for i in $(seq 1 "$HEALTH_RETRIES"); do
    runtime_healthy "$1" "$2" && return 0
    sleep 2
  done
  return 1
}

target_healthy() {
  runtime_healthy "$PRIMARY_PORT" "$TARGET/release.json"
}

# Rehearse the immutable target before touching current or the primary service.
# An occupied candidate port is a deferred prior release: never kill it to proceed.
if ss -Hltn "( sport = :${CANDIDATE_PORT} )" | grep -q .; then
  echo "candidate port occupied; retire the drained previous candidate first" >&2
  exit 75
fi
systemd-run --unit="${CANDIDATE_UNIT%.service}" \
  --property=Type=exec --property=User=www-data --property=Group=www-data \
  --property="WorkingDirectory=$TARGET/app" --property=EnvironmentFile=-/etc/marx-search.env \
  --property="ReadOnlyPaths=$TARGET" \
  --setenv=PYTHONUNBUFFERED=1 --setenv="PYTHONPATH=$TARGET/app:$TARGET/.deps" \
  --setenv="PYTHONPYCACHEPREFIX=$APP_ROOT/runtime-cache" \
  --setenv="APP_RELEASE_FILE=$TARGET/release.json" \
  --setenv="MARX_RUNTIME_DATA_DIR=$APP_ROOT/data" --setenv="MARX_RUNTIME_PDF_DIR=$APP_ROOT/pdfs" \
  --setenv="MARX_RUNTIME_LOG_DIR=$APP_ROOT/logs" \
  --setenv="MARX_RUNTIME_STATIC_LIBRARY_DIR=$APP_ROOT/static_library" \
  --setenv="MARX_RUNTIME_STREAM_LIBRARY_DIR=$APP_ROOT/stream_library" \
  --setenv="MARX_AI_CONFIG_FILE=$APP_ROOT/config/ai.yaml" \
  --setenv="MARX_ALIPAY_CONFIG_FILE=$APP_ROOT/config/alipay.yaml" \
  --setenv="MARX_ZPAY_CONFIG_FILE=$APP_ROOT/config/zpay.yaml" \
  "$APP_ROOT/runtime-python" -m ingestion.runtime --port "$CANDIDATE_PORT" >/dev/null
if ! wait_runtime "$CANDIDATE_PORT" "$TARGET/release.json"; then
  retire_candidate_if_drained "unhealthy rollback candidate" || true
  exit 5
fi
if ! switch_caddy "$PRIMARY_PORT" "$CANDIDATE_PORT"; then
  retire_candidate_if_drained "uncut rollback candidate" || true
  exit 5
fi
if ! drain_port "$PRIMARY_PORT" "pre-rollback primary"; then
  if switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT"; then
    retire_candidate_if_drained "aborted rollback candidate" || true
  fi
  exit 9
fi

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
for _ in $(seq 1 "$HEALTH_RETRIES"); do
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
if ! switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT"; then
  restore_predecessor
  exit 7
fi
if ! target_healthy; then
  restore_predecessor
  exit 8
fi
retire_candidate_if_drained "completed rollback candidate" || true
systemctl disable --now marx-corpus-repair-promote.timer >/dev/null 2>&1 || true
systemctl stop marx-corpus-repair-promote.service >/dev/null 2>&1 || true
ln -sfn -- "$OLD_REAL" "$APP_ROOT/previous"
printf '%s\n' "$TARGET_RELEASE" > "$APP_ROOT/DEPLOYED_SHA.tmp"
mv -f -- "$APP_ROOT/DEPLOYED_SHA.tmp" "$APP_ROOT/DEPLOYED_SHA"
python3 - "$LEDGER" "$CURRENT" "$TARGET_RELEASE" "$TARGET/release.json" <<'PY'
import datetime, json, pathlib, sys
entry = {
    "event": "rollback",
    "from_release_id": sys.argv[2],
    "target_release_id": sys.argv[3],
    "catalog_release": json.loads(pathlib.Path(sys.argv[4]).read_text(encoding="utf-8")).get("catalog_release"),
    "at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
}
with pathlib.Path(sys.argv[1]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
PY
trap - ERR
echo "RELEASE_ROLLED_BACK=$TARGET_RELEASE"
