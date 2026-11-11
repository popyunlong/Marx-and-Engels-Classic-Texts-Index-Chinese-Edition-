"""Real homepage/API templates and routes, using only a temporary synthetic corpus."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import conftest  # Creates isolated runtime data under the configured TEMP.
from chapter_fixture import make_corpus
import app as web
from flask import g
from werkzeug.serving import make_server

corpus, _toc = make_corpus()
web.corpus = corpus
web._public_book_keys = lambda **kw: set(corpus.books)
web._book_is_public = lambda book: book in corpus.books
web._chapter_scope_version = lambda: 'v1'
web._require_search = lambda: None
web._require_feature = lambda *a, **k: None
web._feature_effective_for_user = lambda *a, **k: True
web._content_access_enabled = lambda *a, **k: True
web._attach_viewer_payload = lambda hit, *a, **k: hit
web._record_community_trend = lambda *a, **k: None
web._rate_limit_or_abort = lambda *a, **k: None
original_state = web.current_view_state
def state():
    value = original_state()
    value.update(search_enabled=True, pdf_enabled=True)
    value['feature_access'].update(search=True, associative=True)
    return value
web.current_view_state = state

if __name__ == '__main__':
    server = make_server('127.0.0.1', 0, web.app, threaded=True)
    print(f'READY=http://127.0.0.1:{server.server_port}', flush=True)
    server.serve_forever()
