#!/usr/bin/env bash
# Marx Search 每日数据备份。由 marx-search-backup.timer 触发，也可手动运行。
#
# 备份内容：
#   - 会员库 membership.sqlite3 / 反馈库 feedback.sqlite3：用 sqlite3 在线 .backup（一致
#     快照，对并发写安全、无需停服）。
#   - 文件型数据：/etc/marx-search.env（含支付/邮件/AI 密钥）、session_secret.txt、
#     ai.override.yaml、反馈图片目录 feedback_images。
#   - 可选异地：在 /etc/marx-search.env 配置 RCLONE_REMOTE=remote:bucket/path 后，
#     用 rclone 同步到对象存储（防勒索/删库/磁盘故障一锅端）。
#
# 退路与约束：本机保留最近 BACKUP_KEEP 份；本机备份不能替代异地备份。
set -euo pipefail

APPDATA_DIR="${APPDATA_DIR:-/var/www/.marx_search_full}"
ENV_FILE="${ENV_FILE:-/etc/marx-search.env}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/marx-search}"
KEEP="${BACKUP_KEEP:-14}"

stamp="$(date +%Y%m%d-%H%M%S)"
dest="${BACKUP_ROOT}/${stamp}"
mkdir -p "${dest}"

echo "[backup] 开始：${dest}"

# 1) SQLite 在线安全备份（一致快照）。优先 sqlite3 CLI，缺失则回退到 venv/系统 python。
backup_db() {
  local src="$1" dst="$2"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "${src}" ".backup '${dst}'"
  else
    local py="${BACKUP_PYTHON:-/opt/marx-search/.venv/bin/python}"
    [ -x "${py}" ] || py="python3"
    "${py}" - "${src}" "${dst}" <<'PY'
import sqlite3, sys
s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2])
with d:
    s.backup(d)
s.close(); d.close()
PY
  fi
}
for db in membership.sqlite3 feedback.sqlite3; do
  src="${APPDATA_DIR}/${db}"
  if [ -f "${src}" ]; then
    backup_db "${src}" "${dest}/${db}"
    echo "[backup] 已备份 ${db}"
  fi
done

# 2) 密钥/配置/反馈图片等文件型数据（tar 相对根目录，便于按原路径恢复）
rel=()
add() { if [ -e "$1" ]; then rel+=("${1#/}"); fi; }
add "${ENV_FILE}"
add "${APPDATA_DIR}/session_secret.txt"
add "${APPDATA_DIR}/ai.override.yaml"
add "${APPDATA_DIR}/feedback_images"
if [ ${#rel[@]} -gt 0 ]; then
  tar -czpf "${dest}/files.tar.gz" -C / "${rel[@]}"
  echo "[backup] 已打包文件型数据 files.tar.gz（${#rel[@]} 项）"
fi

# 3) 清理旧备份，仅保留最近 KEEP 份
mapfile -t old < <(ls -1dt "${BACKUP_ROOT}"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) || true)
if [ ${#old[@]} -gt 0 ]; then
  rm -rf "${old[@]}"
  echo "[backup] 已清理 ${#old[@]} 份旧备份（保留 ${KEEP} 份）"
fi

# 4) 可选异地备份（配置 RCLONE_REMOTE 后启用）
if [ -n "${RCLONE_REMOTE:-}" ] && command -v rclone >/dev/null 2>&1; then
  if rclone copy "${dest}" "${RCLONE_REMOTE%/}/${stamp}" --transfers=2 --retries=3; then
    echo "[backup] 已异地同步到 ${RCLONE_REMOTE%/}/${stamp}"
  else
    echo "[backup] 警告：rclone 异地同步失败（本机备份已完成）" >&2
  fi
elif [ -n "${RCLONE_REMOTE:-}" ]; then
  echo "[backup] 警告：配置了 RCLONE_REMOTE 但未安装 rclone，跳过异地同步" >&2
fi

echo "[backup] 完成：${dest}"
