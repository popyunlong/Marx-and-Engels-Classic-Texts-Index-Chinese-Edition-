#!/usr/bin/env bash
# Blue/green restart for the current single-host Caddy + Waitress deployment.
# New traffic is sent to a temporary candidate while the primary is restarted;
# Caddy reloads are graceful, so existing HTTP connections keep their old route.
set -euo pipefail

APP_DIR="${MARX_APP_DIR:-/opt/marx-search}"
RELEASE_DIR="${MARX_RELEASE_DIR:-$APP_DIR}"
PATCH_ARCHIVE="${MARX_PATCH_ARCHIVE:-}"
MAIN_SERVICE="${MARX_MAIN_SERVICE:-marx-search.service}"
CANDIDATE_SERVICE="${MARX_CANDIDATE_SERVICE:-marx-search-candidate.service}"
ENV_FILE="${MARX_ENV_FILE:-/etc/marx-search.env}"
CADDYFILE="${MARX_CADDYFILE:-/etc/caddy/Caddyfile}"
PRIMARY_PORT="${MARX_PRIMARY_PORT:-8000}"
CANDIDATE_PORT="${MARX_CANDIDATE_PORT:-8001}"
# 研究综述可持续 100s+；两次切流后给旧连接足够的自然收尾时间，
# 同时避免候选进程暴露在完整线上负载中过久才进入正式提升。
DRAIN_SECONDS="${MARX_DEPLOY_DRAIN_SECONDS:-120}"
HEALTH_RETRIES="${MARX_DEPLOY_HEALTH_RETRIES:-30}"
EXTRA_PYTHONPATH="${MARX_EXTRA_PYTHONPATH:-$RELEASE_DIR/.deploy-deps}"
SKIP_DEP_INSTALL="${MARX_SKIP_DEP_INSTALL:-0}"

if [ "$(id -u)" -ne 0 ]; then
  echo "zero_downtime_restart.sh must run as root" >&2
  exit 2
fi
for required in "$APP_DIR/serve.py" "$APP_DIR/.venv/bin/python" "$RELEASE_DIR/app.py" "$ENV_FILE" "$CADDYFILE"; do
  if [ ! -e "$required" ]; then
    echo "Missing required deployment path: $required" >&2
    exit 2
  fi
done
if [ "$RELEASE_DIR" = "$APP_DIR" ] || [ -z "$PATCH_ARCHIVE" ] || [ ! -f "$PATCH_ARCHIVE" ]; then
  echo "An isolated MARX_RELEASE_DIR and MARX_PATCH_ARCHIVE are required." >&2
  exit 2
fi
if ! grep -Eq "reverse_proxy[[:space:]]+127\.0\.0\.1:${PRIMARY_PORT}([[:space:]]|$)" "$CADDYFILE"; then
  echo "Caddyfile does not point at primary port ${PRIMARY_PORT}; refusing an ambiguous cutover." >&2
  exit 3
fi

health() {
  local port="$1"
  curl -fsS --max-time 5 "http://127.0.0.1:${port}/api/runtime" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/pricing" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/ai" >/dev/null \
    && curl -fsS --max-time 8 "http://127.0.0.1:${port}/v2/ai" >/dev/null
}

wait_health() {
  local port="$1"
  local i
  for i in $(seq 1 "$HEALTH_RETRIES"); do
    if health "$port"; then return 0; fi
    sleep 2
  done
  return 1
}

switch_caddy() {
  local from_port="$1"
  local to_port="$2"
  local temp original
  temp="$(mktemp /etc/caddy/Caddyfile.marx-cutover.XXXXXX)"
  original="$(mktemp /etc/caddy/Caddyfile.marx-original.XXXXXX)"
  cp -a "$CADDYFILE" "$original"
  sed -E "s#(reverse_proxy[[:space:]]+127\.0\.0\.1:)${from_port}([[:space:]]|$)#\1${to_port}\2#g" "$CADDYFILE" > "$temp"
  if ! grep -Eq "reverse_proxy[[:space:]]+127\.0\.0\.1:${to_port}([[:space:]]|$)" "$temp"; then
    rm -f "$temp" "$original"
    echo "Failed to prepare Caddy cutover ${from_port} -> ${to_port}" >&2
    return 1
  fi
  if ! caddy validate --config "$temp" >/dev/null; then
    rm -f "$temp" "$original"
    return 1
  fi
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

monitor_drain() {
  local active_port="$1"
  local fallback_port="$2"
  local remaining="$DRAIN_SECONDS"
  while [ "$remaining" -gt 0 ]; do
    if ! health "$active_port"; then
      # A long search can briefly saturate a worker or the host.  Require three
      # consecutive failures before rolling back an otherwise healthy cutover.
      sleep 2
      if ! health "$active_port"; then
        sleep 2
        if ! health "$active_port"; then
          echo "Active port ${active_port} failed during drain; returning traffic to ${fallback_port}." >&2
          switch_caddy "$active_port" "$fallback_port" || true
          return 1
        fi
      fi
    fi
    if [ "$remaining" -lt 5 ]; then sleep "$remaining"; else sleep 5; fi
    remaining=$((remaining - 5))
  done
  return 0
}

echo "Starting isolated candidate on port ${CANDIDATE_PORT} ..."
systemctl stop "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
systemctl reset-failed "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
systemd-run \
  --unit="${CANDIDATE_SERVICE%.service}" \
  --property=Type=exec \
  --property=User=www-data \
  --property=Group=www-data \
  --property="WorkingDirectory=${RELEASE_DIR}" \
  --property="EnvironmentFile=${ENV_FILE}" \
  --setenv=PYTHONUNBUFFERED=1 \
  --setenv="PYTHONPATH=${RELEASE_DIR}:${EXTRA_PYTHONPATH}" \
  /usr/bin/env "PORT=${CANDIDATE_PORT}" \
  "$APP_DIR/.venv/bin/python" -c \
  'from app import DEPLOYMENT, run_waitress; assert DEPLOYMENT.is_server; run_waitress()' >/dev/null

if ! wait_health "$CANDIDATE_PORT"; then
  journalctl -u "$CANDIDATE_SERVICE" -n 50 --no-pager >&2 || true
  systemctl stop "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
  echo "Candidate failed health checks; primary was not touched." >&2
  exit 4
fi

echo "Routing new requests to candidate; primary remains available for in-flight requests ..."
if ! switch_caddy "$PRIMARY_PORT" "$CANDIDATE_PORT"; then
  systemctl stop "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
  echo "Caddy cutover failed; primary remains active." >&2
  exit 5
fi
if ! monitor_drain "$CANDIDATE_PORT" "$PRIMARY_PORT"; then
  systemctl stop "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
  echo "Candidate became unhealthy; the unchanged primary is serving traffic." >&2
  exit 8
fi

if [ "$SKIP_DEP_INSTALL" = "1" ]; then
  echo "Skipping shared virtualenv dependency installation (isolated dependencies were prevalidated)."
else
  echo "Installing dependencies while the candidate serves traffic ..."
  if ! "$APP_DIR/.venv/bin/python" -m pip install -r "$RELEASE_DIR/requirements.txt"; then
    echo "Dependency installation failed. Candidate remains live; primary files were not promoted." >&2
    exit 9
  fi
fi
if ! health "$CANDIDATE_PORT"; then
  switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT" || true
  echo "Candidate failed before promotion; the unchanged primary is serving traffic." >&2
  exit 10
fi

echo "Promoting the already-validated patch while the candidate serves traffic ..."
promote_dir="$(mktemp -d)"
cleanup_promote() { rm -rf -- "$promote_dir"; }
trap cleanup_promote EXIT
if ! tar --extract --gzip --file "$PATCH_ARCHIVE" --directory "$promote_dir"; then
  cleanup_promote
  trap - EXIT
  echo "Patch promotion failed. Candidate remains live and continues serving traffic." >&2
  exit 11
fi
while IFS= read -r -d '' source; do
  relative="${source#"$promote_dir"/}"
  mkdir -p -- "$APP_DIR/$relative"
done < <(find "$promote_dir" -mindepth 1 -type d -print0)
while IFS= read -r -d '' source; do
  relative="${source#"$promote_dir"/}"
  destination="$APP_DIR/$relative"
  rm -f -- "$destination"
  cp -a -- "$source" "$destination"
done < <(find "$promote_dir" -mindepth 1 ! -type d -print0)
cleanup_promote
trap - EXIT

echo "Restarting primary while candidate serves all new traffic ..."
if ! systemctl restart "$MAIN_SERVICE" || ! wait_health "$PRIMARY_PORT"; then
  journalctl -u "$MAIN_SERVICE" -n 80 --no-pager >&2 || true
  echo "Primary failed after restart. Candidate remains live on port ${CANDIDATE_PORT}; Caddy was intentionally left on it." >&2
  exit 6
fi

echo "Routing new requests back to the healthy primary ..."
if ! switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT"; then
  echo "Could not route back to primary. Candidate remains live; no traffic outage was introduced." >&2
  exit 7
fi
if ! monitor_drain "$PRIMARY_PORT" "$CANDIDATE_PORT"; then
  echo "Primary became unhealthy; candidate remains live and received traffic again." >&2
  exit 12
fi
systemctl stop "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true
systemctl reset-failed "$CANDIDATE_SERVICE" >/dev/null 2>&1 || true

if ! health "$PRIMARY_PORT" || ! systemctl is-active --quiet "$MAIN_SERVICE"; then
  echo "Post-cutover primary health check failed." >&2
  exit 13
fi
echo "ZERO_DOWNTIME_CUTOVER_OK"
