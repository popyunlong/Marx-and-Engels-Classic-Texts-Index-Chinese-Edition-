"""A candidate import must never compete for the primary's business queue."""
import os
import subprocess
import sys
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]


def test_candidate_and_rollback_override_maintenance_after_environment_file():
    for name in ('promote_release.sh','rollback_release.sh'):
        script=(ROOT/'deploy'/name).read_text('utf-8')
        command=script[script.index('systemd-run --unit="${CANDIDATE_UNIT%.service}"'):]
        command=command[:command.index('>/dev/null')]
        assert '/usr/bin/env MARX_SKIP_STARTUP_MAINTENANCE=1 MARX_SKIP_SEARCH_WARM=1' in command
        assert '--with-worker' not in command


def test_smoke_disables_maintenance_before_application_import(tmp_path):
    (tmp_path/'app.py').write_text('''import os
assert os.environ['MARX_SKIP_STARTUP_MAINTENANCE']=='1'
assert os.environ['MARX_SKIP_SEARCH_WARM']=='1'
assert os.environ['CITATION_ASSISTANT_INLINE_WORKER']=='0'
''','utf-8')
    code='from pathlib import Path; from scripts.deployment_smoke import check_app_import_and_routes; check_app_import_and_routes(Path(__import__("sys").argv[1]),"server",True)'
    env=dict(os.environ,MARX_SKIP_STARTUP_MAINTENANCE='0',MARX_SKIP_SEARCH_WARM='0',CITATION_ASSISTANT_INLINE_WORKER='1')
    env.pop('APP_RELEASE_FILE',None)
    result=subprocess.run([sys.executable,'-B','-c',code,str(tmp_path)],cwd=ROOT,env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
