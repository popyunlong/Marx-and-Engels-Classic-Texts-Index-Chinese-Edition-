#!/usr/bin/env bash
# Sourced only by release/rollback while /run/lock/marx-search-release.lock is held.
agent_api_prepare() {
  local release="$1" python="${MARX_AGENT_API_PYTHON:-/usr/bin/python3.11}"
  [ -f "$release/app/agent_api_mcp.py" ] || return 0
  [ -x "$python" ] || { echo "MCP requires an existing Python 3.11+ at MARX_AGENT_API_PYTHON" >&2; return 1; }
  "$python" -c 'import sys; assert sys.version_info >= (3,11)' || return 1
  mkdir -p "$release/.agent-api-deps"
  "$python" -m pip install --disable-pip-version-check --no-input --no-compile \
    --target "$release/.agent-api-deps" -r "$release/app/deploy/requirements-agent-api.lock" || return 1
  printf '%s\n' "$python" > "$release/agent-api-python"
}

agent_api_health() {
  local port="$1" release="$2"
  curl -fsS --max-time 3 "http://127.0.0.1:$port/health" | python3 -c \
    'import json,sys; value=json.load(sys.stdin); assert value.get("ok") and value.get("release_id")==sys.argv[1]' \
    "$(basename "$release")" >/dev/null 2>&1
}

agent_api_stop() {
  local backend="$1" sidecar=$(( $1 + 10000 )) unit="marx-search-agent-api-$1.service"
  if ss -Hltn "( sport = :${sidecar} )" | grep -q .; then
    drain_port "$sidecar" "MCP adapter" || return 1
  fi
  systemctl disable --now "$unit" >/dev/null 2>&1 || true
}

agent_api_route() {
  local release="$1" backend="$2" config="$3" sidecar=$(( $2 + 10000 ))
  local unit="marx-search-agent-api-$2.service" python i
  if [ ! -f "$release/app/agent_api_mcp.py" ]; then
    # When the restored primary predates MCP, retire any adapter installed by
    # the failed promotion as well as removing its public route. The caller
    # has already routed requests to the candidate before restoring primary.
    if [ "$backend" = "$PRIMARY_PORT" ]; then
      agent_api_stop "$backend" || return 1
    fi
    python3 "$MCP_TRANSACTION_APP/deploy/agent_api_caddy.py" "$config"
    return
  fi
  if ! agent_api_health "$sidecar" "$release"; then
    [ -f "$release/agent-api-python" ] || { echo "MCP runtime was not prepared" >&2; return 1; }
    python="$(cat "$release/agent-api-python")"
    [ -x "$python" ] || return 1
    agent_api_stop "$backend" || return 1
    cat > "/etc/systemd/system/$unit" <<EOF
[Unit]
Description=Public corpus MCP adapter for backend $backend
After=network-online.target
[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=$release/app
EnvironmentFile=-/etc/marx-search.env
Environment=PYTHONPATH=$release/app:$release/.agent-api-deps
Environment=PYTHONPYCACHEPREFIX=$APP_ROOT/runtime-cache/agent-api
Environment=APP_RELEASE_FILE=$release/release.json
Environment=MARX_AGENT_API_UPSTREAM=http://127.0.0.1:$backend/api/v1
ExecStart=$python -m uvicorn agent_api_mcp:app --host 127.0.0.1 --port $sidecar --limit-concurrency 16 --no-access-log --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=full
ReadOnlyPaths=$release
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload || return 1
    systemctl start "$unit" || return 1
    for i in $(seq 1 15); do
      agent_api_health "$sidecar" "$release" && break
      sleep 1
    done
    agent_api_health "$sidecar" "$release" || return 1
    if [ "$backend" = "$PRIMARY_PORT" ]; then systemctl enable "$unit" || return 1; fi
  fi
  python3 "$MCP_TRANSACTION_APP/deploy/agent_api_caddy.py" "$config" "$sidecar"
}

agent_api_switch_route() {
  local target="$1" config="$2" release
  if [ "$target" = "$CANDIDATE_PORT" ]; then
    release="$MCP_CANDIDATE_RELEASE"
  else
    release="$(readlink -f "$APP_ROOT/current")"
  fi
  agent_api_route "$release" "$target" "$config"
}
