"""Network backpressure must not hold the global recovery/release lock."""
import ast
import hashlib
import io
import json
from pathlib import Path
import sys
import types
import urllib.request


def test_original_receiver_releases_global_lock_while_waiting_for_network(tmp_path, monkeypatch):
    module=ast.parse((Path(__file__).resolve().parents[1]/'scripts/upload_book_sources.py').read_text('utf-8'))
    source=next(ast.literal_eval(n.value) for n in module.body if isinstance(n,ast.Assign)
                and any(isinstance(t,ast.Name) and t.id=='RECEIVER' for t in n.targets))
    root=tmp_path/'originals';lockpath=tmp_path/'release.lock'
    source=source.replace('/home/data/pdfs/自动入库',root.as_posix()).replace('/run/lock/marx-search-release.lock',lockpath.as_posix())
    data=b'original PDF bytes'*12000;digest=hashlib.sha256(data).hexdigest()
    state={'held':False,'reads':0,'transactions':0}
    def flock(handle, operation):
        if Path(handle.name)==lockpath:
            state['held']=operation!=8
            if state['held']:state['transactions']+=1
    monkeypatch.setitem(sys.modules,'fcntl',types.SimpleNamespace(flock=flock,LOCK_SH=1,LOCK_EX=2,LOCK_NB=4,LOCK_UN=8))
    class Network(io.BytesIO):
        def read(self,*args):
            assert not state['held'], 'network read held the watchdog lock'
            state['reads']+=1
            return super().read(*args)
    monkeypatch.setattr(sys,'stdin',types.SimpleNamespace(buffer=Network(data)))
    monkeypatch.setattr(sys,'stdout',io.StringIO())
    monkeypatch.setattr(sys,'argv',['receiver','live-release',digest,str(len(data))])
    monkeypatch.setattr(urllib.request,'urlopen',lambda *a,**k:io.BytesIO(json.dumps({'ok':True,'app_release':{'id':'live-release'}}).encode()))
    # The capacity decision itself is covered elsewhere; keep this tiny fixture
    # independent of the CI worker's available disk space.
    import shutil
    monkeypatch.setattr(shutil,'disk_usage',lambda p:types.SimpleNamespace(free=30<<30))
    namespace={}
    try:
        exec(compile(source,'<original-receiver>','exec'),namespace)
        assert (root/(digest+'.pdf')).read_bytes()==data
        assert state['reads']>2 and state['transactions']>=state['reads']+2
        assert not state['held']
    finally:
        namespace.get('lock',io.BytesIO()).close()
