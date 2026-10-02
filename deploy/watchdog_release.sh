#!/usr/bin/env bash
# Source only inside the release/rollback transaction, after it owns fd 9.
# Older installed watchdogs may not know about the global release lock yet.
WATCHDOG_TIMER_WAS_ACTIVE=0
pause_watchdog_for_release() {
  if systemctl is-active --quiet marx-search-watchdog.timer; then
    WATCHDOG_TIMER_WAS_ACTIVE=1
    systemctl stop marx-search-watchdog.timer
  fi
  # Stop an already running probe too, before it can restart the primary.
  systemctl stop marx-search-watchdog.service
}

resume_watchdog_after_release() {
  if [ "$WATCHDOG_TIMER_WAS_ACTIVE" -eq 1 ]; then
    systemctl start marx-search-watchdog.timer
    WATCHDOG_TIMER_WAS_ACTIVE=0
  fi
}
