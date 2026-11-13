"""Hermetic users and reviewed bulletin fixture shared by new acceptance scenarios."""
import secrets
from datetime import timedelta
from _test_env import APPDATA
import app as A
import membership as M
import research_updates as R
import research_preferences as P


def user(plan=None):
    u = M.create_user(email=secrets.token_hex(8)+'@refresh.test', display_name='研究会员', password_hash='unused')
    if plan in {'support_basic', 'support_plus', 'monthly', 'quarterly', 'yearly'}:
        now = M.utc_now().replace(microsecond=0); end = now + timedelta(days=30)
        with M._connect() as c:
            cur = c.execute("INSERT INTO subscriptions(user_id,plan_code,status,source,starts_at,expires_at,created_at,updated_at) VALUES(?,?,'active','fixture',?,?,?,?)",
                (u['id'],plan,now.isoformat(),end.isoformat(),now.isoformat(),now.isoformat()))
            if plan.startswith('support_'):
                product = {**M.get_plan(plan), 'plan_code': plan}
                M._insert_ai_wallet(c,user_id=u['id'],source_type='subscription',source_ref=str(cur.lastrowid),plan=product,starts_at=now,expires_at=end,paid_at=now.isoformat())
            c.commit()
    elif plan:
        order = M.create_pending_order(user_id=u['id'], plan_code=plan)
        M.mark_order_paid(order_no=order['order_no'], provider='fixture')
    return u


def login(client, u):
    with client.session_transaction() as session:
        session['user_id'] = u['id']; session['_csrf_token'] = 'refresh-fixture-csrf'
    return {'X-CSRF-Token':'refresh-fixture-csrf'}


def bulletin():
    articles=[]
    catalog=P.catalog()
    for origin, group in [('domestic','zh'),('foreign','en')]:
        journals=[j for j in catalog if j['group']==group]
        for n in range(8):
            a=R.normalize(dict(title=f'研究条目 {origin} {n}',title_zh=f'中文研究题名 {n}' if group=='en' else '',
                journal=journals[n%3]['name'],origin=origin,authors=['研究作者'],abstract='已审核摘要。',year='2026',
                volume='1',issue='2',pages='12-20',url='https://example.org/research/'+origin+str(n)))
            a.update(entry_id=len(articles)+1,section='new',ordinal=n,
                verified_fields=['title','authors','journal','year','volume','issue','pages','url'])
            articles.append(a)
    snapshot=dict(id=70001,title=R.TITLE,period_start='2026-11-01T00:00:00+08:00',period_end='2026-11-08T00:00:00+08:00',articles=articles)
    with R.connect(True) as c:
        c.execute("INSERT OR REPLACE INTO research_issues(id,period_start,period_end,status,snapshot,created_at) VALUES(?,?,?,'published',?,?)",
            (snapshot['id'],snapshot['period_start'],snapshot['period_end'],R.dumps(snapshot),R.now_text()))
    return snapshot
