#!/usr/bin/env bash
# marx-search 健康看门狗：探测 /api/runtime，连续失败则重启服务（专治「假死挂起」）。
#
# 背景（2026-06-26 宕机）：marx-search.service 是 Restart=always，但那只在进程**退出**时
# 才触发。当 8 线程的 waitress 被慢 AI 调用全部占死时，进程仍在、systemd 认为「active」，
# 却整站无响应约 50 分钟，直到人工重启。systemd 自身抓不到这种「假死」。
#
# 本脚本由 systemd timer 每分钟跑一次：连续探测 /api/runtime 多次都失败（服务在跑但不
# 响应）就强制重启，把宕机自愈窗口从 ~50 分钟压到 ~1-2 分钟。带重启限流，避免崩溃循环时反复抖动。
#
# 安全：本脚本以 root 运行（需 systemctl 权限），故应安装到 root 拥有、www-data 不可写的
# 目录（如 /usr/local/sbin/marx-search-watchdog.sh），不要从 /opt/marx-search 直接跑。
set -u

URL="http://127.0.0.1:8000/api/runtime"
SERVICE="marx-search"
ATTEMPTS=3                    # 连续探测次数：任一成功即判健康（吸收偶发抖动）
GAP=5                        # 两次探测间隔（秒）
CURL_TIMEOUT=10              # 单次探测超时（秒）：>10s 无首字节即视为假死
STAMP="/run/marx-search-watchdog.last-restart"
MIN_RESTART_INTERVAL=120     # 两次自动重启的最小间隔（秒）：防崩溃循环里反复重启

log() { logger -t marx-watchdog "$*" 2>/dev/null || true; echo "marx-watchdog: $*"; }

# 服务本就没在运行 → 交给 systemd 的 Restart=always，看门狗不插手。
if ! systemctl is-active --quiet "$SERVICE"; then
  log "service '$SERVICE' not active; leaving to systemd Restart=always"
  exit 0
fi

# 连续探测：任一成功即健康，正常退出。
for i in $(seq 1 "$ATTEMPTS"); do
  if curl -fsS --max-time "$CURL_TIMEOUT" -o /dev/null "$URL"; then
    exit 0
  fi
  if [ "$i" -lt "$ATTEMPTS" ]; then
    sleep "$GAP"
  fi
done

# 走到这里 = 服务 active 但连续 ${ATTEMPTS} 次探测都失败 = 假死。
# 重启限流：距上次自动重启不足 MIN_RESTART_INTERVAL 秒则按兵不动（宁可让它崩着、可见，
# 也不要在崩溃循环里反复重启把日志和资源搅乱）。
now="$(date +%s)"
if [ -f "$STAMP" ]; then
  last="$(cat "$STAMP" 2>/dev/null || echo 0)"
  if [ -n "$last" ] && [ "$((now - last))" -lt "$MIN_RESTART_INTERVAL" ]; then
    log "health probe failed ${ATTEMPTS}x but auto-restart throttled (last restart $((now - last))s ago < ${MIN_RESTART_INTERVAL}s); not restarting"
    exit 0
  fi
fi

echo "$now" > "$STAMP" 2>/dev/null || true
exec 9>"/run/lock/marx-search-release.lock"
if ! flock -n 9; then
  log "release/rollback transaction is active; deferring watchdog restart"
  exit 0
fi
log "health probe failed ${ATTEMPTS}x (service active but unresponsive) -> restarting ${SERVICE}"
systemctl restart "$SERVICE"
log "restart issued for ${SERVICE}"
exit 0
