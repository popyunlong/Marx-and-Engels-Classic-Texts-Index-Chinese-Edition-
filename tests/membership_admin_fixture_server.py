"""Isolated loopback admin UI; no production authentication or provider calls."""
import sys
import threading
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import conftest
from membership_refresh_fixture import A,M,user
from flask import session,redirect,jsonify
from werkzeug.serving import make_server
import urllib.request
import smtplib

def forbidden(*args,**kwargs):raise AssertionError('External calls disabled')
urllib.request.urlopen=forbidden
smtplib.SMTP=forbidden
A.app.config.update(TESTING=True,SESSION_COOKIE_SECURE=False)
admin=user();M.update_user_account(admin['id'],role='admin')
plus=user('support_plus_20')

@A.app.get('/fixture/login/<role>')
def fixture_login(role):
    session.clear()
    session['user_id']=(admin if role=='admin' else plus)['id']
    session['admin_2fa_verified_at']=M.utc_now_text()
    return redirect('/admin/members' if role=='admin' else '/ai')

@A.app.post('/fixture/shutdown')
def shutdown():
    threading.Thread(target=server.shutdown,daemon=True).start()
    return jsonify(ok=True)

A.CSRF_EXEMPT_ENDPOINTS.add('shutdown')
if __name__=='__main__':
    server=make_server('127.0.0.1',0,A.app,threaded=True)
    print(f'READY=http://127.0.0.1:{server.server_port}',flush=True)
    server.serve_forever();server.server_close()
