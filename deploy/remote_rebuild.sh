#!/usr/bin/env bash
# Runs ON the production server. Rebuilds corpus.sqlite (all books incl. Lenin)
# then rebuilds toc for wenji/quanji/lenin, and fixes DB ownership.
# Mirrors `update_cloud.ps1 -RebuildCorpus`. Does NOT restart the service.
set -euo pipefail
cd /opt/marx-search
. .venv/bin/activate
export PYTHONIOENCODING=utf-8

echo "=== build_index START $(date -u) ==="
python build_index.py
echo "=== wenji toc ==="
python scripts/build_wenji_toc.py
echo "=== quanji toc ==="
python scripts/build_quanji_toc.py
echo "=== lenin toc ==="
python scripts/build_toc.py --book '列宁全集'

DBP=$(python -c 'import build_index; print(build_index.DB_PATH)')
chown www-data:www-data "$DBP" "$DBP.sha256"

echo "=== DB COUNTS ==="
python - <<'PY'
import sqlite3, build_index
c = sqlite3.connect(build_index.DB_PATH)
print("pages:", c.execute("select book,count(*) from pages group by book order by book").fetchall())
print("toc:  ", c.execute("select book,count(*) from toc_entries group by book order by book").fetchall())
c.close()
PY
echo "=== REBUILD_ALL_OK $(date -u) ==="
