#!/usr/bin/env bash
# Source after setting RELEASE_ID and REVIEW_NONCE inside the release lock.
review_candidate() {
  local fifo="${MARX_REVIEW_FIFO_DIR:-/run}/marx-search-candidate-${RELEASE_ID}.fifo"
  # Thirty minutes of paired observation must fit alongside warm-up and UI review.
  local message="" timeout="${MARX_REVIEW_TIMEOUT_SECONDS:-5400}"
  [ ! -e "$fifo" ] || { echo "candidate review pipe already exists" >&2; return 1; }
  mkfifo -m 0600 "$fifo" || return 1
  REVIEW_FIFO="$fifo"
  REVIEW_OWNED=1
  echo "CANDIDATE_REVIEW_READY=$RELEASE_ID $fifo" >&2
  exec 8<>"$fifo"
  local deadline=$((SECONDS + timeout)) remaining slice received=0 busy_samples=0 pressure
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ -n "${CATALOG_OBSERVER_PID:-}" ]; then
      if ! python3 "$FINAL/app/scripts/catalog_observe.py" --server-app "$FINAL/app" --server-status; then
        echo "candidate review interrupted by server observation failure" >&2
        break
      fi
    fi
    # The transaction owns the release lock and temporarily pauses watchdogs.
    # Keep checking both processes while browser review and paired observation run.
    if declare -F health >/dev/null; then
      if ! health "$PRIMARY_PORT" "$EXPECTED_LIVE" || ! health "$CANDIDATE_PORT" "$RELEASE_ID"; then
        echo "candidate review interrupted by primary/candidate health failure" >&2
        break
      fi
      # Already inside the exclusive release transaction; no second lock or
      # production write is needed for these low-cost kernel counters.
      if ! pressure=$(python3 "$FINAL/app/scripts/catalog_review.py" --resources); then
        echo "candidate resource observation unavailable" >&2
        break
      fi
      echo "CANDIDATE_RESOURCE_OBSERVATION=$pressure" >&2
      if [[ "$pressure" == *'"pressured": true'* ]]; then
        busy_samples=$((busy_samples + 1))
      else
        busy_samples=0
      fi
      if [ "$busy_samples" -ge 2 ]; then
        echo "candidate review interrupted by sustained resource contention" >&2
        break
      fi
    fi
    remaining=$((deadline - SECONDS))
    [ "$remaining" -gt 0 ] || break
    slice=$remaining
    [ "$slice" -le 30 ] || slice=30
    if IFS= read -r -t "$slice" message <&8; then received=1; break; fi
  done
  exec 8>&-
  rm -f -- "$fifo"
  REVIEW_OWNED=0
  [ "$received" = 1 ] || { echo "candidate review expired or health failed" >&2; return 1; }
  [ "$message" = "$RELEASE_ID:$REVIEW_NONCE:PASS" ] || {
    echo "candidate review refused or did not match release" >&2
    return 1
  }
}
