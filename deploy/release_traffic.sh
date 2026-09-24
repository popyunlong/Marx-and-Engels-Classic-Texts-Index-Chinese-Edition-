#!/usr/bin/env bash
# Shared rollback traffic helpers; caller owns the release lock.
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

