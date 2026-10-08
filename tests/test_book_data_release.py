import json
import sqlite3
from pathlib import Path
import pytest
import yaml
from book_data_release import Bundle, canonical, file_hash
from ingestion.book_data import build, verify_append


@pytest.fixture
def appended(tmp_path):
    app=tmp_path/'parent';data=app/'data';config=app/'config';pdfs=app/'pdfs'
    for p in (data,config,pdfs/'自动入库'):p.mkdir(parents=True)
    with sqlite3.connect(data/'corpus.sqlite') as c:
        c.executescript('''CREATE TABLE pages(id INTEGER PRIMARY KEY,book TEXT,volume INTEGER,
            source_file TEXT,pdf_page INTEGER,printed_page TEXT,raw_text TEXT,normalized_text TEXT);
            CREATE TABLE toc_entries(book TEXT,volume INTEGER,source_file TEXT,title TEXT,
            pdf_page INTEGER,printed_page TEXT,level INTEGER,kind TEXT,sort_order INTEGER);
            INSERT INTO pages VALUES(71,'旧书',1,'pdfs/old.pdf',1,'629','不能改变的原文','不能改变的原文');
            INSERT INTO toc_entries VALUES('旧书',1,'pdfs/old.pdf','旧篇章',1,'629',1,'body',0);''')
    for name,value in [('books.yaml',{'books':[dict(key='旧书',title='旧书',sort_order=1)]}),
                       ('manifest.yaml',{'旧书':[dict(file='pdfs/old.pdf',volume=1)]}),
                       ('volumes.yaml',{'旧书':{1:2009}})]:
        (config/name).write_text(yaml.safe_dump(value,allow_unicode=True),encoding='utf-8')
    sha=__import__('hashlib').sha256(b'fixture pdf').hexdigest()
    (pdfs/'自动入库'/(sha+'.pdf')).write_bytes(b'fixture pdf')
    p=dict(book_id=sha[:32],source_sha256=sha,source_file='pdfs/自动入库/'+sha+'.pdf',page_count=1,
           pages=[dict(page=1,label='349',text='新的正文以及脚注。',segment_id='body',label_evidence={'method':'image_review'})],
           toc=[dict(title='新的篇章',pdf_page=1,printed_page='349',level=1,kind='body',sort_order=0,
                     authors=['作者'],date='(1978年3月8日)',end_pdf_page=1,evidence_method='body_heading_image_review',provenance_verified=True)],
           metadata=dict(book_key='新书',citation_title='新书',authors=[],editors=['编者'],publisher='出版者',
                         place='北京',year='2009',volume=2,volume_label='（中）',display_title='新书（中）',
                         single_volume=False,collection='party_state_documents',isbn='9787503424861'),
           paddle_source={'schema':1,'json_sha256':'a'*64})
    output=tmp_path/'v1'
    selected=build([p],app,output,baseline=data/'corpus.sqlite',config=config,pdfs=pdfs,parent=None)
    return app,output,p,selected


def test_append_preserves_old_identity_and_binds_all_consumers(appended,monkeypatch):
    app,output,p,selected=appended
    bundle=Bundle(output,selected['sha256'])
    with sqlite3.connect(bundle.path('database')) as c:
        assert c.execute('SELECT id,raw_text,printed_page FROM pages WHERE id=71').fetchone()==(71,'不能改变的原文','629')
        assert c.execute('SELECT count(*) FROM pages').fetchone()[0]==2
        bundle.catalog().verify_corpus(c)
    from book_config import load_book_configs
    cfg={b.key:b for b in load_book_configs(output/'config/books.yaml')}
    assert cfg['新书'].for_volume(2).isbn=='9787503424861'
    import book_data_release as binding
    monkeypatch.setattr(binding,'current',lambda:bundle);binding.article_index.cache_clear()
    assert binding.article_fields(p['source_file'],1,'新的篇章')['authors']==('作者',)
    assert binding.article_fields(p['source_file'],1,'另一个历史标题')=={}
    assert binding.text_only(p['source_file']) and not binding.text_only('pdfs/old.pdf')
    binding.article_index.cache_clear()


@pytest.mark.parametrize('column,value',[('id',73),('raw_text','改坏的文字'),('printed_page','630')])
def test_old_page_changes_are_rejected(appended,column,value):
    app,output,p,_=appended
    db=output/'data/corpus.sqlite';db.chmod(0o644)
    with sqlite3.connect(db) as c:c.execute(f'UPDATE pages SET {column}=? WHERE id=71',(value,))
    with pytest.raises(ValueError,match='old book data changed'):
        verify_append(app/'data/corpus.sqlite',db,[p],app/'config',output/'config')


def test_component_tampering_prevents_startup(appended):
    _,output,_,selected=appended
    path=output/'articles.json';path.chmod(0o644);path.write_bytes(b'[]')
    with pytest.raises(ValueError,match='component changed'):
        Bundle(output,selected['sha256'])


def test_book_health_checks_exact_data_binding():
    from scripts.catalog_deploy import check_health
    with pytest.raises(ValueError,match='book data generation'):
        check_health({'book_data_release':{'id':'v2','sha256':'b'*64}},
                     {'book_data_release':{'id':'v1','sha256':'a'*64}})
