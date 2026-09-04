#!/usr/bin/env bash
# Promote one explicitly confirmed corpus-repair batch with a blue/green cutover.
set -euo pipefail

APP_DIR="${MARX_APP_DIR:-/opt/marx-search}"
OUTPUT_ROOT="${MARX_CORPUS_REPAIR_ROOT:-/home/data/marx-search-corpus-repair}"
REVIEW_DB="$OUTPUT_ROOT/review.sqlite3"
LIVE_DB="$APP_DIR/data/corpus.sqlite"
LIVE_SHA="$APP_DIR/data/corpus.sqlite.sha256"
PYTHON="$APP_DIR/.venv/bin/python"
CADDYFILE="${MARX_CADDYFILE:-/etc/caddy/Caddyfile}"
PRIMARY_PORT=8000
CANDIDATE_PORT=8001
CANDIDATE_SERVICE=marx-search-corpus-candidate.service
DRAIN_SECONDS="${MARX_CORPUS_REPAIR_DRAIN_SECONDS:-120}"

test "$(id -u)" -eq 0 || { echo "promotion must run as root" >&2; exit 2; }
for path in "$REVIEW_DB" "$LIVE_DB" "$PYTHON" "$CADDYFILE"; do
  test -e "$path" || { echo "missing promotion prerequisite: $path" >&2; exit 2; }
done

exec 9>"$OUTPUT_ROOT/promotion.lock"
flock -n 9 || exit 0

manifest="$($PYTHON "$APP_DIR/scripts/corpus_repair_promotion.py" prepare \
  --review-db "$REVIEW_DB" --output-root "$OUTPUT_ROOT" --live-db "$LIVE_DB")"
pending="$(printf '%s' "$manifest" | $PYTHON -c 'import json,sys; print("1" if json.load(sys.stdin).get("pending") else "0")')"
test "$pending" = 1 || exit 0
batch_id="$(printf '%s' "$manifest" | $PYTHON -c 'import json,sys; print(json.load(sys.stdin)["batch_id"])')"
candidate="$(printf '%s' "$manifest" | $PYTHON -c 'import json,sys; print(json.load(sys.stdin)["candidate"])')"
candidate_sha="$(printf '%s' "$manifest" | $PYTHON -c 'import json,sys; print(json.load(sys.stdin)["candidate_sha256"])')"
source_sha="$(printf '%s' "$manifest" | $PYTHON -c 'import json,sys; print(json.load(sys.stdin)["source_sha256"])')"

case "$batch_id" in *[!A-Za-z0-9._-]*|'') exit 2;; esac
case "$candidate" in "$OUTPUT_ROOT/batches/$batch_id/candidate-corpus.sqlite") ;; *) exit 2;; esac
test "$(sha256sum "$candidate" | awk '{print $1}')" = "$candidate_sha"
test "$(sha256sum "$LIVE_DB" | awk '{print $1}')" = "$source_sha"
# A confirmed promotion never overlaps the low-priority scanner.  Stopping the
# scanner only terminates its own cgroup; interrupted pages are recovered from
# the review checkpoint on the next scheduled night.
systemctl stop marx-corpus-repair.service >/dev/null 2>&1 || true
test "$(grep -Ec "reverse_proxy[[:space:]]+127\.0\.0\.1:${PRIMARY_PORT}([[:space:]]|$)" "$CADDYFILE")" -eq 1 || {
  echo "Caddy is not on the primary port; refusing ambiguous promotion" >&2; exit 75;
}

available_mem="$(awk '/MemAvailable:/ {print $2*1024}' /proc/meminfo)"
primary_mem="$(systemctl show marx-search -p MemoryCurrent --value)"
case "$primary_mem" in ''|'[not set]'|*[!0-9]*) echo "cannot determine primary memory" >&2; exit 75;; esac
test "$available_mem" -ge $((primary_mem + 1024*1024*1024)) || {
  echo "insufficient memory for blue/green overlap; leaving request queued" >&2; exit 75;
}
if command -v vmstat >/dev/null; then
  swap_line="$(vmstat 1 2 | tail -1)"
  swap_in="$(printf '%s' "$swap_line" | awk '{print $7}')"
  swap_out="$(printf '%s' "$swap_line" | awk '{print $8}')"
  test "${swap_in:-0}" -eq 0 && test "${swap_out:-0}" -eq 0 || {
    echo "active swap pressure; leaving request queued" >&2; exit 75;
  }
fi

release="$OUTPUT_ROOT/batches/$batch_id/promotion-release"
backup_dir="$OUTPUT_ROOT/backups/$batch_id"
case "$release" in "$OUTPUT_ROOT/batches/$batch_id/"*) ;; *) exit 2;; esac
test ! -e "$release" || { echo "promotion release already exists" >&2; exit 75; }
mkdir -p "$release" "$backup_dir" "$release/config" "$release/data"
find "$APP_DIR" -maxdepth 1 -type f -name '*.py' -exec cp -a -t "$release" {} +
for file in requirements.txt; do test -f "$APP_DIR/$file" && cp -a "$APP_DIR/$file" "$release/$file"; done
cp -a "$APP_DIR/config/." "$release/config/"
cp -a -s "$APP_DIR/data/." "$release/data/"
rm -f "$release/data/corpus.sqlite" "$release/data/corpus.sqlite.sha256"
ln -s "$candidate" "$release/data/corpus.sqlite"
printf '%s\n' "$candidate_sha" > "$release/data/corpus.sqlite.sha256"
for entry in pdfs templates static static_library stream_library vendor logs; do
  test -e "$APP_DIR/$entry" && ln -s "$APP_DIR/$entry" "$release/$entry"
done

health() {
  local port="$1"
  curl -fsS --max-time 6 "http://127.0.0.1:${port}/api/runtime" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/pricing" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/ai" >/dev/null
}

wait_health() {
  local port="$1"
  for _ in $(seq 1 45); do health "$port" && return 0; sleep 2; done
  return 1
}

switch_caddy() {
  local from_port="$1" to_port="$2" temp original
  temp="$(mktemp /etc/caddy/Caddyfile.corpus-repair.XXXXXX)"
  original="$(mktemp /etc/caddy/Caddyfile.corpus-repair-original.XXXXXX)"
  cp -a "$CADDYFILE" "$original"
  sed -E "s#(reverse_proxy[[:space:]]+127\.0\.0\.1:)${from_port}([[:space:]]|$)#\1${to_port}\2#g" "$CADDYFILE" > "$temp"
  test "$(grep -Ec "reverse_proxy[[:space:]]+127\.0\.0\.1:${to_port}([[:space:]]|$)" "$temp")" -eq 1
  caddy validate --config "$temp" >/dev/null
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

drain() {
  local active="$1" fallback="$2" remaining="$DRAIN_SECONDS"
  while test "$remaining" -gt 0; do
    if ! health "$active"; then
      sleep 2
      if ! health "$active"; then
        if switch_caddy "$active" "$fallback"; then
          if test "$fallback" -eq "$CANDIDATE_PORT"; then
            caddy_on_candidate=1
          else
            caddy_on_candidate=0
          fi
        fi
        return 1
      fi
    fi
    sleep 5
    remaining=$((remaining-5))
  done
}

live_changed=0
caddy_on_candidate=0
promotion_terminal_marked=0
rollback() {
  local status=$?
  set +e
  if test "$live_changed" -eq 1 && test -f "$backup_dir/corpus.sqlite"; then
    cp --reflink=auto -a "$backup_dir/corpus.sqlite" "$APP_DIR/data/corpus.sqlite.rollback"
    mv -Tf "$APP_DIR/data/corpus.sqlite.rollback" "$LIVE_DB"
    cp -a "$backup_dir/corpus.sqlite.sha256" "$LIVE_SHA"
    systemctl restart marx-search
    wait_health "$PRIMARY_PORT"
  fi
  if test "$caddy_on_candidate" -eq 1 && health "$PRIMARY_PORT"; then
    if switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT"; then
      caddy_on_candidate=0
    fi
  fi
  if test "$caddy_on_candidate" -eq 0; then
    systemctl stop "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
  fi
  if test "$promotion_terminal_marked" -eq 0; then
    $PYTHON "$APP_DIR/scripts/corpus_repair_promotion.py" mark --review-db "$REVIEW_DB" \
      --batch-id "$batch_id" --status failed --error "promotion rolled back (exit $status)" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap rollback ERR

systemctl stop "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
systemctl reset-failed "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
systemd-run --unit="${CANDIDATE_SERVICE%.service}" --property=Type=exec \
  --property=User=www-data --property=Group=www-data --property=SupplementaryGroups=marx-repair \
  --property=CPUWeight=1 --property=IOWeight=1 --property=IOSchedulingClass=idle --property=Nice=10 \
  --property="WorkingDirectory=$release" \
  --property=EnvironmentFile=/etc/marx-search.env --setenv=PYTHONUNBUFFERED=1 \
  --setenv="PYTHONPATH=$release" /usr/bin/env "PORT=$CANDIDATE_PORT" \
  "$PYTHON" -c 'from pathlib import Path; from app import DEPLOYMENT,corpus,run_waitress; from corpus_candidate_acceptance import verify_candidate_runtime; assert DEPLOYMENT.is_server; verify_candidate_runtime(corpus,Path.cwd()); run_waitress()' >/dev/null
wait_health "$CANDIDATE_PORT"
switch_caddy "$PRIMARY_PORT" "$CANDIDATE_PORT"
caddy_on_candidate=1
drain "$CANDIDATE_PORT" "$PRIMARY_PORT"

# The website or an operator may have replaced the live corpus while the
# candidate was warming up.  Re-bind the cutover to the approved baseline at
# the last possible moment, before any production file is copied or stopped.
if test "$(sha256sum "$LIVE_DB" | awk '{print $1}')" != "$source_sha"; then
  echo "live database changed during candidate validation; refusing cutover" >&2
  $PYTHON "$APP_DIR/scripts/corpus_repair_promotion.py" mark --review-db "$REVIEW_DB" \
    --batch-id "$batch_id" --status stale --error "live database changed during candidate validation" >/dev/null
  promotion_terminal_marked=1
  false
fi
$PYTHON "$APP_DIR/scripts/corpus_repair_promotion.py" mark --review-db "$REVIEW_DB" \
  --batch-id "$batch_id" --status running >/dev/null
cp --reflink=auto -a "$LIVE_DB" "$backup_dir/corpus.sqlite"
if test -f "$LIVE_SHA"; then cp -a "$LIVE_SHA" "$backup_dir/corpus.sqlite.sha256"; else printf '%s\n' "$source_sha" > "$backup_dir/corpus.sqlite.sha256"; fi
owner="$(stat -c '%U:%G' "$LIVE_DB")"
cp --reflink=auto -a "$candidate" "$APP_DIR/data/corpus.sqlite.next-$batch_id"
printf '%s\n' "$candidate_sha" > "$APP_DIR/data/corpus.sqlite.sha256.next-$batch_id"
chown "$owner" "$APP_DIR/data/corpus.sqlite.next-$batch_id" "$APP_DIR/data/corpus.sqlite.sha256.next-$batch_id"
systemctl stop marx-search
mv -Tf "$APP_DIR/data/corpus.sqlite.next-$batch_id" "$LIVE_DB"
mv -Tf "$APP_DIR/data/corpus.sqlite.sha256.next-$batch_id" "$LIVE_SHA"
live_changed=1
systemctl start marx-search
wait_health "$PRIMARY_PORT"
switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT"
caddy_on_candidate=0
drain "$PRIMARY_PORT" "$CANDIDATE_PORT"
systemctl stop "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
systemctl reset-failed "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true

$PYTHON "$APP_DIR/scripts/corpus_repair_promotion.py" mark --review-db "$REVIEW_DB" \
  --batch-id "$batch_id" --status applied >/dev/null
trap - ERR
echo "CORPUS_REPAIR_ZERO_DOWNTIME_OK batch=$batch_id backup=$backup_dir"
