"""Resume an approved JSON package in its immutable data-disk batch directory."""
import argparse
import hashlib
import json
import re
import shlex
import time
from pathlib import Path
import paramiko
from upload_book_sources import RECEIVER


def main():
    p=argparse.ArgumentParser();p.add_argument('--file',type=Path,required=True)
    p.add_argument('--batch',required=True);p.add_argument('--expected-live',required=True)
    p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,80}',a.batch):raise ValueError('unsafe batch')
    h=hashlib.sha256()
    with a.file.open('rb') as source:
        for block in iter(lambda:source.read(1048576),b''):h.update(block)
    sha=h.hexdigest();size=a.file.stat().st_size
    receiver=RECEIVER.replace("'/home/data/pdfs/自动入库'",repr('/home/data/marx-state-documents/'+a.batch))
    receiver=receiver.replace("target=root/(sha+'.pdf');partial=root/(sha+'.pdf.state-documents-part')",
                              "target=root/'packages.json';partial=root/'packages.json.part'")
    cfg=paramiko.SSHConfig.from_path(str(Path.home()/'.ssh/config')).lookup('marx-cloud')
    c=paramiko.SSHClient();c.load_system_host_keys()
    c.connect(cfg['hostname'],username=cfg['user'],key_filename=cfg['identityfile'][0],timeout=15)
    try:
        command='python3 -u -c '+shlex.quote(receiver)+' '+shlex.quote(a.expected_live)+' '+sha+' '+str(size)
        stdin,stdout,stderr=c.exec_command(command,timeout=120)
        line=stdout.readline()
        if not line:raise RuntimeError(stderr.read().decode())
        state=json.loads(line)
        if not state.get('complete'):
            with a.file.open('rb') as source:
                h=hashlib.sha256();remaining=state['offset']
                while remaining:
                    chunk=source.read(min(1048576,remaining));h.update(chunk);remaining-=len(chunk)
                if h.hexdigest()!=state['prefix_sha256']:raise ValueError('partial prefix differs')
                started=time.monotonic();sent=0
                while chunk:=source.read(65536):
                    sent+=len(chunk);time.sleep(max(0,sent/(2<<20)-(time.monotonic()-started)))
                    stdin.write(chunk);stdin.flush()
            stdin.channel.shutdown_write();line=stdout.readline()
            if not line:raise RuntimeError(stderr.read().decode())
            state=json.loads(line)
        if stdout.channel.recv_exit_status()!=0:raise RuntimeError(stderr.read().decode())
        result=dict(batch=a.batch,bytes=size,sha256=sha,complete=state['complete'])
        a.report.write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
    finally:c.close()


if __name__=='__main__':main()
