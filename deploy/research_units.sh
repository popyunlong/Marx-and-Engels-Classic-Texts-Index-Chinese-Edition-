#!/usr/bin/env bash
# Sourced ONLY by the locked immutable release/rollback transaction.
RESEARCH_LEGACY_TIMERS=(marx-search-journal-alerts.timer marx-search-journal-process.timer marx-search-journal-send.timer)
RESEARCH_TIMERS=(marx-search-research-collect.timer marx-search-research-work.timer marx-search-research-send.timer)

capture_research_units() {
  local output="$1" unit enabled active
  : > "$output"
  for unit in "${RESEARCH_LEGACY_TIMERS[@]}" "${RESEARCH_TIMERS[@]}"; do
    enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
    active="$(systemctl is-active "$unit" 2>/dev/null || true)"
    printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active" >> "$output"
  done
}

pause_research_units() {
  local timer
  for timer in "${RESEARCH_LEGACY_TIMERS[@]}" "${RESEARCH_TIMERS[@]}"; do
    systemctl stop "$timer" 2>/dev/null || true
    systemctl stop "${timer%.timer}.service" 2>/dev/null || true
  done
}

restore_research_units() {
  local input="$1" unit enabled active
  [ -f "$input" ] || return 0
  while IFS=$'\t' read -r unit enabled active; do
    case "$unit" in marx-search-journal-*.timer|marx-search-research-*.timer) ;; *) return 1;; esac
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    if [ "$enabled" = enabled ]; then systemctl enable "$unit" >/dev/null; fi
    if [ "$active" = active ]; then systemctl start "$unit"; fi
  done < "$input"
}

activate_research_units() {
  local app="$1" old_state="$2" timer
  if [ -f "$app/scripts/research_update_worker.py" ]; then
    for timer in "${RESEARCH_LEGACY_TIMERS[@]}"; do
      systemctl disable --now "$timer" >/dev/null 2>&1 || true
    done
    for timer in "${RESEARCH_TIMERS[@]}"; do systemctl enable --now "$timer"; done
  else
    for timer in "${RESEARCH_TIMERS[@]}"; do systemctl disable --now "$timer" >/dev/null 2>&1 || true; done
    restore_research_units "$old_state"
    # A rollback must not release a pending legacy issue beside the new bulletin.
    systemctl disable --now marx-search-journal-send.timer >/dev/null 2>&1 || true
    echo "Legacy mail remains paused after rollback; coordinator must review unsent issues before enabling." >&2
  fi
}
