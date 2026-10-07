import io
import json
import shutil
import tarfile

import pytest

from scripts.dictionary_graph_deploy import install,package,verify,verify_review
from scripts.build_dictionary_graph import write_graph,make_edge


@pytest.fixture
def release(tmp_path):
    app=tmp_path/'app';(app/'config').mkdir(parents=True);(app/'data').mkdir()
    source=app/'data/dictionary.sqlite';source.write_text('readonly source')
    entries=[dict(slug=str(i),title='词条'+str(i),content='原文依据',start_page=i,end_page=i,citation='测试来源',needs_review=0) for i in range(101)]
    edges={}
    for i in range(100):
        e=make_edge(str(i),str(i+1),'mention','evidence','提及',[]);edges[e['id']]=e
    artifact=tmp_path/'artifact'
    write_graph(source,artifact,entries,edges,{}, {},'release-v1')
    selected=json.loads((artifact/'binding.json').read_text('utf-8'))
    shutil.copyfile(artifact/'binding.json',app/'config/dictionary_graph_release.json')
    review=dict(graph_sha256=selected['sha256'],source_sha256=selected['source_sha256'],reviewer='isolated-test',reviewed_at='2026-10-07',
                rows=[dict(id=e['id'],quote_valid=True,relation_valid=True) for e in edges.values()])
    (artifact/'review.json').write_text(json.dumps(review),'utf-8')
    return app,artifact,selected,review


def test_install_retains_old_artifacts_and_legacy_rollback(release,tmp_path):
    app,artifact,selected,_=release
    archive=tmp_path/'graph.tar.gz';package(artifact,archive)
    root=tmp_path/'server';root.mkdir()
    install(root,app,archive)
    assert verify(root,app).meta['id']==selected['id']
    install(root,app,archive)
    legacy=tmp_path/'legacy';legacy.mkdir()
    assert verify(root,legacy) is None
    assert (root/'data/dictionary-graphs/release-v1/graph.sqlite').exists()


def test_stale_review_and_small_sample_fail(release):
    _,artifact,selected,review=release
    with pytest.raises(ValueError):verify_review(dict(review,graph_sha256='0'*64),selected)
    with pytest.raises(ValueError):verify_review(dict(review,rows=review['rows'][:99]),selected)
    bad=[dict(r,relation_valid=False) if i<6 else r for i,r in enumerate(review['rows'])]
    with pytest.raises(ValueError):verify_review(dict(review,rows=bad),selected)
    bad=[dict(r,id='not-in-graph') if i==0 else r for i,r in enumerate(review['rows'])]
    with pytest.raises(ValueError):verify_review(dict(review,rows=bad),selected,artifact/'graph.sqlite')


def test_archive_rejects_traversal_and_links_before_install(release,tmp_path):
    app,_,_,_=release
    archive=tmp_path/'bad.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        data=b'unsafe';m=tarfile.TarInfo('../outside');m.size=len(data);tar.addfile(m,io.BytesIO(data))
    with pytest.raises(ValueError):install(tmp_path/'server',app,archive)
    assert not (tmp_path/'outside').exists()


def test_application_binding_and_source_must_agree(release,tmp_path):
    app,artifact,_,_=release
    archive=tmp_path/'graph.tar.gz';package(artifact,archive)
    (app/'data/dictionary.sqlite').write_text('changed source')
    with pytest.raises(ValueError):install(tmp_path/'server',app,archive)
    assert not (tmp_path/'server/data/dictionary-graphs/release-v1').exists()


def test_graph_change_cannot_skip_paired_release_observation(tmp_path):
    from scripts.catalog_review import validate_review
    releases=tmp_path/'releases'
    app=releases/'new/app';app.mkdir(parents=True)
    old=releases/'old';old.mkdir()
    parent={'release_id':'old','catalog_release':{'id':'same'}}
    metadata=dict(parent,release_id='new',parent_release_id='old',dictionary_graph_release={'id':'new-map'})
    (old/'release.json').write_text(json.dumps(parent),'utf-8')
    (app.parent/'release.json').write_text(json.dumps(metadata),'utf-8')
    with pytest.raises(ValueError,match='review identity'):
        validate_review(app,{})
