"""Actual isolated app/member store; models are mocked by the browser harness."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _test_env import APPDATA
from test_legacy_promise import member, exhaust_wallet, NOW
import app as A
import membership as M
from datetime import datetime
from flask import session, redirect
from werkzeug.serving import make_server

M.utc_now = lambda: datetime.fromisoformat(NOW)
A.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
user,_,_ = member()
exhaust_wallet(user)


@A.app.get('/fixture-legacy-login')
def fixture_login():
    session['user_id'] = user['id']
    return redirect('/account')


if __name__ == '__main__':
    server = make_server('127.0.0.1', 0, A.app, threaded=True)
    print(f'READY=http://127.0.0.1:{server.server_port}', flush=True)
    server.serve_forever()
