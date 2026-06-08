#!/usr/bin/env bash
# Bash equivalent of `update_cloud.ps1 -SkipRestart` (Phase C).
# Reliable in headless context (no PowerShell ssh-stdin hang).
# Pushes code/templates/config, backs up remote, py_compile, remote smoke.
# Does NOT rebuild corpus. Does NOT restart service. Does NOT touch systemd.
set -euo pipefail

KEY="$HOME/.ssh/id_marx_cloud_ed25519"
HOST="root@38.76.174.234"
REMOTE_DIR="/opt/marx-search"
STAMP=$(date +%Y%m%d-%H%M%S)
ARCHIVE="/tmp/marx-cloud-patch-$STAMP.tar.gz"
REMOTE_ARCHIVE="/tmp/marx-cloud-patch.tar.gz"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SSH() { ssh -n -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -i "$KEY" "$HOST" "$@"; }

read_manifest() {
  local manifest="$1"
  grep -vE '^[[:space:]]*(#|$)' "$manifest" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

cd "$REPO_ROOT"
mapfile -t FILES < <(read_manifest "$SCRIPT_DIR/cloud_patch_files.txt")
mapfile -t COMPILE < <(read_manifest "$SCRIPT_DIR/cloud_compile_files.txt")

# --- 0. verify all local files exist ---
echo "Verifying ${#FILES[@]} local files ..."
for f in "${FILES[@]}"; do
  [ -f "$f" ] || { echo "ABORT: missing local file $f"; exit 1; }
done

# --- 1. local smoke ---
echo "Running local deployment smoke ..."
PYTHONIOENCODING=utf-8 python scripts/deployment_smoke.py --mode server

# --- 2. package ---
echo "Packaging patch ..."
tar -czf "$ARCHIVE" "${FILES[@]}"
echo "Patch size: $(du -h "$ARCHIVE" | cut -f1)"

# --- 3. sanity: remote project present ---
SSH "test -f '$REMOTE_DIR/app.py' && test -d '$REMOTE_DIR/templates' && test -d '$REMOTE_DIR/data'"

# --- 4. upload archive ---
echo "Uploading patch archive ..."
scp -O -o BatchMode=yes -o StrictHostKeyChecking=accept-new -i "$KEY" "$ARCHIVE" "$HOST:$REMOTE_ARCHIVE"

# --- 5. remote backup (code only; no PDFs / no corpus) ---
echo "Creating remote lightweight backup ..."
BACKUP=$(SSH "b='$REMOTE_DIR.cloud-backup.$STAMP'; mkdir -p \"\$b\"; cd '$REMOTE_DIR' && cp -a *.py DEPLOY_SERVER.md README.md requirements.txt \"\$b/\" 2>/dev/null || true; for d in templates scripts config deploy; do if [ -e \"\$d\" ]; then mkdir -p \"\$b/\$d\"; cp -a \"\$d/.\" \"\$b/\$d/\" 2>/dev/null || true; fi; done; echo \"\$b\"")
echo "Remote backup: $BACKUP"

# --- 6. extract patch ---
echo "Applying patch (no PDFs, no corpus touched) ..."
SSH "mkdir -p '$REMOTE_DIR/templates' '$REMOTE_DIR/scripts' '$REMOTE_DIR/config' '$REMOTE_DIR/deploy' && tar -xzf '$REMOTE_ARCHIVE' -C '$REMOTE_DIR' && rm -f '$REMOTE_ARCHIVE'"

# --- 7. fix perms ---
echo "Fixing permissions ..."
SSH "cd '$REMOTE_DIR' && chown www-data:www-data *.py templates/*.html scripts/*.py deploy/*.ps1 deploy/*.sh deploy/marx-search-journal-alerts.* config/*.example config/books.yaml config/manifest.yaml config/volumes.yaml config/wenji_toc_overrides.yaml config/quanji_toc_overrides.yaml DEPLOY_SERVER.md 2>/dev/null || true; chmod -R a+rX templates scripts deploy config 2>/dev/null || true"

# --- 8. remote py_compile ---
echo "Remote py_compile ..."
SSH "cd '$REMOTE_DIR' && . .venv/bin/activate && python -m py_compile ${COMPILE[*]}"

# --- 9. remote smoke (import only; no rebuild, no restart) ---
echo "Remote deployment smoke ..."
SSH "cd '$REMOTE_DIR' && . .venv/bin/activate && python scripts/deployment_smoke.py --mode server"

# --- 10. confirm running (old) service still healthy; NO restart ---
echo "Checking running service is still healthy (no restart performed) ..."
SSH "systemctl is-active marx-search && curl -fsS --max-time 5 http://127.0.0.1:8000/api/runtime >/dev/null && echo RUNTIME_OK"

rm -f "$ARCHIVE"
echo "PHASE_C_DONE: code+config patched, compiled, smoke-passed, service NOT restarted, corpus NOT rebuilt."
echo "Remote backup kept at: $BACKUP"
