#!/usr/bin/env bash
# Source after setting RELEASE_ID and REVIEW_NONCE inside the release lock.
review_candidate() {
  local fifo="${MARX_REVIEW_FIFO_DIR:-/run}/marx-search-candidate-${RELEASE_ID}.fifo"
  local message="" timeout="${MARX_REVIEW_TIMEOUT_SECONDS:-1800}"
  [ ! -e "$fifo" ] || { echo "candidate review pipe already exists" >&2; return 1; }
  mkfifo -m 0600 "$fifo" || return 1
  REVIEW_FIFO="$fifo"
  REVIEW_OWNED=1
  echo "CANDIDATE_REVIEW_READY=$RELEASE_ID $fifo" >&2
  exec 8<>"$fifo"
  if ! IFS= read -r -t "$timeout" message <&8; then
    exec 8>&-
    rm -f -- "$fifo"
    REVIEW_OWNED=0
    echo "candidate review timed out" >&2
    return 1
  fi
  exec 8>&-
  rm -f -- "$fifo"
  REVIEW_OWNED=0
  [ "$message" = "$RELEASE_ID:$REVIEW_NONCE:PASS" ] || {
    echo "candidate review refused or did not match release" >&2
    return 1
  }
}
