#!/usr/bin/env bash
# Install a checked main archive on the library host. Never releases the website.
set -Eeuo pipefail
umask 027
[ "$#" -eq 4 ] || { echo 'usage: install_paddle_worker.sh ARCHIVE CHECKED_MAIN_SHA PYTHON EXPECTED_WORKER_ID' >&2; exit 2; }
ARCHIVE="$1"; REVISION="$2"; PYTHON="$3"; EXPECTED="$4"
[[ "$REVISION" =~ ^[0-9a-f]{40}$ ]] || exit 2
[ "$(id -u)" -eq 0 ] || exit 2
[ ! -e /opt/marx-search ] || { echo 'Refusing application host' >&2; exit 2; }
mountpoint -q /home/data || { echo 'Data disk is not mounted' >&2; exit 2; }
exec 9>/run/lock/marx-search-release.lock
flock -n 9 || exit 75
ROOT=/home/data/marx-corpus-repair
ID="paddle-$REVISION"
LIVE=none
if [ -L "$ROOT/current" ]; then LIVE="$(basename "$(readlink -f "$ROOT/current")")"; fi
[ "$LIVE" = "$EXPECTED" ] || { echo 'Worker changed since preparation' >&2; exit 73; }
if systemctl is-active --quiet marx-paddle-repair.service; then
  echo 'Save the active worker checkpoint before upgrading' >&2; exit 75
fi
"$PYTHON" -c 'import fitz, sys; assert sys.version_info >= (3,10)'
python3 - "$ROOT" "$ARCHIVE" <<'PY'
import pathlib,shutil,sys,tarfile
root=pathlib.Path(sys.argv[1]);disk=shutil.disk_usage(root)
with tarfile.open(sys.argv[2],'r:gz') as tar:
    names=set();size=0
    for member in tar.getmembers():
        p=pathlib.PurePosixPath(member.name)
        name=p.as_posix()
        if (not (member.isfile() or member.isdir()) or p.is_absolute() or '..' in p.parts
                or '\\' in member.name or ':' in member.name or name in names
                or not (name=='release.json' and member.isfile() or p.parts and p.parts[0]=='app')):
            raise SystemExit('worker archive must contain unique safe files and directories')
        names.add(name)
        if member.isfile():size+=member.size
    if disk.free-size<max(15*1024**3,disk.total*.2):raise SystemExit('insufficient disk reserve')
PY
install -d -o root -g marx-paddle-repair -m 0750 "$ROOT/releases"
FINAL="$ROOT/releases/$ID"
[ ! -e "$FINAL" ] || { echo 'Worker revision already installed; never overwrite' >&2; exit 3; }
mkdir -m 0750 "$FINAL"
tar -xzf "$ARCHIVE" -C "$FINAL" --no-same-owner --no-same-permissions
python3 "$FINAL/app/scripts/build_release_manifest.py" verify \
  --source-dir "$FINAL/app" --metadata "$FINAL/release.json" --release-id "$ID" --git-sha "$REVISION"
chown -R root:marx-paddle-repair "$FINAL"
find "$FINAL" -type d -exec chmod 0550 {} +
find "$FINAL" -type f -exec chmod 0440 {} +
ln -s "$FINAL" "$ROOT/current.pending"
mv -Tf "$ROOT/current.pending" "$ROOT/current"
ln -sfn "$PYTHON" "$ROOT/runtime-python"
if [ ! -e "$ROOT/scope.json" ]; then
  printf '%s\n' '{"phase":"smoke","max_total_pages":12,"daily_budget":18000}' > "$ROOT/scope.json"
  chown root:marx-paddle-repair "$ROOT/scope.json"
  chmod 0440 "$ROOT/scope.json"
fi
install -o root -g root -m 0644 "$FINAL/app/deploy/marx-paddle-repair.service" /etc/systemd/system/marx-paddle-repair.service
systemctl daemon-reload
# Startup is a separate checked step after source planning and secret provisioning.
echo "Installed $ID; worker remains stopped, scope capped at 12 pages."
