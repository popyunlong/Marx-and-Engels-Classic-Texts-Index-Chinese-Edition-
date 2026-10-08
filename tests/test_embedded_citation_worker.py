import threading


def test_embedded_worker_reaches_queue_without_registering_process_signals(monkeypatch):
    from scripts import citation_assistant_worker as worker
    monkeypatch.setattr(worker, '_corpus_file_fingerprint', lambda: None)
    monkeypatch.setattr(worker, '_reload_if_corpus_changed', lambda _: None)
    monkeypatch.setattr(worker, '_citation_corpus_sha256', lambda: 'qa-corpus')
    monkeypatch.setattr(worker, '_citation_template_version', lambda: 'qa-template')
    monkeypatch.setattr(worker, '_search_export_template_version', lambda: 'qa-export')
    monkeypatch.setattr(worker.tasks, 'recover_jobs_for_loaded_corpus', lambda *a: {'analysis':0,'export':0})
    monkeypatch.setattr(worker.tasks, 'recover_pdf_position_failures', lambda *a: 0)
    monkeypatch.setattr(worker.search_export_tasks, 'record_worker_heartbeat', lambda *a: None)
    monkeypatch.setattr(worker.search_export_tasks, 'cleanup_expired', lambda: None)
    monkeypatch.setattr(worker.search_export_tasks, 'resources_allow_start', lambda: False)
    claims=[]
    monkeypatch.setattr(worker.tasks, 'claim_next_job', lambda *a,**k: claims.append(k))
    results=[]
    def run():
        try:
            results.append(worker.run(once=True))
        except Exception as exc:
            results.append(exc)
    thread=threading.Thread(target=run)
    thread.start();thread.join(timeout=5)
    assert not thread.is_alive()
    assert results==[0]
    assert claims[0]['corpus_sha256']=='qa-corpus'
