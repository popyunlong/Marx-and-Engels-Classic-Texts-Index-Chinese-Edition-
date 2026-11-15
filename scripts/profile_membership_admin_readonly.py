"""One read-only range-aggregation measurement on existing production data.

Sends only the pure aggregate module, never imports/starts the production app,
never copies the database, and emits no individual user records.
"""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
OUT=Path('D:/CodexData/outputs/membership-admin-20261115')
record_path=OUT/'readonly-production-metrics.json'
source=(ROOT/'admin_dashboard.py').read_bytes()
record={'status':'running_or_interrupted','started':datetime.now(timezone.utc).isoformat(),
        'source_sha256':hashlib.sha256(source).hexdigest(),'scope':'one 30-day read-only aggregate; no app startup or writes'}
with record_path.open('x',encoding='utf-8') as handle:json.dump(record,handle)
remote='''import base64,contextlib,datetime,json,pathlib,sqlite3,sys,time,types
deadline=time.monotonic()+25
queries=[0]
@contextlib.contextmanager
def connect():
    db=sqlite3.connect('file:/var/www/.marx_search_full/membership.sqlite3?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.set_progress_handler(lambda: int(time.monotonic()>deadline),10000)
    db.set_trace_callback(lambda statement: queries.__setitem__(0,queries[0]+1))
    try:yield db
    finally:db.close()
m=types.ModuleType('membership')
m._connect=connect
m.utc_now_text=lambda:datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
sys.modules['membership']=m
module=types.ModuleType('measured_admin_dashboard')
exec(compile(base64.b64decode(PAYLOAD),'<candidate-admin-dashboard>','exec'),module.__dict__)
today=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()
days=[(today-datetime.timedelta(days=i)).isoformat() for i in range(30)]
started=time.monotonic()
rows=module.history(days)
elapsed=time.monotonic()-started
release=json.loads(pathlib.Path('/opt/marx-search/current/release.json').read_text())
print(json.dumps({'duration_seconds':round(elapsed,4),'query_count':queries[0],'days':len(rows),
                 'database_release':release['git_sha'],'individual_records_exported':False,'target_under_5s':elapsed<=5}))
'''.replace('PAYLOAD',repr(base64.b64encode(source).decode('ascii')))
try:
    process=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','marx-cloud','python3','-'],
                           input=remote,text=True,encoding='utf-8',capture_output=True,timeout=45)
    (OUT/'readonly-production-metrics.log').write_text(process.stdout+process.stderr,'utf-8')
    record.update(exit_code=process.returncode,status='failed')
    if process.returncode==0:
        record['measurement']=json.loads(process.stdout)
        record['status']='passed' if record['measurement']['target_under_5s'] else 'target_not_met'
    print(process.stdout+process.stderr)
finally:
    record['finished']=datetime.now(timezone.utc).isoformat()
    record_path.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n','utf-8')
