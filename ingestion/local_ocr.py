"""Windows offline OCR helper; all page admission remains on the server."""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time
import uuid
from pathlib import Path

from .client import ApiError, Client, Tunnel
from .settings import load, protect, settings_path


def compute(pipe, models):
    try:
        from .free_ocr import engine, recognize
        ocr=engine(models,3)
        pipe.send({'ready':True})
        while True:
            png=pipe.recv()
            if png is None:return
            started=time.monotonic()
            result=recognize(ocr,png)
            result['unit_seconds']=time.monotonic()-started
            result['executor']='windows-local'
            pipe.send({'ok':True,'result':result})
    except Exception as exc:
        pipe.send({'ok':False,'error':str(exc)[:200]})
    finally:pipe.close()


def save_json(path, value):
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    os.replace(temp,path)


def submit_saved(client, path):
    """An uncertain response is retried with the exact original lease and payload."""
    data=json.loads(path.read_text(encoding='utf-8'))
    try:client.request('POST','/free-ocr/result',data,timeout=5)
    except ApiError as exc:
        if exc.status!=400:raise
        # Preserve expired evidence for inspection, never apply it to a new lease.
        path.replace(path.with_suffix('.expired.json'))
        return False
    path.unlink()
    return True


def run(models, stop_file, *, once=False):
    root=settings_path().parent
    root.mkdir(parents=True,exist_ok=True)
    owner=uuid.uuid4().hex
    state=root/'local-ocr-status.json'
    ctx=mp.get_context('spawn')
    process=pipe=tunnel=client=None
    count=0
    def status(message,**details):
        save_json(state,dict(heartbeat=time.time(),state=message,completed=count,**details))
    try:
        while not stop_file.exists():
            try:
                if process is None or not process.is_alive():
                    status('正在检查本机免费 OCR 模型')
                    if pipe:pipe.close()
                    pipe,child=ctx.Pipe()
                    process=ctx.Process(target=compute,args=(child,str(models)),daemon=True)
                    process.start();child.close()
                    if not pipe.poll(45):raise RuntimeError('本机 OCR 模型初始化超时')
                    ready=pipe.recv()
                    if not ready.get('ready'):raise RuntimeError(ready.get('error','本机模型初始化失败'))
                if client is None:
                    cfg=load()
                    tunnel=Tunnel(cfg['host'],cfg['user'],cfg['key'])
                    client=Client(f'http://127.0.0.1:{tunnel.port}',protect(cfg['encrypted_token'],decrypt=True))
                for path in root.glob('ocr-result-*.pending.json'):
                    submit_saved(client,path)
                started=time.monotonic()
                job=client.request('POST','/free-ocr/claim',{'owner':owner},timeout=5)['job']
                if not job:
                    status('等待免费 OCR 页面；服务器保留校注优先权')
                    time.sleep(2);continue
                status('正在本机免费识别',book=job['book'],page=job['page'])
                saved=False
                try:
                    png=client.request('POST','/free-ocr/image',job,raw=True,timeout=10)
                    pipe.send(png)
                    remaining=max(0,50-(time.monotonic()-started))
                    if not pipe.poll(remaining):
                        process.terminate();process.join(timeout=1)
                        raise RuntimeError('本机单页达到处理时限，已保存队列进度')
                    outcome=pipe.recv()
                    if not outcome.get('ok'):raise RuntimeError(outcome.get('error','本机识别失败'))
                    result=outcome['result']
                    result['end_to_end_seconds']=time.monotonic()-started
                    path=root/f"ocr-result-{job['book']}-{job['page']}-{job['token']}.pending.json"
                    save_json(path,{**job,'result':result})
                    saved=True
                    if submit_saved(client,path):
                        count+=1
                        status('本机识别结果已保存至服务器',book=job['book'],page=job['page'],
                               seconds=round(result['end_to_end_seconds'],2))
                    if once:return
                except Exception as exc:
                    if not saved:
                        try:client.request('POST','/free-ocr/failure',{**job,'error':str(exc)[:180]},timeout=3)
                        except Exception:pass
                    raise
            except ApiError as exc:
                status(str(exc))
                if exc.status!=423:
                    if tunnel:tunnel.close()
                    client=tunnel=None
                time.sleep(2)
            except Exception as exc:
                status('本机 OCR 等待恢复：'+str(exc)[:200])
                if tunnel:tunnel.close()
                client=tunnel=None
                if process and process.is_alive():process.terminate();process.join(timeout=1)
                process=None
                if once:raise
                time.sleep(5)
    finally:
        if process and process.is_alive():process.terminate();process.join(timeout=1)
        if pipe:pipe.close()
        if client:
            try:client.request('POST','/free-ocr/release',{'owner':owner},timeout=3)
            except Exception:pass
        if tunnel:tunnel.close()
        status('本机免费 OCR 已停止，服务器可接替处理')


def main():
    parser=argparse.ArgumentParser()
    default=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))/'models'
    parser.add_argument('--models',type=Path,default=default)
    parser.add_argument('--stop-file',type=Path,default=settings_path().parent/'local-ocr.stop')
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    root=settings_path().parent;root.mkdir(parents=True,exist_ok=True)
    lock=(root/'local-ocr.lock').open('a+b')
    try:
        if os.name=='nt':
            import msvcrt,ctypes
            lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
            try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:return
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(),0x4000)
        run(args.models,args.stop_file,once=args.once)
    finally:lock.close()


if __name__=='__main__':
    mp.freeze_support()
    main()
