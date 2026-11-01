"""Coordinator-only OCR component promotion/rollback. Never starts book jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import time
import urllib.request
import urllib.error
from pathlib import Path, PurePosixPath

ROOT=Path('/opt/mylib-ocr')
SITE=Path('/etc/nginx/sites-enabled/mylib')
BEGIN='# BEGIN MANAGED MYLIB OCR\n'
END='# END MANAGED MYLIB OCR\n'
ROUTE=BEGIN+'''    location ^~ /node/ocr/ {
        proxy_pass http://127.0.0.1:8801/ocr/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
        client_max_body_size 4k;
    }
'''+END


def route_config(text, *, paused=False):
    if text.count('location /node/ {')!=1 or 'allow 38.76.174.234;' not in text or not re.search(r'deny\s+all;',text):
        raise ValueError('Storage ACL/proxy baseline is not recognized')
    if BEGIN in text or END in text:
        if text.count(BEGIN)!=1 or text.count(END)!=1:
            raise ValueError('Ambiguous OCR route markers')
        text=text[:text.index(BEGIN)]+text[text.index(END)+len(END):]
    route=(BEGIN+'    location ^~ /node/ocr/ { return 503; }\n'+END) if paused else ROUTE
    return text.replace('    location /node/ {',route+'\n    location /node/ {',1)


def call(*args):
    return subprocess.check_output(args,text=True).strip()


def current_release():
    link=ROOT/'current'
    if not link.exists():
        return 'absent'
    target=link.resolve()
    if target.parent!=(ROOT/'releases').resolve():
        raise ValueError('OCR current points outside immutable releases')
    return target.name


def verify(directory):
    if any(p.is_symlink() for p in directory.rglob('*')):
        raise ValueError('Symlinks are not permitted in immutable OCR source')
    metadata=json.loads((directory/'release.json').read_text())
    if metadata.get('component')!='mylib-ocr' or not re.fullmatch('[a-f0-9]{40}',metadata.get('git_sha','')):
        raise ValueError('Invalid component metadata')
    actual={str(p.relative_to(directory)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file() and p.relative_to(directory).as_posix()!='release.json'}
    if actual!=metadata['files']:
        raise ValueError('OCR release file hash mismatch')
    if metadata['release_id']!=directory.name:
        raise ValueError('OCR release identifier mismatch')
    return metadata


def unpack(archive,target):
    if target.exists():
        raise ValueError('Release directory already exists; never overwrite')
    target.mkdir(parents=True)
    with tarfile.open(archive,'r:gz') as package:
        for member in package.getmembers():
            name=PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or not member.isfile() or member.size>2*1024*1024:
                raise ValueError('Unsafe component archive member')
            path=target.joinpath(*name.parts)
            path.parent.mkdir(parents=True,exist_ok=True)
            with package.extractfile(member) as stream:
                path.write_bytes(stream.read())
    verify(target)
    for path in target.rglob('*'):
        path.chmod(0o555 if path.is_dir() else 0o444)
    target.chmod(0o555)


def switch(target):
    temporary=ROOT/('current-'+str(os.getpid()))
    temporary.symlink_to(target,target_is_directory=True)
    os.replace(temporary,ROOT/'current')


def health():
    # Read only the configured bearer value at its standard service configuration source.
    token=''
    for line in Path('/etc/mylib.env').read_text().splitlines():
        if line.startswith('MYLIB_WORKER_TOKEN='):
            token=line.split('=',1)[1].strip().strip('\"\'')
    if not token:
        raise ValueError('Configured node bearer is missing')
    req=urllib.request.Request('http://127.0.0.1:8801/ocr/health',headers={'Authorization':'Bearer '+token})
    with urllib.request.urlopen(req,timeout=5) as response:
        result=json.load(response)
    if result.get('protocol_version')!=2 or result.get('dispatch_enabled'):
        raise ValueError('OCR API protocol/admission check failed')
    values=call('systemctl','show','mylib-ocr-worker','-p','MemoryHigh','-p','MemoryMax','-p','MemorySwapMax','-p','OOMPolicy')
    required={'MemoryHigh=3221225472','MemoryMax=4294967296','MemorySwapMax=0','OOMPolicy=kill'}
    if not required<=set(values.splitlines()):
        raise ValueError('OCR memory controls did not take effect')


def main():
    import fcntl
    import importlib.metadata
    import pwd
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=['promote','rollback'])
    parser.add_argument('--expected',required=True)
    parser.add_argument('--target',required=True)
    parser.add_argument('--archive')
    parser.add_argument('--sha256')
    args=parser.parse_args()
    if not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',v) for v in [args.expected,args.target]):
        raise ValueError('Unsafe release identity')
    with Path('/run/lock/marx-search-release.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if current_release()!=args.expected:
            raise ValueError('OCR current release changed; transaction rejected')
        ROOT.mkdir(parents=True,exist_ok=True)
        shared=ROOT/'shared';shared.mkdir(exist_ok=True,mode=0o700)
        account=pwd.getpwnam('minio-user')
        os.chown(shared,account.pw_uid,account.pw_gid)
        # A process/service restart must not resume work during promotion or rollback.
        old_site=SITE.read_text()
        old_release=current_release()
        units=['mylib-ocr-worker.service','mylib-ocr-api.service']
        old_units={unit:(Path('/etc/systemd/system')/unit).read_bytes() if (Path('/etc/systemd/system')/unit).exists() else None for unit in units}
        old_enabled={unit:subprocess.run(['systemctl','is-enabled','--quiet',unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0 for unit in units}
        database=shared/'jobs.sqlite3'
        if args.operation=='promote' and database.exists():
            with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
                if db.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone():
                    raise ValueError('An OCR job is running; do not interrupt it for promotion')
        (shared/'dispatch.enabled').unlink(missing_ok=True)
        target=ROOT/'releases'/args.target
        if args.operation=='promote':
            archive=Path(args.archive or '')
            if not re.fullmatch('[a-f0-9]{64}',args.sha256 or '') or hashlib.sha256(archive.read_bytes()).hexdigest()!=args.sha256:
                raise ValueError('Uploaded OCR archive hash mismatch')
            unpack(archive,target)
        elif args.target!='absent':
            if not target.is_dir():
                raise ValueError('Rollback target is missing')
            verify(target)
        backup=shared/'release-events';backup.mkdir(exist_ok=True,mode=0o700)
        event=backup/(str(time.time_ns())+'.json')
        event.write_text(json.dumps({'operation':args.operation,'from':old_release,'to':args.target,'state':'starting','nginx_before':old_site}))
        changed=False
        try:
            if args.target!='absent':
                for name,version in json.loads((target/'mylib_ocr/runtime.json').read_text()).items():
                    if importlib.metadata.version(name)!=version:
                        raise ValueError('Existing OCR runtime dependency differs: '+name+'; no automatic upgrade')
            # Only new OCR services are stopped; storage/MinIO remain running.
            for unit in units:
                subprocess.run(['systemctl','stop',unit],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if args.operation=='rollback' and args.target=='absent':
                SITE.write_text(route_config(old_site,paused=True))
                call('nginx','-t');call('systemctl','reload','nginx')
                (ROOT/'current').unlink(missing_ok=True)
                for unit in units:
                    subprocess.run(['systemctl','disable',unit],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                event.write_text(json.dumps({'operation':'rollback','from':old_release,'to':'absent','state':'complete','dispatch':'paused'}))
                return
            switch(target);changed=True
            for unit in units:
                shutil.copyfile(target/'deploy'/unit,Path('/etc/systemd/system')/unit)
            call('systemctl','daemon-reload')
            call('systemctl','enable','mylib-ocr-api','mylib-ocr-worker')
            call('systemctl','start','mylib-ocr-api','mylib-ocr-worker')
            deadline=time.monotonic()+30
            while True:
                try:
                    health();break
                except (urllib.error.URLError,ConnectionError,TimeoutError):
                    if time.monotonic()>deadline: raise
                    time.sleep(1)
            if SITE.read_text()!=old_site:
                raise ValueError('Storage proxy changed during the transaction')
            SITE.write_text(route_config(old_site))
            call('nginx','-t')
            call('systemctl','reload','nginx')
            event.write_text(json.dumps({'operation':args.operation,'from':old_release,'to':args.target,'state':'complete','dispatch':'paused','nginx_before':old_site}))
        except BaseException:
            (shared/'dispatch.enabled').unlink(missing_ok=True)
            SITE.write_text(old_site if old_release!='absent' else route_config(old_site,paused=True))
            call('nginx','-t');call('systemctl','reload','nginx')
            if changed:
                for unit in units:
                    subprocess.run(['systemctl','stop',unit],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                if old_release!='absent':
                    switch(ROOT/'releases'/old_release)
                else:
                    (ROOT/'current').unlink(missing_ok=True)
                for unit,data in old_units.items():
                    path=Path('/etc/systemd/system')/unit
                    if data is None:
                        subprocess.run(['systemctl','disable',unit],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(data)
                        subprocess.run(['systemctl','enable' if old_enabled[unit] else 'disable',unit],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                call('systemctl','daemon-reload')
                if old_release!='absent':call('systemctl','start','mylib-ocr-api','mylib-ocr-worker')
            event.write_text(json.dumps({'operation':args.operation,'from':old_release,'to':args.target,'state':'rolled_back','dispatch':'paused'}))
            raise


if __name__=='__main__':
    main()
