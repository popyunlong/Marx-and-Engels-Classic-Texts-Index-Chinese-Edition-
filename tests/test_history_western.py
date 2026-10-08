import copy
import pytest
from release_review_policy import append_fast
from scripts.catalog_observe import compare_fast
from scripts.catalog_review import validate_server_samples
from ingestion.history_western import parse_contents
from ingestion.paddle_catalog import register


def test_preface_author_overrides_preserve_the_main_work_and_translation():
    from ingestion.history_western import complete_work_ranges
    meta=dict(title='原著',authors=['原作者'],translators=['译者'],work_ranges=[
        dict(start=2,end=3,title='序言',authors=['序作者'],translators=[]),
        dict(start=9,end=9,title='译后记',authors=['译者'],translators=[])])
    original=copy.deepcopy(meta)
    result=complete_work_ranges(meta,10)
    assert meta==original
    assert [(r['start'],r['end']) for r in result['work_ranges']]==[(1,1),(2,3),(4,8),(9,9),(10,10)]
    assert result['work_ranges'][2]['authors']==['原作者']
    assert result['work_ranges'][2]['translators']==['译者']
    assert result['work_ranges'][1]==meta['work_ranges'][0]
    assert complete_work_ranges(result,10)==result


@pytest.mark.parametrize('ranges', [
    [dict(start=0,end=2)], [dict(start=2,end=11)],
    [dict(start=2,end=5),dict(start=5,end=8)]])
def test_invalid_author_partition_is_rejected(ranges):
    from ingestion.history_western import complete_work_ranges
    with pytest.raises(ValueError):
        complete_work_ranges(dict(title='书',authors=['作者'],work_ranges=ranges),10)


def test_completed_ranges_keep_body_searchable_without_misattributing_preface(monkeypatch):
    from types import SimpleNamespace as S
    from search import Page, Volume
    from ingestion.history_western import complete_work_ranges
    from ingestion import paddle_runtime, paddle_scope
    meta=complete_work_ranges(dict(title='原著',authors=['原作者'],work_ranges=[
        dict(start=1,end=1,title='译序',authors=['译者'],translators=[])]),3)
    pages=[Page(n,str(n),'独立正文'+str(n),'独立正文'+str(n),n) for n in range(1,4)]
    volume=Volume.build('书',1,'source.pdf','原著',pages)
    corpus=S(get_volume_by_source_file=lambda _:volume,
             _scoped_volumes=lambda *a:[volume],_chapter_segments=lambda _:[])
    app=S(corpus=corpus,_resolve_search_scope=lambda *a:({},None,False),
          _chat_grounding_source_text=lambda *a:None,_research_review_passage_text=lambda *a:None)
    monkeypatch.setattr(paddle_runtime,'bibliography',lambda:{'书':{1:meta}})
    paddle_scope.install(app)
    assert [p.pdf_page for v in corpus._scoped_volumes('书') for p in v.pages]==[1,2,3]
    assert [p.pdf_page for v in corpus._scoped_volumes('书',paddle_scope.WorkScope({'书':None},['原作者'])) for p in v.pages]==[2,3]
    assert [p.pdf_page for v in corpus._scoped_volumes('书',paddle_scope.WorkScope({'书':None},['译者'])) for p in v.pages]==[1]


def evidence():
    parent=dict(release_id='old',book_data_release={'id':'old-data'},book_data_catalog={'id':'old-toc'})
    metadata=dict(release_id='new',parent_release_id='old',review_mode='append-fast',
        release_type='new_books',append_only_verified=True,book_data_release={'id':'new-data'},
        book_data_catalog={'id':'new-toc'})
    pressure=dict(cpu_avg10=2.,io_avg10=0.,memory_avg10=0.,available_mib=2000.,pressured=False)
    rows=[dict(elapsed=float(n),live={r:.1 for r in ('/','/api/runtime','/v2/read')},
        candidate={r:.15 for r in ('/','/api/runtime','/v2/read')},resources=pressure) for n in range(3)]
    report=dict(compare_fast(rows),schema_version=3,review_mode='append-fast',sample_source='server_loopback',
        interval_seconds=1,pairs=3,elapsed_seconds=2.,live_release='old',candidate_release='new',
        live_catalog=parent['book_data_catalog'],candidate_catalog=metadata['book_data_catalog'])
    return report,rows,metadata,parent


def test_fast_policy_requires_real_append_and_exact_parent():
    report,rows,metadata,parent=evidence()
    assert append_fast(metadata,parent)
    validate_server_samples(report,rows,metadata,parent)
    assert report['routes']['candidate']['/']['p95'] is None
    for changes in ({'parent_release_id':'other'},{'append_only_verified':False},
                    {'release_type':'repair'},{'dictionary_graph_release':{'id':'changed'}},
                    {'book_data_release':parent['book_data_release']}):
        with pytest.raises(ValueError):append_fast({**metadata,**changes},parent)


@pytest.mark.parametrize('change',['missing','identity','pressure','summary','stalled','duplicate','regular'])
def test_fast_review_rejects_incomplete_or_false_evidence(change):
    report,rows,metadata,parent=copy.deepcopy(evidence())
    if change=='missing':rows.pop()
    if change=='identity':report['candidate_release']='wrong'
    if change=='pressure':rows[-1]['resources']['available_mib']=100
    if change=='summary':report['routes']['candidate']['/']['median']=0
    if change=='stalled':rows[-1]['candidate']['/']=7
    if change=='duplicate':rows[-1]['elapsed']=1.
    if change=='regular':metadata.pop('review_mode')
    with pytest.raises(ValueError):validate_server_samples(report,rows,metadata,parent)


def test_contents_preserve_hierarchy_titles_and_wrapped_lines():
    rows=[dict(blocks=[dict(kind='content',text='目录\n第一章 自由 /1\n第一节 跨行\n标题 ..... (3)\n第二章 承认 _15')])]
    result=parse_contents(rows,[1])
    assert [(r['title'],r['label']) for r in result]==[('第一章自由',1),('第一节跨行标题',3),('第二章承认',15)]


def test_pdf_text_provenance_cannot_masquerade_as_paddle():
    package={'source_sha256':'a'*64,'source_evidence':dict(schema=1,kind='pdf_text_with_markdown',
        pdf_sha256='b'*64,markdown_sha256='c'*64)}
    with pytest.raises(ValueError,match='source identity'):register(package,[],{}, {})


def test_segmented_folios_do_not_merge_preface_and_body(monkeypatch):
    from ingestion import history_western as module
    rows=[dict(page=i,blocks=[dict(kind='text',text='序言' if i<3 else '第一章正文'),
                             dict(kind='number',text=str(i if i<3 else i-2))]) for i in range(1,5)]
    monkeypatch.setattr(module,'read_pages',lambda _: (rows,{'paddle_source':{'schema':1,'json_sha256':'b'*64}},[]))
    monkeypatch.setattr(module,'sha',lambda _: 'a'*64)
    spec=dict(metadata={},toc_pages=[],segments=[dict(id='preface',title='序言',start=1,end=2,offset=0,kind='front'),
                                                dict(id='body',title='正文',start=3,end=4,offset=2)])
    result=module.prepare('source.pdf',spec)
    assert [(r['label'],r['segment_id']) for r in result['pages']]==[('1','preface'),('2','preface'),('1','body'),('2','body')]
    assert all(r['label_evidence']['method']=='ocr_number_with_sequence' for r in result['pages'])


def test_history_and_western_share_actual_config_scope():
    from types import SimpleNamespace as S
    from ingestion.scopes import install
    books=[S(key=k,collection=c,available=True,folder='pdfs/自动入库') for k,c in
           [('史','marxism_history'),('西','western_marxism'),('旧','western_marxism')]]
    app=S(BOOK_CONFIGS=books,corpus=S(books={'史':1,'西':1,'旧':1}),
          _COLLECTION_LABELS={},_COLLECTION_DESCRIPTIONS={},_COLLECTION_LIBRARY_SORT_ORDERS={},
          CORPUS_SCOPES=({'id':'western_marxism','books':('旧',),'hints':()},))
    install(app)
    assert app._CORPUS_SCOPE_BY_ID['marxism_history']['books']==('史',)
    assert app._CORPUS_SCOPE_BY_ID['western_marxism']['books']==('西','旧')
    assert '2 个书目' in app._COLLECTION_DESCRIPTIONS['western_marxism']
