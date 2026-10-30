#!/usr/bin/env bash
# Canonical source; failed download/validation/reload preserves the live config.
set -euo pipefail
SNIPPET="/etc/caddy/cf_origin_lock.caddy"
CADDYFILE="/etc/caddy/Caddyfile"
exec 9>/run/lock/marx-search-release.lock
flock -w 5 9 || exit 75
work="$(mktemp -d /etc/caddy/cf-sync.XXXXXX)"
trap 'rm -rf -- "$work"' EXIT
curl -fsS --max-time 30 --max-filesize 65536 https://www.cloudflare.com/ips-v4 -o "$work/v4"
curl -fsS --max-time 30 --max-filesize 65536 https://www.cloudflare.com/ips-v6 -o "$work/v6"
python3 - "$work" "$CADDYFILE" "$SNIPPET" <<'PY'
import ipaddress, pathlib, sys
work, config, snippet = map(pathlib.Path, sys.argv[1:])
ranges = []
for name, version, minimum in [('v4', 4, 10), ('v6', 6, 5)]:
    values = (work/name).read_text().splitlines()
    networks = [ipaddress.ip_network(value.strip(), strict=True) for value in values]
    if len(networks) < minimum or len(networks) > 100 or len(set(networks)) != len(networks):
        raise SystemExit('invalid/incomplete Cloudflare ranges')
    if any(n.version != version or n.prefixlen < (8 if version == 4 else 19) for n in networks):
        raise SystemExit('unexpected Cloudflare range')
    ranges.extend(map(str, networks))
ranges += ['127.0.0.0/8', '::1']
networks = ' '.join(ranges)
content = '(cf_origin_lock) {\n\t@deny_direct not remote_ip ' + networks + '\n'
content += '\t@not_cf not remote_ip ' + networks + '\n'
for header in ['CF-Connecting-IP', 'CF-IPCountry', 'True-Client-IP']:
    content += '\trequest_header @not_cf -' + header + '\n'
content += '\trespond @deny_direct 403\n}\n'
(work/'snippet').write_text(content)
source = config.read_text()
needle = 'import ' + str(snippet)
if source.count(needle) != 1:
    raise SystemExit('expected exactly one live snippet import; manual review required')
(work/'Caddyfile').write_text(source.replace(needle, 'import ' + str(work/'snippet')))
PY
cmp -s "$work/snippet" "$SNIPPET" && exit 0
# The service user must also be able to provision imported files and log writers.
chmod 0755 "$work"
chmod 0644 "$work/Caddyfile" "$work/snippet"
timeout 30s runuser -u caddy -- caddy validate --config "$work/Caddyfile" --adapter caddyfile >/dev/null
cp -a "$SNIPPET" "$work/previous"
install -m 0644 "$work/snippet" "$SNIPPET"
if ! timeout 30s runuser -u caddy -- caddy reload --config "$CADDYFILE" --force; then
  cp -a "$work/previous" "$SNIPPET"
  timeout 30s runuser -u caddy -- caddy reload --config "$CADDYFILE" --force || true
  logger -t cf-ip-sync 'reload failed; restored previous snippet'
  exit 1
fi
logger -t cf-ip-sync 'validated Cloudflare ranges installed'
