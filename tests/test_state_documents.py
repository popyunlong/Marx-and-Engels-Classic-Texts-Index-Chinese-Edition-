import json
from pathlib import Path
import pytest
from ingestion.state_documents import read_parts, audit
from book_config import BookConfig


def page(n, title=''):
    return {'prunedResult': {'parsing_res_list': [
        {'block_label': 'number', 'block_content': str(n)},
        {'block_label': 'doc_title', 'block_content': title},
        {'block_label': 'text', 'block_content': '原始正文不得因合并改变。'},
    ]}}


def test_split_parts_use_absolute_physical_ranges(tmp_path):
    pdf=tmp_path/'同一本.pdf'
    for a,b in [(11,12),(1,10)]:
        p=tmp_path/f'同一本_{a}-{b}.pdf_by_PaddleOCR-VL-1.6.json'
        p.write_text(json.dumps([page(i+300) for i in range(a,b+1)]),encoding='utf-8')
    rows,parts=read_parts(pdf)
    assert [r['page'] for r in rows]==list(range(1,13))
    assert rows[10]['source_part_page']==1
    assert rows[10]['blocks'][0]['text']=='311'
    assert parts[0]['start']==1 and parts[1]['start']==11


@pytest.mark.parametrize('ranges', [[(1,2),(2,3)],[(1,2),(4,5)]])
def test_split_overlap_and_gap_rejected(tmp_path,ranges):
    for a,b in ranges:
        (tmp_path/f'书_{a}-{b}.pdf_by_PaddleOCR-VL-1.6.json').write_text(json.dumps([page(a),page(b)]))
    with pytest.raises(ValueError,match='gap/overlap'):
        read_parts(tmp_path/'书.pdf')


def test_missing_tail_is_not_a_valid_table_of_contents(tmp_path):
    import fitz
    pdf=tmp_path/'书.pdf';doc=fitz.open()
    for _ in range(4):doc.new_page()
    doc.save(pdf);doc.close()
    pages=[{'prunedResult':{'parsing_res_list':[
        {'block_label':'doc_title','block_content':'目录'},
        {'block_label':'text','block_content':'第一篇 …… (629)\n未扫描的篇章 …… (640)'}]}}]
    pages.extend([page(629,'第一篇'),page(630),page(631)])
    pdf.with_name(pdf.name+'_by_PaddleOCR-VL-1.6.json').write_text(json.dumps(pages))
    data=audit(pdf)
    assert data['offset']==-627
    assert data['pages'][1]['label']=='629'
    assert data['toc'][0]['pdf_page']==2
    assert any(i['kind']=='toc_outside_pdf' for i in data['issues'])


def test_volume_bibliography_does_not_contaminate_other_volumes():
    book=BookConfig(key='书',title='书',short_title='书',citation_title='书',folder='pdfs/书',
        sort_order=1,publisher='原出版社',place='北京',tag_class='default',edition_note='原版',
        volume_bibliography={2:{'publisher':'分册出版社','edition_note':'修订版','editors':['分册编者'],'isbn':'123'}})
    revised=book.for_volume(2)
    assert revised.publisher=='分册出版社' and revised.editors==('分册编者',)
    assert revised.isbn=='123' and revised.edition_note=='修订版'
    assert book.for_volume(1) is book and book.publisher=='原出版社'
