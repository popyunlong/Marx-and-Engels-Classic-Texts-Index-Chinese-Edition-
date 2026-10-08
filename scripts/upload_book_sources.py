"""Resume hash-addressed originals on the data disk under the release lock."""
import argparse
import hashlib
import json
import shlex
import time
from pathlib import Path
import paramiko

RECEIVER=r'''
import contextlib,fcntl,hashlib,json,pathlib,shutil,sys,time,urllib.request
expected,sha,size=sys.argv[1],sys.argv[2],int(sys.argv[3])
assert len(sha)==64 and all(c in '0123456789abcdef' for c in sha)
lock=open('/run/lock/marx-search-release.lock','a')
@contextlib.contextmanager
def transaction():
    deadline=time.monotonic()+120
    while True:
        try:
            fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB);break
        except BlockingIOError:
            if time.monotonic()>=deadline:raise
            time.sleep(1)
    try:yield
    finally:fcntl.flock(lock,fcntl.LOCK_UN)
def check():
    data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/runtime',timeout=10))
    if data['app_release']['id']!=expected or not data['ok']:raise ValueError('live version/health changed')
check()
root=pathlib.Path('/home/data/pdfs/自动入库')
with transaction():root.mkdir(parents=True,exist_ok=True)
target=root/(sha+'.pdf');partial=root/(sha+'.pdf.state-documents-part')
def fingerprint(path):
    h=hashlib.sha256()
    if path.exists():
        with path.open('rb') as f:
            for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h
if target.exists():
    if target.stat().st_size!=size or fingerprint(target).hexdigest()!=sha:raise ValueError('existing original mismatch')
    print(json.dumps({'complete':True}),flush=True);sys.exit()
offset=partial.stat().st_size if partial.exists() else 0
if offset>size:raise ValueError('partial exceeds original')
if shutil.disk_usage(root).free-(size-offset)<15*1024**3:raise OSError('data reserve would be violated')
h=fingerprint(partial)
print(json.dumps({'offset':offset,'prefix_sha256':h.hexdigest()}),flush=True)
count=offset;last_check=time.monotonic()
with transaction():f=partial.open('ab',buffering=0)
with f:
    # A private file lock prevents concurrent resumptions of this one original;
    # network waits never own the global lock needed by health recovery.
    fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if f.tell()!=offset:raise ValueError('partial changed before resume')
    while count<size:
        b=sys.stdin.buffer.read(min(65536,size-count))
        if not b:raise EOFError('interrupted transfer retained for resume')
        if time.monotonic()-last_check>=5:check();last_check=time.monotonic()
        with transaction():
            if f.write(b)!=len(b):raise OSError('short original write')
        h.update(b);count+=len(b)
if count!=size or h.hexdigest()!=sha:raise ValueError('original checksum mismatch')
with transaction():
    check();partial.chmod(0o444);partial.rename(target)
print(json.dumps({'complete':True,'bytes':count,'sha256':sha}),flush=True)
'''


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True)
    p.add_argument('--inventory',type=Path,required=True);p.add_argument('--expected-live',required=True)
    p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    cfg=paramiko.SSHConfig.from_path(str(Path.home()/'.ssh/config')).lookup('marx-cloud')
    c=paramiko.SSHClient();c.load_system_host_keys()
    c.connect(cfg['hostname'],username=cfg['user'],key_filename=cfg['identityfile'][0],timeout=15)
    report=[]
    try:
        for item in json.loads(a.inventory.read_text('utf-8')):
            if item.get('upload') is False:continue
            path=a.source/item['pdf'];sha=item['source_sha256'];size=path.stat().st_size
            command='python3 -u -c '+shlex.quote(RECEIVER)+' '+shlex.quote(a.expected_live)+' '+sha+' '+str(size)
            stdin,stdout,stderr=c.exec_command(command,timeout=180)
            line=stdout.readline()
            if not line:raise RuntimeError(stderr.read().decode())
            state=json.loads(line)
            if not state.get('complete'):
                with path.open('rb') as f:
                    h=hashlib.sha256();remaining=state['offset']
                    while remaining:
                        chunk=f.read(min(1<<20,remaining));h.update(chunk);remaining-=len(chunk)
                    if h.hexdigest()!=state['prefix_sha256']:raise ValueError('partial prefix differs')
                    sent=0;start=time.monotonic()
                    while chunk:=f.read(65536):
                        sent+=len(chunk);time.sleep(max(0,sent/(2<<20)-(time.monotonic()-start)))
                        stdin.write(chunk);stdin.flush()
                stdin.channel.shutdown_write()
                last=stdout.readline()
                if not last:raise RuntimeError(stderr.read().decode())
                state=json.loads(last)
            if stdout.channel.recv_exit_status()!=0:raise RuntimeError(stderr.read().decode())
            report.append(dict(file=path.name,sha256=sha,bytes=size,complete=state['complete']))
            a.report.parent.mkdir(parents=True,exist_ok=True)
            a.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report[-1],ensure_ascii=False),flush=True)
    finally:c.close()


if __name__=='__main__':main()
