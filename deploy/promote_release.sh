#!/usr/bin/env bash
# Immutable, compare-and-swap production promotion. This file is streamed from
# the uploaded archive; every state-changing operation stays under one flock.
set -Eeuo pipefail
umask 027

if [ "$#" -ne 6 ]; then
  echo "usage: promote_release.sh APP_ROOT ARCHIVE EXPECTED_LIVE RELEASE_ID CATALOG_ARCHIVE REVIEW_NONCE" >&2
  exit 2
fi

APP_ROOT="$1"
ARCHIVE="$2"
EXPECTED_LIVE="$3"
RELEASE_ID="$4"
CATALOG_ARCHIVE="${5:-}"
REVIEW_NONCE="${6:-}"
RELEASES="$APP_ROOT/releases"
LOCK_FILE="/run/lock/marx-search-release.lock"
LEDGER="$APP_ROOT/release-ledger.jsonl"
CADDYFILE="${MARX_CADDYFILE:-/etc/caddy/Caddyfile}"
MAIN_SERVICE="${MARX_MAIN_SERVICE:-marx-search.service}"
PRIMARY_PORT="${MARX_PRIMARY_PORT:-8000}"
CANDIDATE_PORT="${MARX_CANDIDATE_PORT:-8001}"
# Loading the production corpus can take a little over a minute while both the
# candidate and primary coexist. Keep serving the healthy side of the cutover
# while allowing the replacement up to three minutes to become ready.
HEALTH_RETRIES="${MARX_DEPLOY_HEALTH_RETRIES:-90}"
DRAIN_TIMEOUT_SECONDS="${MARX_DEPLOY_DRAIN_TIMEOUT_SECONDS:-720}"
CANDIDATE_UNIT="marx-search-candidate-${RELEASE_ID//[^A-Za-z0-9_.-]/-}.service"
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

if [ "$(id -u)" -ne 0 ]; then
  echo "promotion must run as root" >&2
  exit 2
fi
case "$APP_ROOT" in /) echo "APP_ROOT must not be the filesystem root" >&2; exit 2;; /*) ;; *) echo "APP_ROOT must be absolute" >&2; exit 2;; esac
if ! [[ "$RELEASE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "unsafe release id" >&2
  exit 2
fi
if ! [[ "$EXPECTED_LIVE" =~ ^[A-Za-z0-9][A-Za-z0-9._:+-]{0,191}$ ]]; then
  echo "unsafe parent release id" >&2
  exit 2
fi
if ! [[ "$REVIEW_NONCE" =~ ^[0-9a-f]{32}$ ]]; then
  echo "unsafe candidate review nonce" >&2
  exit 2
fi
if ! [[ "$DRAIN_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "MARX_DEPLOY_DRAIN_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
fi
for command in flock python3 tar curl systemctl systemd-run caddy sha256sum ss; do
  command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 2; }
done
[ -f "$ARCHIVE" ] || { echo "release archive not found" >&2; exit 2; }
[ -f "$CADDYFILE" ] || { echo "Caddyfile not found" >&2; exit 2; }

mkdir -p "$RELEASES"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "another production release or rollback owns $LOCK_FILE" >&2
  exit 75
fi

read_release_id() {
  local metadata="$1"
  python3 - "$metadata" <<'PY'
import json, pathlib, sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["release_id"])
PY
}

current_release_id() {
  if [ -f "$APP_ROOT/current/release.json" ]; then
    read_release_id "$APP_ROOT/current/release.json"
  elif [ -s "$APP_ROOT/DEPLOYED_SHA" ]; then
    tr -d '\r\n' < "$APP_ROOT/DEPLOYED_SHA"
  else
    echo "unversioned"
  fi
}

LIVE_BEFORE="$(current_release_id)"
if [ "$LIVE_BEFORE" != "$EXPECTED_LIVE" ]; then
  echo "stale release rejected: expected '$EXPECTED_LIVE', live is '$LIVE_BEFORE'" >&2
  exit 73
fi
FINAL="$RELEASES/$RELEASE_ID"
[ ! -e "$FINAL" ] || { echo "release directory already exists: $FINAL" >&2; exit 3; }
STAGING="$RELEASES/.staging-$RELEASE_ID"
rm -rf -- "$STAGING"
mkdir -p "$STAGING"
KEEP_FINAL=0
cleanup_incomplete() {
  if [ "${REVIEW_OWNED:-0}" -eq 1 ]; then rm -f -- "$REVIEW_FIFO"; fi
  rm -rf -- "$STAGING"
  if [ "$KEEP_FINAL" -eq 0 ] && [ -d "$FINAL" ]; then
    rm -rf -- "$FINAL"
  fi
}
trap cleanup_incomplete EXIT

python3 - "$ARCHIVE" <<'PY'
import pathlib, sys, tarfile
with tarfile.open(sys.argv[1], "r:gz") as archive:
    for member in archive.getmembers():
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit("unsafe archive member: " + member.name)
        if member.issym() or member.islnk():
            target = pathlib.PurePosixPath(member.linkname)
            if target.is_absolute() or ".." in target.parts:
                raise SystemExit("unsafe archive link: " + member.name)
PY
tar -xzf "$ARCHIVE" -C "$STAGING" --no-same-owner --no-same-permissions
for required in release.json app/app.py app/ingestion/runtime.py app/deploy/marx-search.service app/scripts/build_release_manifest.py; do
  [ -f "$STAGING/$required" ] || { echo "release missing $required" >&2; exit 3; }
done
for unit in "${MANAGED_SUPPORT_UNITS[@]}"; do
  [ -f "$STAGING/app/deploy/$unit" ] || { echo "release missing managed unit $unit" >&2; exit 3; }
done
for unit in "${OPTIONAL_PRODUCTION_UNITS[@]}"; do
  [ -f "$STAGING/app/deploy/production_units/$unit" ] || { echo "release missing optional unit $unit" >&2; exit 3; }
done
python3 "$STAGING/app/scripts/build_release_manifest.py" verify \
  --source-dir "$STAGING/app" --metadata "$STAGING/release.json" --release-id "$RELEASE_ID"
META_PARENT="$(python3 - "$STAGING/release.json" <<'PY'
import json, pathlib, sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["parent_release_id"])
PY
)"
[ "$META_PARENT" = "$EXPECTED_LIVE" ] || { echo "archive parent_release_id mismatch" >&2; exit 73; }
mv -- "$STAGING" "$FINAL"

# Runtime data and secrets stay outside the immutable source tree and are
# injected through explicit environment paths. This keeps the verified source
# hash valid for the entire life of a release, including rollback.
[ -d "$APP_ROOT/data" ] || { echo "shared runtime data directory is missing" >&2; exit 3; }
[ -d "$APP_ROOT/pdfs" ] || { echo "shared runtime PDF directory is missing" >&2; exit 3; }

# Same lock and compare-and-swap transaction as application promotion. No live
# database is replaced, and installed snapshots are never mutated or overwritten.
CATALOG_ARGS=()
if [ -n "$CATALOG_ARCHIVE" ]; then CATALOG_ARGS=(--archive "$CATALOG_ARCHIVE"); fi
python3 "$FINAL/app/scripts/catalog_deploy.py" preflight \
  --root "$APP_ROOT" --app "$FINAL/app" "${CATALOG_ARGS[@]}"

RUNTIME_PYTHON="${MARX_RUNTIME_PYTHON:-}"
if [ -z "$RUNTIME_PYTHON" ]; then
  RUNTIME_PYTHON="$(systemctl show "$MAIN_SERVICE" -p ExecStart --value | grep -oE '/[^ ;{}]+/python([0-9.]*)?' | head -n1 || true)"
fi
if [ -z "$RUNTIME_PYTHON" ] && [ -x "$APP_ROOT/.venv/bin/python" ]; then
  RUNTIME_PYTHON="$APP_ROOT/.venv/bin/python"
fi
[ -x "$RUNTIME_PYTHON" ] || { echo "unable to locate the approved production Python" >&2; exit 3; }
PY_VERSION="$($RUNTIME_PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
case "$PY_VERSION" in 3.10.*) ;; *) echo "production Python must be 3.10.x, got $PY_VERSION" >&2; exit 3;; esac
ln -sfn -- "$RUNTIME_PYTHON" "$APP_ROOT/runtime-python"
install -d -o www-data -g www-data -m 0750 "$APP_ROOT/logs"
install -d -o www-data -g www-data -m 0770 "$APP_ROOT/runtime-cache"

mkdir -p "$FINAL/.deps"
"$RUNTIME_PYTHON" -m pip install --disable-pip-version-check --no-input --no-compile \
  --target "$FINAL/.deps" -r "$FINAL/app/requirements.txt"
PYTHONPYCACHEPREFIX="$FINAL/.pycache" PYTHONPATH="$FINAL/app:$FINAL/.deps" \
  "$RUNTIME_PYTHON" -m compileall -q "$FINAL/app"
chown -R root:www-data "$FINAL"
(
  cd "$FINAL/app"
  PYTHONPATH="$FINAL/app:$FINAL/.deps" APP_RELEASE_FILE="$FINAL/release.json" \
    PYTHONPYCACHEPREFIX="$APP_ROOT/runtime-cache" \
    MARX_RUNTIME_DATA_DIR="$APP_ROOT/data" MARX_RUNTIME_PDF_DIR="$APP_ROOT/pdfs" \
    MARX_RUNTIME_LOG_DIR="$APP_ROOT/logs" \
    MARX_RUNTIME_STATIC_LIBRARY_DIR="$APP_ROOT/static_library" \
    MARX_RUNTIME_STREAM_LIBRARY_DIR="$APP_ROOT/stream_library" \
    MARX_AI_CONFIG_FILE="$APP_ROOT/config/ai.yaml" \
    MARX_ALIPAY_CONFIG_FILE="$APP_ROOT/config/alipay.yaml" \
    MARX_ZPAY_CONFIG_FILE="$APP_ROOT/config/zpay.yaml" \
    "$RUNTIME_PYTHON" scripts/deployment_smoke.py --mode server
)

health() {
  local port="$1" expected_release="${2:-}" runtime_json
  runtime_json="$(curl -fsS --max-time 6 "http://127.0.0.1:${port}/api/runtime")" \
    || return 1
  if [ -n "$expected_release" ]; then
    if [ -f "$RELEASES/$expected_release/release.json" ]; then
      printf '%s' "$runtime_json" | python3 "$FINAL/app/scripts/catalog_deploy.py" health \
        --metadata "$RELEASES/$expected_release/release.json" || return 1
    fi
    printf '%s' "$runtime_json" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
expected = sys.argv[1]
actual = ((payload.get("app_release") or {}).get("id") or "")
raise SystemExit(0 if actual == expected else 1)
' "$expected_release" || return 1
  fi
  curl -fsS --max-time 10 "http://127.0.0.1:${port}/" >/dev/null \
    && curl -fsS --max-time 10 "http://127.0.0.1:${port}/pricing" >/dev/null \
    && curl -fsS --max-time 10 "http://127.0.0.1:${port}/ai" >/dev/null \
    && curl -fsS --max-time 10 "http://127.0.0.1:${port}/v2/ai" >/dev/null \
    && curl -fsS --max-time 10 "http://127.0.0.1:${port}/v2/read" >/dev/null
}

wait_health() {
  local port="$1" expected_release="${2:-}" i
  for i in $(seq 1 "$HEALTH_RETRIES"); do
    health "$port" "$expected_release" && return 0
    sleep 2
  done
  return 1
}

switch_caddy() {
  local from_port="$1" to_port="$2" temp backup
  grep -Eq "reverse_proxy[[:space:]]+127\\.0\\.0\\.1:${from_port}([[:space:]]|$)" "$CADDYFILE" || return 1
  temp="$(mktemp /etc/caddy/Caddyfile.release.XXXXXX)"
  backup="$(mktemp /etc/caddy/Caddyfile.backup.XXXXXX)"
  cp -a "$CADDYFILE" "$backup"
  sed -E "s#(reverse_proxy[[:space:]]+127\\.0\\.0\\.1:)${from_port}([[:space:]]|$)#\\1${to_port}\\2#g" "$CADDYFILE" > "$temp"
  caddy validate --config "$temp" >/dev/null || { rm -f "$temp" "$backup"; return 1; }
  install -o root -g root -m 0644 "$temp" "$CADDYFILE"
  rm -f "$temp"
  if ! systemctl reload caddy; then
    install -o root -g root -m 0644 "$backup" "$CADDYFILE"
    systemctl reload caddy >/dev/null 2>&1 || true
    rm -f "$backup"
    return 1
  fi
  rm -f "$backup"
}

drain_port() {
  local port="$1" label="$2" elapsed=0 connections
  echo "waiting for $label on port $port to drain (timeout=${DRAIN_TIMEOUT_SECONDS}s)"
  while true; do
    if ! connections="$(ss -Htn state established "( sport = :${port} )" 2>/dev/null)"; then
      echo "unable to inspect active connections for $label on port $port" >&2
      return 1
    fi
    [ -z "$connections" ] && break
    if [ "$elapsed" -ge "$DRAIN_TIMEOUT_SECONDS" ]; then
      echo "$label on port $port did not drain within ${DRAIN_TIMEOUT_SECONDS}s" >&2
      return 1
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  echo "$label on port $port drained"
}

retire_candidate_if_drained() {
  local label="$1"
  if ! drain_port "$CANDIDATE_PORT" "$label"; then
    echo "WARNING: $label still has active connections; leaving $CANDIDATE_UNIT running for manual retirement" >&2
    echo "CANDIDATE_RETIREMENT_DEFERRED=$CANDIDATE_UNIT" >&2
    return 1
  fi
  if ! systemctl stop "$CANDIDATE_UNIT" >/dev/null 2>&1; then
    echo "WARNING: failed to stop $CANDIDATE_UNIT; preserving its immutable release" >&2
    echo "CANDIDATE_RETIREMENT_DEFERRED=$CANDIDATE_UNIT" >&2
    return 1
  fi
  if systemctl is-active --quiet "$CANDIDATE_UNIT"; then
    echo "WARNING: $CANDIDATE_UNIT is still active after stop; preserving its immutable release" >&2
    echo "CANDIDATE_RETIREMENT_DEFERRED=$CANDIDATE_UNIT" >&2
    return 1
  fi
  systemctl reset-failed "$CANDIDATE_UNIT" >/dev/null 2>&1 || true
}

abort_before_commit() {
  echo "previous primary did not drain; restoring traffic without restarting or replacing it" >&2
  if ! switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT"; then
    echo "CRITICAL: could not restore the primary route; healthy candidate remains live" >&2
    return 1
  fi
  if ! wait_health "$PRIMARY_PORT" "$EXPECTED_LIVE"; then
    echo "CRITICAL: restored primary route is unhealthy; moving traffic back to the healthy candidate" >&2
    switch_caddy "$PRIMARY_PORT" "$CANDIDATE_PORT" >/dev/null 2>&1 || true
    return 1
  fi
  if retire_candidate_if_drained "aborted release candidate"; then
    KEEP_FINAL=0
  fi
  return 0
}

if ss -Hltn "( sport = :${CANDIDATE_PORT} )" | grep -q .; then
  echo "candidate port occupied; preserve the prior candidate until it drains" >&2
  exit 75
fi
systemctl stop "$CANDIDATE_UNIT" >/dev/null 2>&1 || true
systemd-run --unit="${CANDIDATE_UNIT%.service}" \
  --property=Type=exec --property=User=www-data --property=Group=www-data \
  --property="WorkingDirectory=$FINAL/app" --property=EnvironmentFile=-/etc/marx-search.env \
  --property="ReadOnlyPaths=$FINAL" \
  --setenv=PYTHONUNBUFFERED=1 --setenv="PYTHONPATH=$FINAL/app:$FINAL/.deps" \
  --setenv="PYTHONPYCACHEPREFIX=$APP_ROOT/runtime-cache" \
  --setenv="APP_RELEASE_FILE=$FINAL/release.json" \
  --setenv="MARX_RUNTIME_DATA_DIR=$APP_ROOT/data" --setenv="MARX_RUNTIME_PDF_DIR=$APP_ROOT/pdfs" \
  --setenv="MARX_RUNTIME_LOG_DIR=$APP_ROOT/logs" \
  --setenv="MARX_RUNTIME_STATIC_LIBRARY_DIR=$APP_ROOT/static_library" \
  --setenv="MARX_RUNTIME_STREAM_LIBRARY_DIR=$APP_ROOT/stream_library" \
  --setenv="MARX_AI_CONFIG_FILE=$APP_ROOT/config/ai.yaml" \
  --setenv="MARX_ALIPAY_CONFIG_FILE=$APP_ROOT/config/alipay.yaml" \
  --setenv="MARX_ZPAY_CONFIG_FILE=$APP_ROOT/config/zpay.yaml" \
  "$RUNTIME_PYTHON" -m ingestion.runtime --port "$CANDIDATE_PORT" >/dev/null
if ! wait_health "$CANDIDATE_PORT" "$RELEASE_ID"; then
  journalctl -u "$CANDIDATE_UNIT" -n 80 --no-pager >&2 || true
  retire_candidate_if_drained "unhealthy candidate" || KEEP_FINAL=1
  echo "candidate failed; live traffic was not changed" >&2
  exit 4
fi

# A named pipe lets the coordinator inspect the live candidate through an SSH
# tunnel while this release transaction continues to own the global lock. Only
# a matching release id and one-use nonce lets new traffic cross the cutover.
if [ -n "$REVIEW_NONCE" ]; then
  source "$FINAL/app/deploy/review_gate.sh"
  if ! review_candidate; then
    retire_candidate_if_drained "rejected candidate" || KEEP_FINAL=1
    echo "candidate review rejected; primary unchanged" >&2
    exit 4
  fi
  if ! health "$CANDIDATE_PORT" "$RELEASE_ID"; then
    retire_candidate_if_drained "candidate changed during review" || KEEP_FINAL=1
    echo "candidate became unhealthy during review; primary unchanged" >&2
    exit 4
  fi
fi

# Register this already-validated version before it can appear in a reader URL.
# Retain the receipt even after a failed cutover so an opened tab stays readable.
if ! python3 "$FINAL/app/scripts/catalog_deploy.py" accept --root "$APP_ROOT" --app "$FINAL/app"; then
  retire_candidate_if_drained "unaccepted catalogue candidate" || KEEP_FINAL=1
  exit 4
fi
if ! switch_caddy "$PRIMARY_PORT" "$CANDIDATE_PORT"; then
  retire_candidate_if_drained "uncut candidate" || KEEP_FINAL=1
  echo "candidate was healthy but Caddy cutover failed; primary is unchanged" >&2
  exit 5
fi
# From this point Caddy may be serving files from FINAL. Never remove the
# release directory in an error trap; the explicit rollback path restores the
# predecessor before stopping the candidate.
KEEP_FINAL=1

# Caddy sends every new request to the candidate now.  Do not restart the old
# primary until its already accepted requests (notably long-running AI SSE
# answers) have completed.  A timeout aborts before current/systemd are touched.
if ! drain_port "$PRIMARY_PORT" "previous primary"; then
  if abort_before_commit; then
    exit 9
  fi
  exit 10
fi

OLD_CURRENT=""
if [ -L "$APP_ROOT/current" ]; then OLD_CURRENT="$(readlink -f "$APP_ROOT/current")"; fi
SYSTEMD_BACKUP="$FINAL/systemd-before.tar.gz"
tar -czf "$SYSTEMD_BACKUP" -C /etc/systemd/system \
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

rollback_primary() {
  trap - ERR
  set +e
  # A post-cutover health failure can occur while the primary still has live
  # requests. Keep the healthy candidate until this check and drain both hops.
  if grep -Eq "reverse_proxy[[:space:]]+127\\.0\\.0\\.1:${PRIMARY_PORT}([[:space:]]|$)" "$CADDYFILE"; then
    if ! switch_caddy "$PRIMARY_PORT" "$CANDIDATE_PORT" || ! drain_port "$PRIMARY_PORT" "failed primary"; then
      echo "CRITICAL: cannot safely drain the failed primary; preserving both instances" >&2
      return 1
    fi
  fi
  echo "new primary failed; restoring the direct predecessor" >&2
  rm -f -- /etc/systemd/system/marx-search.service
  rm -rf -- /etc/systemd/system/marx-search.service.d
  for unit in "${MANAGED_SUPPORT_UNITS[@]}" "${OPTIONAL_PRODUCTION_UNITS[@]}"; do
    rm -f -- "/etc/systemd/system/$unit"
  done
  if [ -s "$SYSTEMD_BACKUP" ]; then
    tar -xzf "$SYSTEMD_BACKUP" -C /etc/systemd/system
  fi
  if [ -n "$OLD_CURRENT" ]; then
    ln -s -- "$OLD_CURRENT" "$APP_ROOT/.current-rollback"
    mv -Tf -- "$APP_ROOT/.current-rollback" "$APP_ROOT/current"
  else
    rm -f -- "$APP_ROOT/current"
  fi
  systemctl daemon-reload
  systemctl restart "$MAIN_SERVICE" || true
  restart_active_citation_workers || true
  if wait_health "$PRIMARY_PORT" && switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT"; then
    if retire_candidate_if_drained "rollback candidate"; then
      KEEP_FINAL=0
    fi
  else
    echo "CRITICAL: predecessor did not recover; healthy candidate remains on the candidate route" >&2
  fi
}

# Any unexpected failure after traffic moves to the candidate must restore the
# previous service files, current link and primary route.
trap 'rollback_primary; exit 6' ERR
ln -s -- "$FINAL" "$APP_ROOT/.current-$RELEASE_ID"
mv -Tf -- "$APP_ROOT/.current-$RELEASE_ID" "$APP_ROOT/current"
if [ -n "$OLD_CURRENT" ]; then
  ln -sfn -- "$OLD_CURRENT" "$APP_ROOT/previous"
fi
install -o root -g root -m 0644 "$FINAL/app/deploy/marx-search.service" /etc/systemd/system/marx-search.service
rm -rf -- /etc/systemd/system/marx-search.service.d
for unit in "${MANAGED_SUPPORT_UNITS[@]}"; do
  install -o root -g root -m 0644 "$FINAL/app/deploy/$unit" "/etc/systemd/system/$unit"
done
for unit in "${OPTIONAL_UNITS_PRESENT[@]}"; do
  install -o root -g root -m 0644 "$FINAL/app/deploy/production_units/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload

if ! systemctl restart "$MAIN_SERVICE" || ! wait_health "$PRIMARY_PORT" "$RELEASE_ID"; then
  rollback_primary
  exit 6
fi
if ! restart_active_citation_workers; then
  rollback_primary
  exit 6
fi
if ! switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT"; then
  rollback_primary
  exit 7
fi
# New traffic is back on the canonical primary.  Existing candidate requests
# get the same bounded drain protection before the transient process retires.
health "$PRIMARY_PORT" "$RELEASE_ID" || { rollback_primary; exit 8; }
retire_candidate_if_drained "promoted release candidate" || true

# The historical corpus-promotion path mutates live state outside the release
# transaction. Keep it installed as an explanatory compatibility stub, but
# never leave its old timer armed after the first immutable promotion.
systemctl disable --now marx-corpus-repair-promote.timer >/dev/null 2>&1 || true
systemctl stop marx-corpus-repair-promote.service >/dev/null 2>&1 || true

printf '%s\n' "$RELEASE_ID" > "$APP_ROOT/DEPLOYED_SHA.tmp"
mv -f -- "$APP_ROOT/DEPLOYED_SHA.tmp" "$APP_ROOT/DEPLOYED_SHA"
python3 - "$LEDGER" "$RELEASE_ID" "$EXPECTED_LIVE" "$FINAL/release.json" <<'PY'
import datetime, json, pathlib, sys
entry = {
    "event": "promote",
    "release_id": sys.argv[2],
    "parent_release_id": sys.argv[3],
    "catalog_release": json.loads(pathlib.Path(sys.argv[4]).read_text(encoding="utf-8")).get("catalog_release"),
    "at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
}
with pathlib.Path(sys.argv[1]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
PY
trap - ERR

# Retain every prior release and catalogue. Archival requires evidence that two
# newer releases have each been healthy for 30 days; count-based deletion is unsafe.

echo "RELEASE_PROMOTED=$RELEASE_ID"
