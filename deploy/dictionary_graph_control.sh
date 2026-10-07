#!/usr/bin/env bash
# Feature-only emergency control; never changes app source, upstream or service.
set -euo pipefail
[ "$#" -eq 3 ] || { echo 'usage: dictionary_graph_control.sh ROOT EXPECTED_RELEASE on|off' >&2; exit 2; }
ROOT="$1"; EXPECTED="$2"; MODE="$3"
[[ "$ROOT" =~ ^/[A-Za-z0-9._/-]+$ && "$ROOT" != / ]] || exit 2
[[ "$EXPECTED" =~ ^[A-Za-z0-9][A-Za-z0-9._:+-]{0,191}$ ]] || exit 2
[[ "$MODE" == on || "$MODE" == off ]] || exit 2
exec 9>/run/lock/marx-search-release.lock
flock -n 9 || exit 75
python3 - "$ROOT" "$EXPECTED" "$MODE" <<'PY'
import datetime,json,os,sys,tempfile
from pathlib import Path
root=Path(sys.argv[1]);expected=sys.argv[2];mode=sys.argv[3]
meta=json.loads((root/'current/release.json').read_text())
if meta['release_id']!=expected:raise SystemExit('stale dictionary graph control rejected')
if mode=='on':
    sys.path.insert(0,str(root/'current/app'))
    from scripts.dictionary_graph_deploy import verify
    if verify(root,root/'current/app') is None:raise SystemExit('no graph bound to this release')
value={'disabled':mode=='off','release_id':expected,'at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
with (root/'release-ledger.jsonl').open('a') as f:
    f.write(json.dumps(dict(value,event='dictionary_graph_control_attempt'))+'\n');f.flush();os.fsync(f.fileno())
fd,name=tempfile.mkstemp(prefix='.dictionary-graph-control-',dir=root/'data')
try:
    with os.fdopen(fd,'w') as f:json.dump(value,f);f.flush();os.fsync(f.fileno())
    os.chmod(name,0o644)
    os.replace(name,root/'data/dictionary-graph-control.json')
finally:
    if os.path.exists(name):os.unlink(name)
with (root/'release-ledger.jsonl').open('a') as f:
    f.write(json.dumps(dict(value,event='dictionary_graph_control'))+'\n');f.flush();os.fsync(f.fileno())
print(json.dumps(value))
PY
