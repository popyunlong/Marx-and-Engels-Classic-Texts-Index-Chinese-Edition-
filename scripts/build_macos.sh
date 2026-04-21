#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "${PROJECT_ROOT}"

"${PYTHON_BIN}" -m pip install --upgrade pip
"${PYTHON_BIN}" -m pip install -r requirements.txt -r requirements-build.txt
"${PYTHON_BIN}" scripts/make_icns.py marx_multisize.ico build/icons/marx_multisize.icns
"${PYTHON_BIN}" -m PyInstaller --noconfirm --clean app.spec

ARTIFACT="${PROJECT_ROOT}/dist/马恩文集全集检索程序.app"
if [[ ! -d "${ARTIFACT}" ]]; then
  echo "Build succeeded but artifact was not found: ${ARTIFACT}" >&2
  exit 1
fi

echo
echo "macOS artifact ready:"
echo "${ARTIFACT}"
