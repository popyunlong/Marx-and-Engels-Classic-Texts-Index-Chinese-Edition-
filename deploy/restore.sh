#!/usr/bin/env bash
# 从 backup.sh 产出的某次备份目录恢复 Marx Search 数据。
# 用法： bash restore.sh /var/backups/marx-search/<时间戳>
# 例：   bash restore.sh /var/backups/marx-search/20260608-200000
set -euo pipefail

SRC="${1:-}"
APPDATA_DIR="${APPDATA_DIR:-/var/www/.marx_search_full}"
SERVICE="${SERVICE:-marx-search}"

if [ -z "${SRC}" ] || [ ! -d "${SRC}" ]; then
  echo "用法： bash restore.sh <备份目录>（如 /var/backups/marx-search/20260608-200000）" >&2
  echo "可用备份：" >&2
  ls -1dt /var/backups/marx-search/*/ 2>/dev/null | head -20 >&2 || true
  exit 1
fi

echo "将从 ${SRC} 恢复到 ${APPDATA_DIR}（会覆盖现有会员库/反馈库/密钥/图片）。"
read -r -p "确认继续？输入 yes： " ans
[ "${ans}" = "yes" ] || { echo "已取消。"; exit 1; }

echo "[restore] 停服 ${SERVICE} ..."
systemctl stop "${SERVICE}" || true

for db in membership.sqlite3 feedback.sqlite3; do
  if [ -f "${SRC}/${db}" ]; then
    cp -a "${SRC}/${db}" "${APPDATA_DIR}/${db}"
    chown www-data:www-data "${APPDATA_DIR}/${db}" || true
    chmod 600 "${APPDATA_DIR}/${db}" || true
    echo "[restore] 已恢复 ${db}"
  fi
done

if [ -f "${SRC}/files.tar.gz" ]; then
  tar -xzpf "${SRC}/files.tar.gz" -C /
  echo "[restore] 已恢复密钥/配置/反馈图片"
fi

chown -R www-data:www-data "${APPDATA_DIR}" 2>/dev/null || true

echo "[restore] 启动 ${SERVICE} ..."
systemctl start "${SERVICE}"
sleep 3
if curl -fsS --max-time 8 http://127.0.0.1:8000/api/runtime >/dev/null 2>&1; then
  echo "[restore] 完成，服务已恢复响应。请再手动验证 /admin 登录与阅读器。"
else
  echo "[restore] 警告：服务未在预期时间内响应 /api/runtime，请查 journalctl -u ${SERVICE}。" >&2
fi
