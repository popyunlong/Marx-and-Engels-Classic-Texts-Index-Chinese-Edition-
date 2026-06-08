#!/usr/bin/env bash
# Safe, idempotent upload of ONLY the Lenin 《全集》 PDFs to production.
# Mirrors deploy/upload_lenin_pdfs.ps1 logic but in bash, which handles the
# Chinese paths and scp reliably in a headless context.
# - Only writes under /opt/marx-search/pdfs/列宁《全集》
# - Never touches pdfs/文集 or pdfs/全集, never deletes anything
# - Skips files already present with identical byte size (resumable)
# - Does NOT restart the service, does NOT touch corpus.sqlite
set -euo pipefail

KEY="$HOME/.ssh/id_marx_cloud_ed25519"
HOST="root@38.76.174.234"
REMOTE_BASE="/opt/marx-search"
REMOTE_DIR="$REMOTE_BASE/pdfs/列宁《全集》"
LOCAL_DIR="pdfs/列宁《全集》"

SSH() { ssh -n -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -i "$KEY" "$HOST" "$@"; }
SCP1() { scp -O -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -i "$KEY" "$1" "$HOST:$REMOTE_DIR/"; }

# --- safety: exactly 60 local Lenin PDFs ---
if [ ! -d "$LOCAL_DIR" ]; then echo "ABORT: missing local dir $LOCAL_DIR"; exit 1; fi
lcount=$(find "$LOCAL_DIR" -maxdepth 1 -type f -name '*.pdf' | wc -l)
if [ "$lcount" -ne 60 ]; then echo "ABORT: local PDF count $lcount != 60"; exit 1; fi
echo "Local Lenin PDFs: $lcount"

# --- safety: remote project exists; create only the Lenin dir ---
SSH "test -d '$REMOTE_BASE' && test -d '$REMOTE_BASE/pdfs' && mkdir -p '$REMOTE_DIR'"

uploaded=0; skipped=0; i=0
while IFS= read -r f; do
  i=$((i+1))
  name=$(basename "$f")
  size=$(stat -c%s "$f")
  rsize=$(SSH "stat -c%s '$REMOTE_DIR/$name' 2>/dev/null || echo -1")
  if [ "$rsize" = "$size" ]; then
    skipped=$((skipped+1))
    echo "[$i/60] SKIP same-size ($size): $name"
    continue
  fi
  echo "[$i/60] UPLOAD ($size bytes): $name"
  SCP1 "$f"
  uploaded=$((uploaded+1))
done < <(find "$LOCAL_DIR" -maxdepth 1 -type f -name '*.pdf' | sort)

echo "Fixing permissions ..."
SSH "chown -R www-data:www-data '$REMOTE_DIR' && chmod -R a+rX '$REMOTE_DIR'"

rcount=$(SSH "find '$REMOTE_DIR' -maxdepth 1 -type f -name '*.pdf' | wc -l")
rbytes=$(SSH "find '$REMOTE_DIR' -maxdepth 1 -type f -name '*.pdf' -printf '%s\n' | awk '{s+=\$1} END {print s+0}'")
echo "DONE uploaded=$uploaded skipped=$skipped remote_count=$rcount remote_bytes=$rbytes"
if [ "$rcount" -ne 60 ]; then echo "WARN: remote count $rcount != 60"; exit 2; fi
echo "ALL_OK"
