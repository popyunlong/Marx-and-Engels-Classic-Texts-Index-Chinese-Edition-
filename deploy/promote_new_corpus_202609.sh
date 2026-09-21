#!/usr/bin/env bash
# Promote the verified 2026-09 corpus with a blue/green, 30-minute rollback window.
set -euo pipefail

APP_DIR="${MARX_APP_DIR:-/opt/marx-search}"
WORK_ROOT="${MARX_NEW_CORPUS_ROOT:-/home/data/marx-search-new-corpus-202609}"
CANDIDATE_DB="${MARX_NEW_CORPUS_DB:-$WORK_ROOT/corpus.sqlite}"
PUBLISH_CONFIG_DIR="${MARX_NEW_CORPUS_CONFIG_DIR:-$WORK_ROOT/publish-config}"
BASELINE="${MARX_NEW_CORPUS_BASELINE:-$WORK_ROOT/monitor/baseline.json}"
LIVE_DB="$APP_DIR/data/corpus.sqlite"
LIVE_SHA="$APP_DIR/data/corpus.sqlite.sha256"
PYTHON="$APP_DIR/.venv/bin/python"
CADDYFILE="${MARX_CADDYFILE:-/etc/caddy/Caddyfile}"
PRIMARY_PORT=8000
CANDIDATE_PORT=8001
CANDIDATE_SERVICE=marx-search-new-corpus-candidate.service
PREFLIGHT_SERVICE=marx-search-new-corpus-preflight.service
CITATION_SERVICE=marx-search-citation-worker.service
WARM_SECONDS="${MARX_NEW_CORPUS_WARM_SECONDS:-300}"
ROLLBACK_SECONDS="${MARX_NEW_CORPUS_ROLLBACK_SECONDS:-1800}"
DRAIN_SECONDS="${MARX_NEW_CORPUS_DRAIN_SECONDS:-120}"

test "$(id -u)" -eq 0 || { echo "promotion must run as root" >&2; exit 2; }
for path in "$CANDIDATE_DB" "$PUBLISH_CONFIG_DIR" "$BASELINE" "$LIVE_DB" "$PYTHON" \
  "$CADDYFILE" "$APP_DIR/deploy/monitor_new_corpus_202609.py"; do
  test -e "$path" || { echo "missing promotion prerequisite: $path" >&2; exit 2; }
done
publish_files=(
  books.yaml manifest.yaml volumes.yaml western_marxism_sources.yaml
  western_marxism_reviewed.yaml auxiliary_sources.yaml
  bibliographic_evidence_202609.yaml new_corpus_corrections_202609.yaml
)
for file in "${publish_files[@]}"; do
  test -f "$PUBLISH_CONFIG_DIR/$file" || { echo "missing publish config: $file" >&2; exit 2; }
done
test -f "$PUBLISH_CONFIG_DIR/publish-config.json" || {
  echo "missing publish config manifest" >&2; exit 2;
}
"$PYTHON" - "$PUBLISH_CONFIG_DIR" "${publish_files[@]}" <<'PY'
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1])
expected=set(sys.argv[2:])
manifest=json.loads((root/'publish-config.json').read_text(encoding='utf-8'))
recorded=set((manifest.get('files') or {}).keys())
if recorded != expected:
    raise SystemExit(f'publish config manifest mismatch: {sorted(recorded ^ expected)}')
for name,digest in manifest['files'].items():
    actual=hashlib.sha256((root/name).read_bytes()).hexdigest()
    if actual != digest:
        raise SystemExit(f'publish config SHA-256 mismatch: {name}')
if len(set(manifest.get('available_books') or [])) != 8:
    raise SystemExit('publish config must expose exactly eight books')
PY
install -d -m 0750 "$WORK_ROOT" "$WORK_ROOT/backups"
# Candidate runs as www-data and must be able to traverse the staging root.
# Files remain non-public; only the service account's group gains read/execute.
chgrp www-data "$WORK_ROOT"
chmod 0750 "$WORK_ROOT"
exec 9>"$WORK_ROOT/promotion.lock"
flock -n 9 || { echo "another promotion is active" >&2; exit 75; }

candidate_sha="$(sha256sum "$CANDIDATE_DB" | awk '{print $1}')"
if test -f "$CANDIDATE_DB.sha256"; then
  test "$(awk 'NR==1{print $1}' "$CANDIDATE_DB.sha256")" = "$candidate_sha" || {
    echo "candidate SHA-256 sidecar mismatch" >&2; exit 2;
  }
fi
source_sha="$(sha256sum "$LIVE_DB" | awk '{print $1}')"
test "$($PYTHON - "$CANDIDATE_DB" <<'PY'
import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as c: print(c.execute('pragma integrity_check').fetchone()[0])
PY
)" = ok
cd "$APP_DIR"
primary_proxy_count="$(grep -Ec "reverse_proxy.*127\.0\.0\.1:${PRIMARY_PORT}([[:space:]{]|$)" "$CADDYFILE")"
test "$primary_proxy_count" -ge 1 || { echo "Caddy is not on the primary port" >&2; exit 75; }
available_mem="$(awk '/MemAvailable:/ {print $2*1024}' /proc/meminfo)"
primary_mem="$(systemctl show marx-search -p MemoryCurrent --value)"
case "$primary_mem" in ''|'[not set]'|*[!0-9]*) echo "cannot determine primary memory" >&2; exit 75;; esac
test "$available_mem" -ge $((2*1024*1024*1024)) || {
  echo "less than 2 GiB is available before candidate preparation" >&2; exit 75;
}
"$PYTHON" scripts/accept_new_corpus_202609.py --db "$CANDIDATE_DB" --pdf-root "$APP_DIR/pdfs" \
  --structural-only --report "$WORK_ROOT/server-acceptance.json" >/dev/null

release="$WORK_ROOT/release-${candidate_sha:0:12}"
backup_dir="$WORK_ROOT/backups/$(date +%Y%m%d-%H%M%S)-${source_sha:0:12}"
test ! -e "$release" || { echo "candidate release already exists: $release" >&2; exit 75; }
mkdir -p "$release/config" "$release/data" "$backup_dir"
find "$APP_DIR" -maxdepth 1 -type f -name '*.py' -exec cp -a -t "$release" {} +
for file in requirements.txt; do test -f "$APP_DIR/$file" && cp -a "$APP_DIR/$file" "$release/$file"; done
cp -a "$APP_DIR/scripts" "$release/scripts"
cp -a "$APP_DIR/config/." "$release/config/"
for file in "${publish_files[@]}"; do cp -a "$PUBLISH_CONFIG_DIR/$file" "$release/config/$file"; done
# The release lives on /home/data while APP_DIR commonly lives on the system
# volume.  A hard-link clone therefore cannot be relied upon and falling back
# to a recursive copy would duplicate every retired corpus backup.  Runtime
# account/session state is stored in APPDATA_DIR (/var/www/.marx_search_full)
# and is already shared by both services.  Share the remaining application
# data read-only-by-convention and mount only the candidate corpus separately.
while IFS= read -r -d '' entry; do
  name="$(basename "$entry")"
  case "$name" in
    corpus.sqlite*) continue ;;
  esac
  ln -s "$entry" "$release/data/$name"
done < <(find "$APP_DIR/data" -mindepth 1 -maxdepth 1 -print0)
ln -s "$CANDIDATE_DB" "$release/data/corpus.sqlite"
printf '%s\n' "$candidate_sha" > "$release/data/corpus.sqlite.sha256"
for entry in pdfs templates static static_library stream_library vendor logs; do
  test -e "$APP_DIR/$entry" && ln -s "$APP_DIR/$entry" "$release/$entry"
done
chown -R www-data:www-data "$release"
chmod a+r "$CANDIDATE_DB"

health() {
  local port="$1"
  curl -fsS --max-time 6 "http://127.0.0.1:${port}/api/runtime" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/pricing" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/ai" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/v2/ai" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/v2/read" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/api/ai/runtime" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/api/ai/assistant-config" >/dev/null
}

wait_health() {
  local port="$1"
  for _ in $(seq 1 60); do health "$port" && return 0; sleep 2; done
  return 1
}

switch_caddy() {
  local from_port="$1" to_port="$2" temp original from_count
  temp="$(mktemp /etc/caddy/Caddyfile.new-corpus.XXXXXX)"
  original="$(mktemp /etc/caddy/Caddyfile.new-corpus-original.XXXXXX)"
  cp -a "$CADDYFILE" "$original"
  from_count="$(grep -Ec "reverse_proxy.*127\.0\.0\.1:${from_port}([[:space:]{]|$)" "$CADDYFILE")"
  test "$from_count" -ge 1
  sed -E "/^[[:space:]]*reverse_proxy[[:space:]]/ s#127\.0\.0\.1:${from_port}([[:space:]{]|$)#127.0.0.1:${to_port}\1#g" "$CADDYFILE" > "$temp"
  test "$(grep -Ec "reverse_proxy.*127\.0\.0\.1:${from_port}([[:space:]{]|$)" "$temp")" -eq 0
  test "$(grep -Ec "reverse_proxy.*127\.0\.0\.1:${to_port}([[:space:]{]|$)" "$temp")" -ge "$from_count"
  caddy validate --adapter caddyfile --config "$temp" >/dev/null
  install -o root -g root -m 0644 "$temp" "$CADDYFILE"
  rm -f "$temp"
  if ! systemctl reload caddy; then
    install -o root -g root -m 0644 "$original" "$CADDYFILE"
    systemctl reload caddy >/dev/null 2>&1 || true
    rm -f "$original"
    return 1
  fi
  rm -f "$original"
}

monitor_for() {
  local port="$1" seconds="$2" remaining="$2"
  while test "$remaining" -gt 0; do
    health "$port" || { sleep 2; health "$port" || return 1; }
    test "$(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo)" -ge 2048 || return 1
    test "$(df -BG --output=avail /home/data | tail -1 | tr -dc '0-9')" -ge 45 || return 1
    sleep 5
    remaining=$((remaining-5))
  done
}

monitor_candidate_phase() {
  local port="$1" seconds="$2" phase="$3"
  local services=(--service caddy --service "$CANDIDATE_SERVICE")
  if test "$low_memory_mode" -eq 0; then services+=(--service marx-search); fi
  "$PYTHON" "$APP_DIR/deploy/monitor_new_corpus_202609.py" \
    --base "http://127.0.0.1:${port}" --baseline "$BASELINE" \
    --duration-seconds "$seconds" --fast-seconds "$seconds" \
    --fast-interval 5 --slow-interval 5 \
    "${services[@]}" \
    --journal-unit marx-search --journal-unit "$CANDIDATE_SERVICE" \
    --report "$WORK_ROOT/monitor/candidate-${candidate_sha:0:12}-${phase}.jsonl"
}

drain_port() {
  local port="$1" seconds="${2:-120}" remaining="${2:-120}" active
  while test "$remaining" -gt 0; do
    active="$(ss -Htn state established "( sport = :${port} )" 2>/dev/null | wc -l)"
    test "$active" -eq 0 && return 0
    sleep 2
    remaining=$((remaining-2))
  done
  echo "timed out draining ${active} connection(s) from port ${port}" >&2
  return 1
}

live_changed=0
config_changed=0
caddy_on_candidate=0
citation_was_active=0
citation_paused=0
low_memory_mode=0
force_low_memory_mode="${MARX_NEW_CORPUS_LOW_MEMORY_MODE:-0}"
rollback() {
  local status=$?
  set +e
  systemctl stop "$PREFLIGHT_SERVICE" >/dev/null 2>&1 || true
  if test "$config_changed" -eq 1; then
    for file in "${publish_files[@]}"; do
      if test -f "$backup_dir/config/$file"; then
        cp -a "$backup_dir/config/$file" "$APP_DIR/config/$file.rollback"
        mv -Tf "$APP_DIR/config/$file.rollback" "$APP_DIR/config/$file"
      elif test -f "$backup_dir/config-absent/$file"; then
        rm -f "$APP_DIR/config/$file"
      fi
    done
  fi
  if test "$live_changed" -eq 1 && test -f "$backup_dir/corpus.sqlite"; then
    cp --reflink=auto -a "$backup_dir/corpus.sqlite" "$APP_DIR/data/corpus.sqlite.rollback"
    mv -Tf "$APP_DIR/data/corpus.sqlite.rollback" "$LIVE_DB"
    cp -a "$backup_dir/corpus.sqlite.sha256" "$LIVE_SHA"
  fi
  if test "$live_changed" -eq 1 || test "$config_changed" -eq 1 \
      || ! systemctl is-active --quiet marx-search || ! health "$PRIMARY_PORT"; then
    systemctl restart marx-search >/dev/null 2>&1 || true
    wait_health "$PRIMARY_PORT" || true
  fi
  if test "$caddy_on_candidate" -eq 1 && health "$PRIMARY_PORT"; then
    switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT" && caddy_on_candidate=0
  fi
  if test "$caddy_on_candidate" -eq 0; then systemctl stop "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true; fi
  if test "$citation_paused" -eq 1 && test "$citation_was_active" -eq 1; then
    systemctl start "$CITATION_SERVICE" >/dev/null 2>&1 || true
    citation_paused=0
  fi
  echo "NEW_CORPUS_PROMOTION_ROLLED_BACK exit=$status" >&2
  exit "$status"
}
trap rollback ERR

# The standalone citation worker imports the application and keeps a second
# complete corpus in memory.  The candidate starts an equivalent versioned
# worker in-process, so quiescing the old worker during overlap only queues work
# briefly and releases enough RAM for a fail-safe blue/green launch.  It is
# restarted after either rollback or successful promotion.
if systemctl is-active --quiet "$CITATION_SERVICE"; then
  citation_was_active=1
fi
if { test "$force_low_memory_mode" = 1 \
      || test "$(awk '/MemAvailable:/ {print $2*1024}' /proc/meminfo)" -lt $((4*1024*1024*1024)); } \
    && test "$citation_was_active" -eq 1; then
  systemctl stop "$CITATION_SERVICE"
  citation_paused=1
  low_memory_mode=1
fi
available_mem="$(awk '/MemAvailable:/ {print $2*1024}' /proc/meminfo)"
test "$available_mem" -ge $((3*1024*1024*1024)) || {
  echo "insufficient memory after safely quiescing the citation worker" >&2
  if test "$citation_paused" -eq 1; then
    systemctl start "$CITATION_SERVICE" >/dev/null 2>&1 || true
    citation_paused=0
  fi
  exit 75
}

systemctl stop "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
systemctl reset-failed "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
systemctl stop "$PREFLIGHT_SERVICE" >/dev/null 2>&1 || true
systemctl reset-failed "$PREFLIGHT_SERVICE" >/dev/null 2>&1 || true
systemd-run --unit="${PREFLIGHT_SERVICE%.service}" --wait --collect --pipe \
  --property=Type=exec --property=User=www-data --property=Group=www-data \
  --property=CPUWeight=10 --property=IOWeight=10 --property=Nice=5 \
  --property=MemoryHigh=1792M --property=MemoryMax=2304M --property=OOMPolicy=stop \
  --property="WorkingDirectory=$release" --property=EnvironmentFile=/etc/marx-search.env \
  --setenv=PYTHONUNBUFFERED=1 --setenv=MARX_SKIP_SEARCH_WARM=1 --setenv="PYTHONPATH=$release" \
  "$PYTHON" -c 'from pathlib import Path; import app; from scripts.runtime_new_corpus_acceptance import verify_new_runtime; verify_new_runtime(app,Path.cwd()); print("NEW_CORPUS_RUNTIME_ACCEPTANCE_OK",flush=True)'
systemd-run --unit="${CANDIDATE_SERVICE%.service}" --property=Type=exec \
  --property=User=www-data --property=Group=www-data \
  --property=CPUWeight=10 --property=IOWeight=10 --property=Nice=5 \
  --property=MemoryHigh=1792M --property=MemoryMax=2304M --property=OOMPolicy=stop \
  --property="WorkingDirectory=$release" --property=EnvironmentFile=/etc/marx-search.env \
  --setenv=PYTHONUNBUFFERED=1 --setenv=MARX_SKIP_SEARCH_WARM=1 --setenv="PYTHONPATH=$release" /usr/bin/env "PORT=$CANDIDATE_PORT" \
  "$PYTHON" -c 'import threading; import app; from scripts.citation_assistant_worker import run as run_versioned_worker; threading.Thread(target=run_versioned_worker,name="candidate-versioned-worker",daemon=True).start(); app.run_waitress()' >/dev/null
wait_health "$CANDIDATE_PORT"
if test "$low_memory_mode" -eq 1; then
  switch_caddy "$PRIMARY_PORT" "$CANDIDATE_PORT"
  caddy_on_candidate=1
  drain_port "$PRIMARY_PORT" "$DRAIN_SECONDS"
  systemctl stop marx-search
  monitor_candidate_phase "$CANDIDATE_PORT" "$WARM_SECONDS" warm
else
  monitor_candidate_phase "$CANDIDATE_PORT" "$WARM_SECONDS" warm
  switch_caddy "$PRIMARY_PORT" "$CANDIDATE_PORT"
  caddy_on_candidate=1
fi

# The unchanged primary and old database remain ready for immediate rollback for
# the full observation window while the candidate serves new requests.
monitor_candidate_phase "$CANDIDATE_PORT" "$ROLLBACK_SECONDS" rollback-window
test "$(sha256sum "$LIVE_DB" | awk '{print $1}')" = "$source_sha" || {
  echo "live database changed during observation" >&2; false;
}
cp --reflink=auto -a "$LIVE_DB" "$backup_dir/corpus.sqlite"
if test -f "$LIVE_SHA"; then cp -a "$LIVE_SHA" "$backup_dir/corpus.sqlite.sha256"; else printf '%s\n' "$source_sha" > "$backup_dir/corpus.sqlite.sha256"; fi
mkdir -p "$backup_dir/config" "$backup_dir/config-absent"
for file in "${publish_files[@]}"; do
  if test -f "$APP_DIR/config/$file"; then
    cp -a "$APP_DIR/config/$file" "$backup_dir/config/$file"
  else
    : > "$backup_dir/config-absent/$file"
  fi
done
owner="$(stat -c '%U:%G' "$LIVE_DB")"
cp --reflink=auto -a "$CANDIDATE_DB" "$APP_DIR/data/corpus.sqlite.next"
printf '%s\n' "$candidate_sha" > "$APP_DIR/data/corpus.sqlite.sha256.next"
chown "$owner" "$APP_DIR/data/corpus.sqlite.next" "$APP_DIR/data/corpus.sqlite.sha256.next"
systemctl stop marx-search
config_changed=1
for file in "${publish_files[@]}"; do
  cp -a "$PUBLISH_CONFIG_DIR/$file" "$APP_DIR/config/$file.next"
  chown --reference="$APP_DIR/config" "$APP_DIR/config/$file.next"
  mv -Tf "$APP_DIR/config/$file.next" "$APP_DIR/config/$file"
done
mv -Tf "$APP_DIR/data/corpus.sqlite.next" "$LIVE_DB"
mv -Tf "$APP_DIR/data/corpus.sqlite.sha256.next" "$LIVE_SHA"
live_changed=1
systemctl start marx-search
wait_health "$PRIMARY_PORT"
switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT"
caddy_on_candidate=0
if test "$low_memory_mode" -eq 1; then
  drain_port "$CANDIDATE_PORT" "$DRAIN_SECONDS"
  systemctl stop "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
  monitor_for "$PRIMARY_PORT" "$DRAIN_SECONDS"
else
  monitor_for "$PRIMARY_PORT" "$DRAIN_SECONDS"
  systemctl stop "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
fi
systemctl reset-failed "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
if test "$citation_paused" -eq 1 && test "$citation_was_active" -eq 1; then
  systemctl start "$CITATION_SERVICE"
  citation_paused=0
fi
trap - ERR
echo "NEW_CORPUS_ZERO_DOWNTIME_OK sha=$candidate_sha backup=$backup_dir release=$release"
