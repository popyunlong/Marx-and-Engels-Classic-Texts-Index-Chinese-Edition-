#!/usr/bin/env bash
# 同步 Cloudflare 官方 IP 段，生成 Caddy「源站锁定」snippet：/etc/caddy/cf_origin_lock.caddy
# 语义：既不来自 CF 边缘/本机回环、又不带监控豁免 UA token 的请求 → Caddy 静态 403，不进 waitress。
# 失败模式永远是「保持现状」：下载失败/列表残缺/validate 失败都不会改动线上配置。
# 安装位置：/usr/local/sbin/update_cf_ips.sh（root 属主 0755），由 marx-search-cfip-sync.timer 每周触发。
# 手动触发：systemctl start marx-search-cfip-sync.service
set -euo pipefail

SNIPPET="/etc/caddy/cf_origin_lock.caddy"
# 与 app.py _DEFAULT_MONITORING_UA_TOKENS（mazhumonitor）保持一致；改动须两处同步
MONITOR_UA_REGEX='(?i)mazhumonitor'
V4_URL="https://www.cloudflare.com/ips-v4"
V6_URL="https://www.cloudflare.com/ips-v6"

tmp_v4="$(mktemp)"; tmp_v6="$(mktemp)"; tmp_out="$(mktemp)"
trap 'rm -f "$tmp_v4" "$tmp_v6" "$tmp_out"' EXIT

curl -fsS --max-time 30 "$V4_URL" -o "$tmp_v4"
curl -fsS --max-time 30 "$V6_URL" -o "$tmp_v6"

# 逐行 CIDR 校验 + 数量下限：防截断/劫持的响应生成残缺白名单（否则会把 CF 边缘也 403 掉）
v4_ok=$(grep -Ec '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$' "$tmp_v4" || true)
v4_all=$(grep -c . "$tmp_v4" || true)
v6_ok=$(grep -Ec '^[0-9a-fA-F:]+/[0-9]{1,3}$' "$tmp_v6" || true)
v6_all=$(grep -c . "$tmp_v6" || true)
if [ "$v4_ok" -lt 10 ] || [ "$v4_ok" != "$v4_all" ] || [ "$v6_ok" -lt 5 ] || [ "$v6_ok" != "$v6_all" ]; then
  echo "CF IP 列表校验失败(v4 ${v4_ok}/${v4_all}, v6 ${v6_ok}/${v6_all})，保持现有 snippet 不动" >&2
  exit 1
fi

# 注意：$( ) 会剥末尾空白、官方文件末尾无换行——必须显式加分隔空格，否则 v4/v6 段粘连
ranges="$(tr '\n' ' ' < "$tmp_v4") $(tr '\n' ' ' < "$tmp_v6") 127.0.0.0/8 ::1"
expected=$((v4_ok + v6_ok + 2))
actual=$(echo "$ranges" | wc -w)
if [ "$actual" -ne "$expected" ]; then
  echo "网段拼接数量不符(expected=${expected} actual=${actual})，保持现有 snippet 不动" >&2
  exit 1
fi

# 注意：不要在生成内容里放时间戳——靠 cmp 判断“列表没变就不 reload”
cat > "$tmp_out" <<EOF
# 本文件由 update_cf_ips.sh 自动生成——手改会被下次同步覆盖。
# 放行 = 来自 CF 边缘 或 本机回环 或 带监控豁免 UA；其余一律 403（不进应用）。
(cf_origin_lock) {
	@deny_direct {
		not remote_ip ${ranges}
		not header_regexp User-Agent ${MONITOR_UA_REGEX}
	}
	@not_cf not remote_ip ${ranges}
	request_header @not_cf -CF-Connecting-IP
	request_header @not_cf -CF-IPCountry
	request_header @not_cf -True-Client-IP
	respond @deny_direct 403 {
		body "403 Forbidden: direct origin access is blocked. Use https://mazhuzuojiansuo.com/"
		close
	}
}
EOF

if [ -f "$SNIPPET" ] && cmp -s "$tmp_out" "$SNIPPET"; then
  exit 0
fi
[ -f "$SNIPPET" ] && cp -a "$SNIPPET" "${SNIPPET}.bak"
install -m 0644 "$tmp_out" "$SNIPPET"
if ! caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
  echo "caddy validate 失败，回滚 snippet" >&2
  if [ -f "${SNIPPET}.bak" ]; then cp -a "${SNIPPET}.bak" "$SNIPPET"; else rm -f "$SNIPPET"; fi
  exit 1
fi
systemctl reload caddy
logger -t cf-ip-sync "cf_origin_lock.caddy updated, caddy reloaded"
