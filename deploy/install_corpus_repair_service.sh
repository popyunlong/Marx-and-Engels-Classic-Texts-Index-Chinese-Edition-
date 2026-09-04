#!/usr/bin/env bash
# Install the nightly repair and confirmed-promotion services. Run as root.
set -euo pipefail

APP_DIR="${MARX_APP_DIR:-/opt/marx-search}"
OUTPUT_ROOT="${MARX_CORPUS_REPAIR_ROOT:-/home/data/marx-search-corpus-repair}"
UNIT_DIR=/etc/systemd/system
ENABLE_TIMERS=0

if test "${1:-}" = "--enable"; then
  ENABLE_TIMERS=1
  shift
fi
test "$#" -eq 0 || { echo "usage: $0 [--enable]" >&2; exit 2; }

test "$(id -u)" -eq 0 || { echo "installer must run as root" >&2; exit 2; }
case "$APP_DIR" in /*) ;; *) echo "MARX_APP_DIR must be absolute" >&2; exit 2;; esac
case "$OUTPUT_ROOT" in /*) ;; *) echo "MARX_CORPUS_REPAIR_ROOT must be absolute" >&2; exit 2;; esac
case "$APP_DIR$OUTPUT_ROOT" in *" "*|*"&"*|*"|"*) echo "repair paths may not contain spaces, ampersands, or pipes" >&2; exit 2;; esac
for path in "$APP_DIR/data/corpus.sqlite" "$APP_DIR/pdfs" "$APP_DIR/deploy/marx-corpus-repair.service"; do
  test -e "$path" || { echo "missing install prerequisite: $path" >&2; exit 2; }
done
test -r /sys/fs/cgroup/cgroup.controllers || { echo "cgroup v2 is required" >&2; exit 78; }
controllers="$(cat /sys/fs/cgroup/cgroup.controllers)"
for controller in cpu io memory; do case " $controllers " in *" $controller "*) ;; *) exit 78;; esac; done

if ! getent group marx-repair >/dev/null; then
  groupadd --system marx-repair
fi
if ! getent passwd marx-repair >/dev/null; then
  useradd --system --gid marx-repair --home-dir /nonexistent --shell /usr/sbin/nologin --no-create-home marx-repair
else
  usermod --gid marx-repair marx-repair
fi
install -d -o marx-repair -g www-data -m 3770 "$OUTPUT_ROOT"
install -d -o marx-repair -g marx-repair -m 2750 "$OUTPUT_ROOT/batches"
install -d -o marx-repair -g marx-repair -m 2700 "$OUTPUT_ROOT/backups"
install -d -o marx-repair -g www-data -m 2750 "$OUTPUT_ROOT/evidence"
feedback_db=/var/www/.marx_search_full/feedback.sqlite3
if test -f "$feedback_db" && command -v setfacl >/dev/null; then
  setfacl -m u:marx-repair:--x "$(dirname "$feedback_db")"
  setfacl -m d:u:marx-repair:r-- "$(dirname "$feedback_db")"
  setfacl -m u:marx-repair:r-- "$feedback_db"
  for sidecar in "$feedback_db-wal" "$feedback_db-shm"; do
    test -e "$sidecar" && setfacl -m u:marx-repair:r-- "$sidecar"
  done
fi

for file in corpus_repair_preflight.sh promote_corpus_repair_candidate.sh install_corpus_repair_service.sh; do
  chmod 0755 "$APP_DIR/deploy/$file"
done
for file in marx-corpus-repair.service marx-corpus-repair-promote.service; do
  temp_unit="$(mktemp)"
  sed -e "s|/opt/marx-search|$APP_DIR|g" \
      -e "s|/home/data/marx-search-corpus-repair|$OUTPUT_ROOT|g" \
      "$APP_DIR/deploy/$file" > "$temp_unit"
  install -o root -g root -m 0644 "$temp_unit" "$UNIT_DIR/$file"
  rm -f "$temp_unit"
done
for file in marx-corpus-repair.timer marx-corpus-repair-promote.timer; do
  install -o root -g root -m 0644 "$APP_DIR/deploy/$file" "$UNIT_DIR/$file"
done

install -d -o root -g root -m 0755 "$UNIT_DIR/marx-corpus-repair.service.d"
dropin="$UNIT_DIR/marx-corpus-repair.service.d/20-actual-io-devices.conf"
db_device="$(findmnt -n -o SOURCE -T "$APP_DIR/data/corpus.sqlite" | sed 's/\[.*//')"
pdf_device="$(findmnt -n -o SOURCE -T "$APP_DIR/pdfs" | sed 's/\[.*//')"
out_device="$(findmnt -n -o SOURCE -T "$OUTPUT_ROOT" | sed 's/\[.*//')"
for device in "$db_device" "$pdf_device" "$out_device"; do
  test -b "$device" || { echo "cannot apply I/O limit to unresolved device: $device" >&2; exit 78; }
  case "$device" in /dev/*) ;; *) echo "unexpected block device path: $device" >&2; exit 78;; esac
done
{
  echo '[Service]'
  printf 'IOReadBandwidthMax=%s 2M\n' "$db_device"
  if test "$pdf_device" != "$db_device"; then printf 'IOReadBandwidthMax=%s 2M\n' "$pdf_device"; fi
  printf 'IOWriteBandwidthMax=%s 1M\n' "$out_device"
} > "$dropin"
chown root:root "$dropin"
chmod 0644 "$dropin"

device_manifest=/etc/marx-corpus-repair-devices.env
{
  printf 'CORPUS_DB_READ_DEVICE=%q\n' "$db_device"
  printf 'CORPUS_PDF_READ_DEVICE=%q\n' "$pdf_device"
  printf 'CORPUS_OUTPUT_WRITE_DEVICE=%q\n' "$out_device"
} > "$device_manifest"
chown root:root "$device_manifest"
chmod 0644 "$device_manifest"

# The web process only reads review evidence and records administrator decisions;
# these environment values take effect at its next blue/green deployment.
install -d -o root -g root -m 0755 "$UNIT_DIR/marx-search.service.d"
web_dropin="$UNIT_DIR/marx-search.service.d/30-corpus-repair-review.conf"
{
  echo '[Service]'
  printf 'Environment=MARX_CORPUS_REPAIR_ROOT=%s\n' "$OUTPUT_ROOT"
  printf 'Environment=MARX_CORPUS_REPAIR_DB=%s/review.sqlite3\n' "$OUTPUT_ROOT"
} > "$web_dropin"
chown root:root "$web_dropin"
chmod 0644 "$web_dropin"

systemctl daemon-reload
if test "$ENABLE_TIMERS" -eq 1; then
  systemctl enable --now marx-corpus-repair.timer marx-corpus-repair-promote.timer
  systemctl list-timers marx-corpus-repair.timer marx-corpus-repair-promote.timer --no-pager
  echo "CORPUS_REPAIR_SERVICES_INSTALLED_AND_ENABLED"
else
  echo "CORPUS_REPAIR_SERVICES_INSTALLED_NOT_ENABLED"
  echo "After explicit approval, rerun with --enable to activate both timers."
fi
echo "The main website was not restarted; apply its environment drop-in with the normal blue/green deploy."
