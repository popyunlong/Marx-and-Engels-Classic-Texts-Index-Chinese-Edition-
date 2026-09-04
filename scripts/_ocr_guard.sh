#!/bin/bash
# OCR circuit breaker: protects the live website from the OCR batch job.
#
# 两道保护：
#  ① 站点变慢/失败 -> 立刻 FREEZE marx-ocr 的 cgroup（v2 freezer，OCR 任务瞬间全停、
#     交还 CPU/IO）；连续健康 RECOVER_OK 次后自动 THAW。反应必须远快于站点自身的
#     marx-search-watchdog（它 3 次 10s 超时才重启网站），否则冻结来不及（2026-07-28
#     11:52:26 冻结、11:52:27 网站已被重启，慢了一步 -> 因此改为 10s 探测、1 次即冻结）。
#  ② 硬规则：只要 OCR 运行期间网站被重启过一次，就永久停掉 OCR，不赌第二次。
#     （近 7 天基线：07-22~07-27 共 0 次重启；07-28 跑 OCR 当天 3 次。）
set -u
UNIT_CG=/sys/fs/cgroup/system.slice/marx-ocr.service
PROBE_URL=http://127.0.0.1:8000/api/runtime
PROBE_TIMEOUT=5      # curl hard timeout (s)
SLOW_BUDGET=1.5      # over this many seconds counts as "slow"
STRIKES_TO_FREEZE=1  # 1 次即冻结：误冻结只损失一点 OCR 时间，冻晚了却会导致网站重启
RECOVER_OK=6         # 连续 6 次（60s）健康才解冻，防抖动
PROBE_EVERY=10

strikes=0
good=0
# frozen 每轮从 cgroup 真实状态读取，绝不靠进程内记忆：否则守护自身重启（Restart=always）
# 时会以 frozen=0 起步，而 cgroup 可能已处于冻结态 -> 解冻分支永不触发，OCR 被永久冻死。
frozen=0

log() { echo "$(date '+%F %T') $*"; }

is_ocr_running() { systemctl is-active --quiet marx-ocr; }

freeze() {
  [ -w "$UNIT_CG/cgroup.freeze" ] || { log "cannot write cgroup.freeze (unit gone?)"; return 1; }
  echo 1 > "$UNIT_CG/cgroup.freeze" && frozen=1 && log "FROZE marx-ocr (site unhealthy)"
}

thaw() {
  [ -w "$UNIT_CG/cgroup.freeze" ] || return 1
  echo 0 > "$UNIT_CG/cgroup.freeze" && frozen=0 && log "THAWED marx-ocr (site healthy again)"
}

# 网站启动时刻基线（用于检测 OCR 运行期间网站是否被重启过）
APP_TS_BASE=$(systemctl show marx-search -p ExecMainStartTimestamp --value 2>/dev/null)

log "### ocr guard start (probe ${PROBE_EVERY}s, slow>${SLOW_BUDGET}s, freeze after ${STRIKES_TO_FREEZE}, thaw after ${RECOVER_OK})"
log "### app start baseline: ${APP_TS_BASE}"

while true; do
  if ! is_ocr_running; then
    log "marx-ocr not active -> guard exiting"
    exit 0
  fi

  # ② 硬规则：网站在 OCR 运行期间被重启 -> 永久停 OCR（BindsTo 会连带停掉本守护）
  app_ts_now=$(systemctl show marx-search -p ExecMainStartTimestamp --value 2>/dev/null)
  if [ -n "$app_ts_now" ] && [ -n "$APP_TS_BASE" ] && [ "$app_ts_now" != "$APP_TS_BASE" ]; then
    log "!!! marx-search RESTARTED during OCR (was '${APP_TS_BASE}', now '${app_ts_now}')"
    log "!!! STOPPING marx-ocr permanently - OCR is still disturbing production, needs human decision"
    systemctl stop marx-ocr
    exit 0
  fi

  # 以 cgroup 真实状态为准（自愈：守护重启后也能认出既有冻结态并按恢复条件解冻）
  frozen=$(cat "$UNIT_CG/cgroup.freeze" 2>/dev/null || echo 0)

  t=$( { /usr/bin/time -f "%e" curl -fsS -m "$PROBE_TIMEOUT" -A mazhumonitor "$PROBE_URL" -o /dev/null; } 2>&1 )
  rc=$?
  bad=0
  if [ "$rc" -ne 0 ]; then
    bad=1
    why="probe failed (rc=$rc)"
  else
    # t 形如 "0.02"；用 awk 比较，免 bc 依赖
    if awk -v a="$t" -v b="$SLOW_BUDGET" 'BEGIN{exit !(a+0 > b+0)}'; then
      bad=1
      why="slow ${t}s > ${SLOW_BUDGET}s"
    fi
  fi

  if [ "$bad" -eq 1 ]; then
    good=0
    strikes=$((strikes+1))
    log "unhealthy: $why (strike $strikes/$STRIKES_TO_FREEZE)"
    if [ "$strikes" -ge "$STRIKES_TO_FREEZE" ] && [ "$frozen" -eq 0 ]; then
      freeze
    fi
  else
    strikes=0
    if [ "$frozen" -eq 1 ]; then
      good=$((good+1))
      log "healthy ${t}s while frozen ($good/$RECOVER_OK to thaw)"
      if [ "$good" -ge "$RECOVER_OK" ]; then
        thaw
        good=0
      fi
    fi
  fi

  sleep "$PROBE_EVERY"
done
