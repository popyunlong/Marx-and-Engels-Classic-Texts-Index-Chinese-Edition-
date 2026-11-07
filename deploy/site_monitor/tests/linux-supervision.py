"""Isolated OS primitives only: no browser, website, Docker, or production writes."""
import base64,json,os,pathlib,signal,subprocess,sys,tempfile,time

script=base64.b64decode(sys.argv[1])
result={'scenario':'linux-flock-timeout-and-shell-syntax','production_requests':0,'checks':[]}
with tempfile.TemporaryDirectory(prefix='mazhu-monitor-offline-') as tmp:
    root=pathlib.Path(tmp)
    try:
        syntax=subprocess.run(['bash','-n'],input=script,capture_output=True)
        assert syntax.returncode==0,syntax.stderr.decode()
        result['checks'].append('shell syntax passed')
        lock=root/'lock'; ready=root/'ready'
        holder=subprocess.Popen(['flock','-n',str(lock),'python3','-c',
            'import pathlib,time;pathlib.Path(__import__("sys").argv[1]).touch();time.sleep(3)',str(ready)])
        try:
            deadline=time.monotonic()+2
            while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
            assert ready.exists(),'holder did not start'
            loser=subprocess.run(['flock','-n','-E','75',str(lock),'true'])
            assert loser.returncode==75,loser.returncode
            holder.wait(timeout=5)
            successor=subprocess.run(['flock','-n',str(lock),'true'])
            assert successor.returncode==0
            result['checks'].append('shared lock excludes overlapping process and releases normally')
        finally:
            if holder.poll() is None:holder.terminate();holder.wait()
        child_code='import subprocess,time; p=subprocess.Popen(["sleep","20"]); print(p.pid,flush=True); time.sleep(20)'
        killed=subprocess.run(['timeout','--signal=TERM','--kill-after=1s','0.3s','python3','-c',child_code],capture_output=True,timeout=4)
        assert killed.returncode==124,killed.returncode
        pid=int(killed.stdout.strip())
        state=pathlib.Path(f'/proc/{pid}/stat')
        assert not state.exists() or state.read_text().split()[2]=='Z','descendant still running'
        result['checks'].append('hard timeout terminates worker and descendant process group')
        result['status']='passed'
    except Exception as exc:
        result.update(status='failed',error=repr(exc))
print(json.dumps(result,indent=2))
sys.exit(0 if result['status']=='passed' else 1)
