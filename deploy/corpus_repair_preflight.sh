#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${MARX_APP_DIR:-/opt/marx-search}"
OUTPUT_ROOT="${MARX_CORPUS_REPAIR_ROOT:-/home/data/marx-search-corpus-repair}"
LIVE_DB="$APP_DIR/data/corpus.sqlite"
DEVICE_LIMITS=/etc/marx-corpus-repair-devices.env

test -n "${MIMO_API_KEY:-}" || { echo "MIMO_API_KEY is not configured" >&2; exit 78; }
if test -z "${DEEPSEEK_API_KEY:-${APP_AI_API_KEY:-}}"; then
  # The website may keep its established DeepSeek key in config/ai.yaml.
  # Probe only for usability; never print or duplicate the credential.
  PYTHONPATH="$APP_DIR" "$APP_DIR/.venv/bin/python" - <<'PY' || {
from ai import load_ai_config
config = load_ai_config()
if str(config.provider or "").strip().lower() != "deepseek" or not str(config.api_key or "").strip():
    raise SystemExit(1)
PY
    echo "DeepSeek credential is not configured" >&2
    exit 78
  }
fi

test -r /sys/fs/cgroup/cgroup.controllers
controllers="$(cat /sys/fs/cgroup/cgroup.controllers)"
for controller in cpu io memory; do
  case " $controllers " in *" $controller "*) ;; *) echo "missing cgroup v2 controller: $controller" >&2; exit 78;; esac
done

test -r "$DEVICE_LIMITS" || { echo "I/O device limit manifest is missing" >&2; exit 78; }
test "$(stat -c '%U' "$DEVICE_LIMITS")" = root || { echo "I/O device limit manifest is not root-owned" >&2; exit 78; }
case "$(stat -c '%a' "$DEVICE_LIMITS")" in 600|640|644) ;; *) echo "I/O device limit manifest permissions are unsafe" >&2; exit 78;; esac
# The installer writes only validated /dev paths to this root-owned file.
# shellcheck disable=SC1091
source "$DEVICE_LIMITS"

resolved_devices=()
for path in "$LIVE_DB" "$APP_DIR/pdfs" "$OUTPUT_ROOT"; do
  test -e "$path" || { echo "required repair path missing: $path" >&2; exit 78; }
  source="$(findmnt -n -o SOURCE -T "$path" | sed 's/\[.*//')"
  test -n "$source" && test -b "$source" || { echo "cannot resolve block device for $path: $source" >&2; exit 78; }
  resolved_devices+=("$source")
done
test "${resolved_devices[0]}" = "${CORPUS_DB_READ_DEVICE:-}" \
  && test "${resolved_devices[1]}" = "${CORPUS_PDF_READ_DEVICE:-}" \
  && test "${resolved_devices[2]}" = "${CORPUS_OUTPUT_WRITE_DEVICE:-}" \
  || { echo "mounted devices changed after I/O limits were installed" >&2; exit 78; }

available_kib="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
total_kib="$(awk '/MemTotal:/ {print $2}' /proc/meminfo)"
test "$available_kib" -ge $((3*1024*1024)) || { echo "MemAvailable is below 3GiB" >&2; exit 75; }
test $((available_kib*100)) -ge $((total_kib*35)) || { echo "MemAvailable is below 35 percent" >&2; exit 75; }

free_bytes="$(df -B1 --output=avail "$OUTPUT_ROOT" | awk 'NR==2 {print $1}')"
test "$free_bytes" -ge $((15*1024*1024*1024)) || { echo "repair data disk has less than 15GiB free" >&2; exit 75; }

curl -fsS --max-time 5 http://127.0.0.1:8000/api/runtime >/dev/null
curl -fsS --max-time 8 http://127.0.0.1:8000/ >/dev/null
echo CORPUS_REPAIR_PREFLIGHT_OK
