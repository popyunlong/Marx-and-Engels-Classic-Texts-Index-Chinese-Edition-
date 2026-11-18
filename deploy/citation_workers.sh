#!/usr/bin/env bash
# Called by the locked promotion and rollback transactions after manifest verification.
citation_bridge=marx-search-citation-agent-test-worker
citation_model=marx-citation-agent

capture_citation_units() {
  local unit active enabled state
  for unit in "$citation_model" "$citation_bridge"; do
    active=0
    enabled=0
    systemctl is-active --quiet "$unit.service" && active=1
    state=$(systemctl is-enabled "$unit.service" 2>/dev/null) || true
    [[ "$state" == enabled ]] && enabled=1
    printf '%s %s %s\n' "$unit" "$active" "$enabled"
  done > "$1"
}

stop_citation_units() {
  # Finish the producer's claimed job while its model consumer is still available.
  systemctl stop "$citation_bridge.service" || return 1
  systemctl stop "$citation_model.service" || return 1
}

remember_legacy_citation() {
  local release="$1" old="$2" bundle
  if [[ -n "$old" && -f "$old/app/deploy/citation_workers.sh" ]]; then
    cp -- "$old/citation-legacy.tsv" "$release/citation-legacy.tsv" || return 1
    cp -- "$old/citation-legacy-model.path" "$release/citation-legacy-model.path" || return 1
  else
    cp -- "$release/citation-units-before.tsv" "$release/citation-legacy.tsv" || return 1
    bundle=$(readlink -e /opt/marx-citation-agent/current) || return 1
    [[ "$bundle" == /opt/marx-citation-agent/releases/* && -f "$bundle/scripts/citation_agent_worker.py" ]] || return 1
    printf '%s\n' "$bundle" > "$release/citation-legacy-model.path"
  fi
}

citation_legacy_compatible() {
  # Read only counts from the actual shared stores. v1 can edit/re-export even
  # completed tasks, and cannot honor v2 evidence or partial-export decisions.
  python3 - <<'PY'
import datetime, os, pathlib, pwd, sqlite3, sys
root = pathlib.Path(pwd.getpwnam("www-data").pw_dir) / ".marx_search_full"
queue = pathlib.Path("/var/lib/marx-citation-agent/queue/queue.sqlite3")
config = pathlib.Path("/etc/marx-citation-agent-bridge.env")
if config.is_file():
    for line in config.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key == "CITATION_AGENT_QUEUE_PATH":
            queue = pathlib.Path(value.strip().strip("\"'"))
if not queue.is_absolute():
    sys.exit("citation queue path must be absolute")
now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
def count(file, table, check, query):
    if not file.is_file():
        return 0
    conn = sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        return conn.execute(query, (now,)).fetchone()[0] if check in cols else 0
    finally:
        conn.close()
jobs = count(root / "citation_agent_test.sqlite3", "citation_assistant_jobs", "policy_version",
             "SELECT count(*) FROM citation_assistant_jobs WHERE (policy_version>=2 OR execution_generation>0) "
             "AND expires_at>? AND status NOT IN ('expired','deleted')")
requests = count(queue, "citation_agent_queue", "request_json",
                 "SELECT count(*) FROM citation_agent_queue WHERE expires_at>? AND status IN ('queued','running') "
                 "AND json_extract(request_json,'$.protocol_version')=2")
if jobs or requests:
    sys.exit(f"v1 citation workers incompatible with {jobs} retained v2 jobs and {requests} active v2 requests")
PY
}

restore_citation_units() {
  local unit active enabled state
  while read -r unit active enabled; do
    if [[ "$enabled" == 1 ]]; then
      systemctl enable "$unit.service" || return 1
    else
      systemctl disable "$unit.service" || return 1
    fi
    if [[ "$active" == 1 ]]; then
      systemctl restart "$unit.service" || return 1
      if [[ -f "${2:-}/app/deploy/citation_workers.sh" ]]; then
        wait_citation_ready "$unit" || return 1
      fi
    else
      systemctl stop "$unit.service" || return 1
    fi
  done < "$1"
}

recover_citation_units() {
  # The website can recover from a failed promotion even when a v2 candidate
  # received a new submission during drain. Preserve these jobs for v2.
  if [[ ! -f "$2/app/deploy/citation_workers.sh" ]] && ! citation_legacy_compatible; then
    systemctl disable --now "$citation_bridge.service" "$citation_model.service" || return 1
    echo "citation workers held for v2 recovery; predecessor website may resume" >&2
    return
  fi
  restore_citation_units "$1" "$2"
}

# Isolate only the three allowlisted files outside the application's inaccessible path.
install_citation_model() {
  local app="$1/app" dest="/opt/marx-citation-agent/releases/${1##*/}"
  local temp name
  temp=$(mktemp -d "${dest%/*}/.stage.XXXXXX") || return 1
  if ! (
    set -e
    cd "$temp" || exit 1
    # Give the service account traversal on this root-owned, code-only directory.
    chmod 0755 . || exit 1
    install -d -m 0755 scripts || exit 1
    for name in ai_models.py citation_agent_queue.py scripts/citation_agent_worker.py; do
      install -m 0644 "$app/$name" "$name" || exit 1
    done
    if [[ -d "$dest" ]]; then
      diff -qr . "$dest" || exit 1
    else
      mv -T -- "$temp" "$dest" || exit 1
    fi
  ); then
    rm -rf -- "$temp"
    return 1
  fi
  [[ ! -d "$temp" ]] || rm -rf -- "$temp" || return 1
  # Use a release-specific path so restoring the backed-up unit restores its code.
  sed "s|^WorkingDirectory=/opt/marx-citation-agent/current$|WorkingDirectory=$dest|" \
    "$app/deploy/$citation_model.service" > "/etc/systemd/system/$citation_model.service" || return 1
  chmod 0644 "/etc/systemd/system/$citation_model.service" || return 1
}

citation_ready() {
  local unit="$1" pid
  pid=$(systemctl show -p MainPID --value "$unit.service") || return 1
  python3 - "$pid" "/run/$unit/ready-v2.json" <<'PY'
import json, pathlib, sys, time
try:
    state = json.loads(pathlib.Path(sys.argv[2]).read_text())
    ok = int(sys.argv[1]) > 0 and state["pid"] == int(sys.argv[1])
    ok = ok and state["protocol"] == 2 and 0 <= time.time() - state["time"] < 60
except (OSError, KeyError, ValueError, TypeError):
    ok = False
sys.exit(0 if ok else 1)
PY
}

wait_citation_ready() {
  local unit="$1" i
  for i in $(seq 1 "${HEALTH_RETRIES:-180}"); do
    citation_ready "$unit" && return 0
    sleep 2
  done
  echo "$unit did not publish its protocol-v2 heartbeat" >&2
  return 1
}

activate_citation_units() {
  local release="$1" owner="${2:-}" bundle
  if [[ ! -f "$release/app/deploy/citation_workers.sh" ]]; then
    citation_legacy_compatible || return 1
    read -r bundle < "$owner/citation-legacy-model.path" || return 1
    [[ "$bundle" =~ ^/opt/marx-citation-agent/releases/[A-Za-z0-9_.-]+$ && -f "$bundle/scripts/citation_agent_worker.py" ]] || return 1
    sed "s|^WorkingDirectory=/opt/marx-citation-agent/current$|WorkingDirectory=$bundle|" \
      "$release/app/deploy/$citation_model.service" > "/etc/systemd/system/$citation_model.service" || return 1
    systemctl daemon-reload || return 1
    restore_citation_units "$owner/citation-legacy.tsv" "$release" || return 1
    return 0
  fi
  install_citation_model "$release" || return 1
  systemctl daemon-reload || return 1
  systemctl enable "$citation_model.service" "$citation_bridge.service" || return 1
  systemctl start "$citation_model.service" || return 1
  wait_citation_ready "$citation_model" || return 1
  systemctl start "$citation_bridge.service" || return 1
  wait_citation_ready "$citation_bridge" && citation_ready "$citation_model"
}
