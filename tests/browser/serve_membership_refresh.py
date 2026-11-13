import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
# Build the standard miniature corpus; no production data is read.
import conftest
from membership_refresh_fixture import A,M,P,user,bulletin
import notes
from flask import session,redirect
from werkzeug.serving import make_server

import urllib.request, smtplib
def no_external(*args, **kwargs):
    raise RuntimeError("External services disabled in browser fixture")
urllib.request.urlopen = no_external
smtplib.SMTP = no_external
A.app.config.update(TESTING=True,SESSION_COOKIE_SECURE=False)
u=user('support_plus_20')
max_user=user('support_max')
bulletin();P.prepare_sample()
source=A.corpus.get_volumes('文集')[0].source_file
notes.create_note(user_id=u['id'],reader='viewer',book_key=source,book_title='研究笔记测试书',page=1,
    page_label='第 1 页',quote='原文摘录',body='用于核验导出和定位的笔记。',source_url='/viewer?'+__import__('urllib.parse',fromlist=['urlencode']).urlencode({'file':source,'page':1}))

@A.app.get('/fixture-login/<tier>')
def fixture_login(tier):
    session['user_id']=(max_user if tier=='max' else u)['id']
    return redirect('/account')

if __name__=='__main__':
    server=make_server('127.0.0.1',0,A.app,threaded=True)
    print(f'READY=http://127.0.0.1:{server.server_port}',flush=True)
    server.serve_forever()
