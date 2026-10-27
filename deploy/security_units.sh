#!/usr/bin/env bash
# Called only by an existing release/rollback transaction holding fd 9.
activate_security_units() {
  local app_source="$1" unit
  if [ ! -f "$app_source/scripts/security_ops_watch.py" ]; then
    systemctl disable --now marx-search-security-watch.timer >/dev/null 2>&1 || true
    systemctl stop marx-search-security-watch.service >/dev/null 2>&1 || true
    return 0
  fi
  for unit in marx-search-security-watch.service marx-search-security-watch.timer; do
    install -o root -g root -m 0644 "$app_source/deploy/$unit" "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
  systemctl enable --now marx-search-security-watch.timer
}
