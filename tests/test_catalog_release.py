import copy
import json
import sqlite3
from pathlib import Path

import pytest
from flask import Flask

from catalog_release import Catalog, canonical, digest, file_digest, inventory, safe_file
from scripts.catalog_bundle import build, groups, validate_changes


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / 'input'
    root.mkdir()
    rows = [dict(book='B', volume=1, source_file='pdfs/b.pdf', title='Chapter', pdf_page=1,
                 printed_page='i', level=1, kind='body', sort_order=1),
            dict(book='B', volume=1, source_file='pdfs/b.pdf', title='Section', pdf_page=1,
                 printed_page='i', level=2, kind='body', sort_order=2)]
    (root / 'toc_entries.json').write_bytes(canonical(rows))
    (root / 'sources.json').write_bytes(canonical([dict(book='B', volume=1, source_file='pdfs/b.pdf',
                                                       page_count=1, last_page=1)]))
    for name in ('static_library', 'stream_library'):
        (root / name / 'b').mkdir(parents=True)
        (root / name / 'b/index.htm').write_text('<a href="next.html#old">next</a>', encoding='utf-8')
        (root / name / 'b/next.html').write_text('<h2 id="old">Title</h2>', encoding='utf-8')
    return root


def test_baseline_equivalent_and_complete(snapshot, tmp_path):
    expected = build(snapshot, tmp_path / 'v1', 'v1')
    catalog = Catalog(tmp_path / 'v1', expected['sha256'])
    assert catalog.rows == json.loads((snapshot / 'toc_entries.json').read_text())
    assert catalog.manifest['parent'] is None
    assert len(catalog.manifest['files']) == 6


def test_same_page_and_deep_numbering_allowed(snapshot, tmp_path):
    rows = json.loads((snapshot / 'toc_entries.json').read_text())
    rows[1]['level'] = 8
    rows.append(dict(rows[1], title='1. restarted numbering', sort_order=3))
    (snapshot / 'toc_entries.json').write_bytes(canonical(rows))
    build(snapshot, tmp_path / 'v1', 'v1')
    assert Catalog(tmp_path / 'v1').rows[-1]['level'] == 8


@pytest.mark.parametrize('mutation', ['remove', 'parent', 'flatten', 'page', 'title'])
def test_unapproved_toc_changes_rejected(snapshot, mutation):
    before = json.loads((snapshot / 'toc_entries.json').read_text())
    after = copy.deepcopy(before)
    if mutation == 'remove':
        after.pop()
    else:
        key, value = {'parent': ('level', 3), 'flatten': ('level', 1),
                      'page': ('pdf_page', 2), 'title': ('title', 'bad')}[mutation]
        after[1][key] = value
    with pytest.raises(ValueError, match='unapproved'):
        validate_changes(before, after, {}, {}, {})


def test_exact_approval_and_stale_baseline(snapshot):
    before = json.loads((snapshot / 'toc_entries.json').read_text())
    after = copy.deepcopy(before)
    after[1]['level'] = 8
    approvals = {'toc': {'pdfs/b.pdf': {'before': groups(before)['pdfs/b.pdf'],
                                       'after': groups(after)['pdfs/b.pdf'], 'evidence': 'PDF p1'}}}
    validate_changes(before, after, {}, {}, approvals)
    before[0]['title'] = 'new live edit'
    with pytest.raises(ValueError, match='stale fingerprints'):
        validate_changes(before, after, {}, {}, approvals)


def test_tampered_and_extra_files_rejected(snapshot, tmp_path):
    build(snapshot, tmp_path / 'v1', 'v1')
    (tmp_path / 'v1/static_library/b/next.html').write_text('changed')
    with pytest.raises(ValueError, match='modified'):
        Catalog(tmp_path / 'v1')


@pytest.mark.parametrize('path', ['../secret', '/secret', 'C:/secret', 'a\\secret'])
def test_traversal_rejected(tmp_path, path):
    with pytest.raises(ValueError):
        safe_file(tmp_path, path)


def test_corpus_changes_rejected(snapshot, tmp_path):
    build(snapshot, tmp_path / 'v1', 'v1')
    catalog = Catalog(tmp_path / 'v1')
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE toc_entries(book,volume,source_file,title,pdf_page,printed_page,level,kind,sort_order)')
    for row in catalog.rows:
        from catalog_release import FIELDS
        db.execute('INSERT INTO toc_entries VALUES(?,?,?,?,?,?,?,?,?)', [row[k] for k in FIELDS])
    db.execute('CREATE TABLE pages(book,volume,source_file,pdf_page)')
    db.execute("INSERT INTO pages VALUES('B',1,'pdfs/b.pdf',1)")
    catalog.verify_corpus(db)
    db.execute("INSERT INTO pages VALUES('NEW',1,'new.pdf',1)")
    with pytest.raises(ValueError, match='sources changed'):
        catalog.verify_corpus(db)


def test_history_routes_keep_auth_and_relative_links(snapshot, tmp_path, monkeypatch):
    import static_library_web as web
    build(snapshot, tmp_path / 'v1', 'v1')
    old = Catalog(tmp_path / 'v1')
    (snapshot / 'static_library/b/next.html').write_text('new version')
    build(snapshot, tmp_path / 'v2', 'v2')
    new = Catalog(tmp_path / 'v2')
    monkeypatch.setattr(web, '_CATALOG', new)
    monkeypatch.setattr(web, 'historic_catalog', lambda version: {'v1': old, 'v2': new}[version])
    cfg = tmp_path / 'books.yaml'
    cfg.write_text('books:\n- key: b\n  folder: b\n  volumes:\n  - n: 1\n    index: index.htm\n')
    source = web._BookSource(cfg, new.root / 'static_library')
    app = Flask(__name__)
    allowed = [True]
    def auth(feature):
        from flask import abort
        if not allowed[0]:
            abort(403)
    web.register_static_library(app, source=source, require_content_feature=auth)
    client = app.test_client()
    assert client.get('/wenku/raw/b/index.htm').location.endswith('/wenku/v/v2/raw/b/index.htm')
    assert b'Title' in client.get('/wenku/v/v1/raw/b/next.html').data
    assert b'new version' in client.get('/wenku/v/v2/raw/b/next.html').data
    assert client.get('/wenku/raw/b/next.html', headers={'Referer': 'http://localhost/wenku/v/v1/raw/b/index.htm'}).location.endswith('/wenku/v/v1/raw/b/next.html')
    allowed[0] = False
    assert client.get('/wenku/v/v1/raw/b/next.html').status_code == 403
    assert client.get('/wenku/raw/b/index.htm').status_code == 403


def test_bound_empty_catalog_never_extracts_pdf(monkeypatch):
    from search import Corpus
    corpus = object.__new__(Corpus)
    corpus.catalog = object()
    corpus._toc_db_entries = {}
    assert corpus.get_toc_entries('pdfs/empty.pdf') == []


def test_duplicate_named_anchors_and_missing_target_scan(snapshot, tmp_path):
    from scripts.scan_catalog_links import scan
    (snapshot / 'static_library/b/next.html').write_text(
        '<a id="old" name="old"></a><h2 id="old">Duplicate</h2><a href="missing.htm#x">bad</a>')
    build(snapshot, tmp_path / 'v1', 'v1')
    result = scan(tmp_path / 'v1')
    assert result['counts'] == {'duplicate_anchor': 1, 'missing_file': 1}
    duplicate = next(i for i in result['issues'] if i['kind'] == 'duplicate_anchor')
    assert duplicate['count'] == 2  # id+name on one element is one anchor.


def test_release_health_rejects_wrong_catalog_and_unhealthy_corpus():
    from scripts.catalog_deploy import check_health
    metadata = {'release_id': 'app1', 'catalog_release': {'id': 'v1', 'sha256': 'a' * 64}}
    payload = {'ok': True, 'app_release': {'id': 'app1'}, 'catalog_release': metadata['catalog_release']}
    check_health(payload, metadata)
    for bad in (dict(payload, ok=False), dict(payload, catalog_release={'id': 'v0'}),
                dict(payload, app_release={'id': 'old'})):
        with pytest.raises(ValueError, match='identity mismatch'):
            check_health(bad, metadata)


def test_archive_installed_atomically_and_tamper_rejected(snapshot, tmp_path):
    import tarfile
    from scripts.catalog_deploy import install_archive
    selected = build(snapshot, tmp_path / 'v1', 'v1')
    archive = tmp_path / 'bundle.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for p in (tmp_path / 'v1').rglob('*'):
            if p.is_file():
                tar.add(p, arcname=p.relative_to(tmp_path / 'v1').as_posix())
    dest = tmp_path / 'installed/v1'
    install_archive(archive, dest, selected)
    install_archive(archive, dest, selected)  # Idempotent, no replacement.
    (dest / 'toc.json').write_text('[]')
    with pytest.raises(ValueError, match='modified'):
        install_archive(archive, dest, selected)


@pytest.mark.parametrize('kind', ['traversal', 'symlink', 'duplicate'])
def test_unsafe_catalog_archive_cannot_install(tmp_path, kind):
    import io
    import tarfile
    from scripts.catalog_deploy import install_archive
    archive = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        info = tarfile.TarInfo('../escape' if kind == 'traversal' else 'one')
        if kind == 'symlink':
            info.type = tarfile.SYMTYPE
            info.linkname = '../escape'
        tar.addfile(info, io.BytesIO(b''))
        if kind == 'duplicate':
            tar.addfile(info, io.BytesIO(b''))
    target = tmp_path / 'installed/v1'
    with pytest.raises(ValueError):
        install_archive(archive, target, {'id': 'v1', 'sha256': '0' * 64})
    assert not target.exists()
    assert not (tmp_path / 'escape').exists()


def test_missing_bound_release_does_not_enter_legacy_mode(tmp_path, monkeypatch):
    import catalog_release as module
    monkeypatch.setenv('APP_RELEASE_FILE', str(tmp_path / 'missing-release.json'))
    with pytest.raises(FileNotFoundError):
        module.binding()


def test_historical_catalog_needs_acceptance_receipt(snapshot, tmp_path, monkeypatch):
    import catalog_release as module
    from scripts.catalog_deploy import accept_catalog
    selected = build(snapshot, tmp_path / 'data/catalog-releases/v1', 'v1')
    monkeypatch.setattr(module, 'catalog_root', lambda: tmp_path / 'data/catalog-releases')
    monkeypatch.setattr(module, 'active_catalog', lambda: None)
    module.historic_catalog.cache_clear()
    with pytest.raises(FileNotFoundError):
        module.historic_catalog('v1')
    app = tmp_path / 'app'
    (app / 'config').mkdir(parents=True)
    (app / 'config/catalog_release.json').write_bytes(canonical(selected))
    accept_catalog(tmp_path, app)
    assert module.historic_catalog('v1').version == 'v1'
    module.historic_catalog.cache_clear()


def test_rollback_refuses_different_catalog_binding(tmp_path):
    from scripts.catalog_deploy import rollback_guard
    for name, version in [('current/app', 'v2'), ('target/app', 'v1')]:
        config = tmp_path / name / 'config'
        config.mkdir(parents=True)
        (config / 'catalog_release.json').write_bytes(canonical({'id': version, 'sha256': 'a' * 64}))
    with pytest.raises(ValueError, match='inverse patch'):
        rollback_guard(tmp_path, tmp_path / 'target/app')


def test_first_batch_preserves_nested_catalog_metadata(tmp_path):
    from scripts.prepare_catalog_repairs import FIXES, prepare
    snap = tmp_path / 'snap'
    snap.mkdir()
    rows = [dict(book='文集', volume=v, source_file=f'pdfs/v{v}.pdf', title=title,
                 pdf_page=page, printed_page=label, level=2, kind='body', sort_order=order)
            for v, order, title, page, label, _, _ in FIXES]
    (snap / 'toc_entries.json').write_bytes(canonical(rows))
    (snap / 'sources.json').write_bytes(canonical([
        dict(book='文集', volume=v, source_file=f'pdfs/v{v}.pdf', page_count=1100, last_page=1100)
        for v in [4, 5, 10]]))
    for name in ['static_library/lenin-ru/1', 'static_library/mew-de/42',
                 'static_library/mega-full', 'stream_library', 'config']:
        (snap / name).mkdir(parents=True)
    (snap / 'static_library/mega-full/catalog.json').write_text('{"volumes":["must survive"]}')
    (snap / 'static_library/lenin-ru/1/index.html').write_text('<a href="../index.html">Back</a>')
    old = 'karl-marx-grundrisse-der-kritik-der-politischen-oekonomie/ergaenzungen-zu-den-kapiteln-von-geld-und-vom-kapital.html'
    (snap / 'static_library/mew-de/42/index.htm').write_text('<a href="' + old + '">E</a><a href="me42_670.htm">rest</a>')
    (snap / 'static_library/mew-de/42/me42_670.htm').write_text('original target')
    (snap / 'config/static_books.yaml').write_text('books:\n- key: lenin-ru\n  folder: lenin-ru\n  volumes:\n  - index: 1/index.html\n    label: Vol 1\n')
    build(snap, tmp_path / 'baseline', 'baseline')
    prepare(tmp_path / 'baseline', tmp_path / 'work', tmp_path / 'fixed', 'fixed')
    before, after = Catalog(tmp_path / 'baseline'), Catalog(tmp_path / 'fixed')
    nested = 'static_library/mega-full/catalog.json'
    assert after.manifest['files'][nested] == before.manifest['files'][nested]
    assert set(after.manifest['changes']['files']) == {'static_library/lenin-ru/index.html', 'static_library/mew-de/42/index.htm'}
    assert len(after.rows) == len(before.rows)


def test_bound_catalog_blocks_legacy_database_writers(monkeypatch):
    import catalog_release as module
    from scripts.book_changeset import cmd_inject
    monkeypatch.setattr(module, 'binding', lambda: {'id': 'bound', 'sha256': 'a'*64})
    with pytest.raises(RuntimeError, match='旧整库注入'):
        cmd_inject(None)  # Must reject before opening any input, backup or database.


def test_first_binding_requires_compatible_predecessor(tmp_path):
    from scripts.catalog_deploy import preflight
    current = tmp_path / 'current'
    (current / 'app').mkdir(parents=True)
    (current / 'release.json').write_text('{"release_id":"legacy"}')
    candidate = tmp_path / 'candidate'
    (candidate / 'config').mkdir(parents=True)
    (candidate / 'config/catalog_release.json').write_bytes(canonical({'id': 'v1', 'sha256': 'a'*64}))
    with pytest.raises(ValueError, match='compatibility foundation'):
        preflight(tmp_path, candidate)
    assert not (tmp_path / 'data').exists()
