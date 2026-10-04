import json
from unittest.mock import Mock

import pytest
import research_updates as r
import research_delivery as m


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(r, 'DB_PATH', tmp_path / 'journal.sqlite3')
    clock = ['2026-10-04T12:00:00+00:00']
    monkeypatch.setattr(r, 'now_text', lambda: clock[0])
    r.init_db()
    issue = r.issue_for('2026-10-03T22:00:00+08:00')
    a = r.upsert(issue['id'], dict(title='学术研究', journal='期刊', origin='domestic', authors=['甲'],
                                url='https://example.org/one', year='2026', published_at='2026-09-29'))
    r.review(issue['id'], [a['entry_id']], 'approved', 'admin')
    r.publish(issue['id'], r.preview_hash(issue['id']), 'admin')
    return issue['id'], clock


def send(sender, recipients=None, **kw):
    return m.deliver('https://example.org', sender=sender, smtp=object(),
                     recipients=recipients if recipients is not None else [{'email':'a@example.org'}], **kw)


def test_web_public_before_mail_and_exact_window(setup):
    iid, clock = setup
    assert r.get_issue(iid)['status'] == 'published'
    assert m.get(iid)['scheduled_at'] == '2026-10-05T01:00:00+00:00'
    sender = Mock()
    assert send(sender)['sent'] == 0
    clock[0] = '2026-10-05T00:59:59+00:00'
    assert send(sender)['sent'] == 0
    clock[0] = '2026-10-05T01:00:59+00:00'
    assert send(sender)['sent'] == 1
    send(sender)
    assert sender.call_count == 1


def test_missed_never_catches_up_until_rescheduled(setup):
    iid, clock = setup
    clock[0] = '2026-10-05T01:01:00+00:00'
    sender = Mock()
    assert send(sender)['sent'] == 0
    assert m.get(iid)['status'] == 'missed'
    m.change(iid, 'reschedule', 'admin', '2026-10-05T10:00:00+08:00')
    clock[0] = '2026-10-05T02:00:00+00:00'
    assert send(sender)['sent'] == 1


def test_cancel_and_pause_do_not_recall_web(setup):
    iid, clock = setup
    m.change(iid, 'cancel', 'admin')
    clock[0] = '2026-10-05T01:00:00+00:00'
    sender = Mock()
    send(sender)
    sender.assert_not_called()
    assert r.get_issue(iid)['status'] == 'published'


def test_pause_during_smtp_stops_remaining_then_resume_without_duplicates(setup):
    iid, clock = setup
    clock[0] = '2026-10-05T01:00:00+00:00'
    audience = [{'email':'a@example.org'}, {'email':'b@example.org'}]
    sender = Mock(side_effect=lambda *a: m.change(iid, 'pause', 'admin'))
    assert send(sender, audience)['sent'] == 1
    assert m.get(iid)['status'] == 'paused'
    m.change(iid, 'reschedule', 'admin', '2026-10-05T10:00:00+08:00')
    clock[0] = '2026-10-05T02:00:00+00:00'
    second = Mock()
    assert send(second, audience)['sent'] == 1
    assert second.call_args.args[1] == 'b@example.org'


def test_concurrent_sender_cannot_claim_owned_batch(setup):
    iid, clock = setup
    clock[0] = '2026-10-05T01:00:00+00:00'
    other = Mock()
    sender = Mock(side_effect=lambda *a: send(other))
    assert send(sender)['sent'] == 1
    other.assert_not_called()


def test_stale_worker_requires_manual_recovery(setup):
    iid, clock = setup
    with r.connect(True) as c:
        c.execute("UPDATE research_mail_schedules SET status='sending',worker='dead',heartbeat='2026-10-05T01:00:00+00:00'")
        c.execute("INSERT INTO research_deliveries(issue_id,email,recipient,snapshot,status,updated_at) VALUES(?,?,?,?,?,?)",
                  (iid, 'a@example.org', '{}', '{}', 'sending', clock[0]))
    clock[0] = '2026-10-05T01:16:00+00:00'
    sender = Mock()
    send(sender)
    assert m.get(iid)['status'] == 'paused'
    with r.connect() as c:
        assert c.execute('SELECT status FROM research_deliveries').fetchone()[0] == 'uncertain'
    sender.assert_not_called()


def test_future_validation_and_atomic_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(r, 'DB_PATH', tmp_path/'new.sqlite3')
    monkeypatch.setattr(r, 'now_text', lambda:'2026-10-05T02:00:00+00:00')
    r.init_db()
    i = r.issue_for('2026-10-03T22:00:00+08:00')
    a = r.upsert(i['id'], {'title':'论文', 'origin':'domestic','journal':'研究','url':'https://example.org/x','published_at':'2026-09-30'})
    r.review(i['id'], [a['entry_id']], 'approved','admin')
    with pytest.raises(ValueError, match='过去'):
        r.publish(i['id'],r.preview_hash(i['id']),'admin')
    assert r.get_issue(i['id'])['status']=='draft'


def test_failure_retry_explicit_schedule_and_no_snapshot_replacement_after_start(setup):
    iid, clock = setup
    clock[0] = '2026-10-05T01:00:00+00:00'
    assert send(Mock(side_effect=ValueError('rejected')))['failed'] == 1
    with r.connect() as c:
        did=c.execute('SELECT id FROM research_deliveries').fetchone()[0]
    m.resolve(iid,did,'admin','queued')
    assert m.get(iid)['status']=='paused'
    with pytest.raises(ValueError, match='不能替换'):
        m.change(iid,'reschedule','admin','2026-10-05T10:00:00+08:00',replace_snapshot=True)
    sender=Mock()
    send(sender)
    sender.assert_not_called()


def test_unmanaged_legacy_queued_rows_cannot_send(setup):
    iid, clock=setup
    m.change(iid,'cancel','admin')
    with r.connect(True) as c:
        c.execute("INSERT INTO research_deliveries(issue_id,email,recipient,snapshot,updated_at) VALUES(?,?,?,?,?)",(iid,'a@example.org','{}','{}',clock[0]))
    clock[0]='2026-10-05T01:00:00+00:00'
    sender=Mock()
    send(sender)
    sender.assert_not_called()
