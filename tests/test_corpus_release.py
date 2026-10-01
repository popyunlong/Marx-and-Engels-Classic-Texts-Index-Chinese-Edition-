import json
import sqlite3
import shutil
from pathlib import Path
import pytest
from paddle_repair import canonical,file_hash
from corpus_release import Bundle,binding
from scripts import corpus_deploy
from scripts.build_paddle_candidate import seal


def quality(root=None):
    report=dict(verified_characters=20000,character_error_rate=.001,known_error_reduction=.6,
        no_quality_regression=True,unreviewed_high_risk=0,geometry_samples=150,
        geometry_accuracy=1,wrong_book_or_page=0,volume_samples={})
    if root:
        report.update(candidate_id=root.name,corpus_sha256=file_hash(root/'corpus.sqlite'),
            geometry_sha256=file_hash(root/'geometry.sqlite'),layout_sha256=file_hash(root/'layout/manifest.json'))
    return report


def draft(tmp_path,name='generation-a',parent=None):
    root=tmp_path/name;root.mkdir(parents=True)
    with sqlite3.connect(root/'corpus.sqlite') as c:
        c.executescript('CREATE TABLE pages(id INTEGER PRIMARY KEY,book TEXT,volume INTEGER,source_file TEXT,pdf_page INTEGER,printed_page TEXT,raw_text TEXT,normalized_text TEXT); CREATE TABLE toc_entries(id INTEGER PRIMARY KEY,title TEXT);')
        c.execute('INSERT INTO pages VALUES(1,?,?,?,?,?,?,?)',('文集',1,'pdfs/1.pdf',1,'1','原文','原文'))
    with sqlite3.connect(root/'geometry.sqlite') as c:c.execute('CREATE TABLE geometry_meta(key TEXT,value TEXT)')
    (root/'corpus.sqlite.sha256').write_text(file_hash(root/'corpus.sqlite'),'utf-8')
    (root/'layout').mkdir();(root/'layout/manifest.json').write_bytes(canonical({'version':1,'volumes':{}}))
    (root/'draft.json').write_bytes(canonical({'schema_version':1,'id':name,'parent':parent,
        'baseline_sha256':file_hash(root/'corpus.sqlite'),'catalog_release':{'id':'cat'},
        'changed_sources':[],'changed_pages':[],'pdfs':{}}))
    return root


def test_seal_binds_all_components_and_rejects_modification(tmp_path):
    root=draft(tmp_path);selected=seal(root,quality(root))
    bundle=Bundle(root,selected['sha256'])
    assert bundle.path('database')==root/'corpus.sqlite'
    with pytest.raises(ValueError,match='already sealed'):seal(root,quality())
    (root/'layout/extra.text').write_text('unregistered')
    with pytest.raises(ValueError,match='unregistered'):Bundle(root,selected['sha256'])


def test_unreviewed_generation_never_gets_publishable_manifest(tmp_path):
    root=draft(tmp_path)
    with pytest.raises(ValueError):seal(root,{**quality(),'unreviewed_high_risk':1})
    assert not (root/'manifest.json').exists()


def test_app_binding_cannot_disagree_with_archive_metadata(tmp_path):
    app=tmp_path/'app';(app/'config').mkdir(parents=True)
    (app/'config/corpus_release.json').write_bytes(canonical({'id':'a','sha256':'a'*64}))
    (tmp_path/'release.json').write_bytes(canonical({'corpus_release':{'id':'b','sha256':'b'*64}}))
    with pytest.raises(ValueError,match='differs'):binding(app)


def test_preflight_requires_foundation_and_unchanged_first_generation(tmp_path):
    root=tmp_path/'site';current=root/'current/app';candidate=root/'releases/app'
    for app in (current,candidate):
        (app/'config').mkdir(parents=True)
        (app/'config/catalog_release.json').write_bytes(canonical({'id':'cat'}))
    gen=draft(root/'data/corpus-releases');selected=seal(gen,quality(gen))
    (candidate/'config/corpus_release.json').write_bytes(canonical(selected))
    shutil.copy2(gen/'corpus.sqlite',root/'data/corpus.sqlite')
    metadata=root/'current/release.json';metadata.write_bytes(canonical({'release_id':'old'}))
    with pytest.raises(ValueError,match='foundation'):corpus_deploy.preflight(root,candidate)
    metadata.write_bytes(canonical({'release_id':'old','corpus_protocol':1}))
    corpus_deploy.preflight(root,candidate)
    (root/'data/corpus.sqlite').chmod(0o644)
    with sqlite3.connect(root/'data/corpus.sqlite') as c:c.execute("UPDATE pages SET printed_page='changed'")
    with pytest.raises(ValueError,match='identity'):corpus_deploy.preflight(root,candidate)


def test_archive_rejects_links_and_traversal_without_touching_live_data(tmp_path):
    import tarfile,io
    archive=tmp_path/'evil.tar.gz'
    with tarfile.open(archive,'w:gz') as t:
        m=tarfile.TarInfo('../outside');m.size=1;t.addfile(m,io.BytesIO(b'x'))
    with pytest.raises(ValueError,match='unsafe'):
        corpus_deploy.install_archive(archive,tmp_path/'bundles/version',{'id':'version','sha256':'0'*64})
    assert not (tmp_path/'outside').exists()
