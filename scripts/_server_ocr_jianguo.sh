#!/bin/bash
# Server-side OCR driver: JianGuo (建国以来) vol8-20.
# Runs under marx-ocr.service, which pins it to ONE core (AllowedCPUs=3) with
# CPUQuota=80% / MemoryMax=2G / Nice=19 so the website's cores are never contended.
# Sidecars go to data/ symlinks -> /home/data/ocr_sidecars (data disk). Resumable.
# --threads 1 on purpose: the cgroup only grants one core, extra threads just thrash.
set -u
cd /opt/marx-search || exit 1
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
echo "### server OCR driver start $(date '+%F %T') pid=$$"
for n in 08 09 10 11 12 13 14 15 16 17 18 19 20; do
  pdf="pdfs/建国以来重要文献选编/建国以来重要文献选编_第${n}册.pdf"
  out="data/jianguo_vol${n}_ocr.jsonl"
  if [ ! -f "$pdf" ]; then echo "[skip] vol${n} pdf missing"; continue; fi
  echo "=== jianguo vol${n} start $(date '+%F %T') ==="
  .venv/bin/python scripts/_ocr_scan_volume.py --pdf "$pdf" --out "$out" --threads 1
  echo "=== jianguo vol${n} done  $(date '+%F %T') lines=$(wc -l < "$out" 2>/dev/null) ==="
done
echo "### ALL jianguo vol8-20 OCR finished $(date '+%F %T')"
