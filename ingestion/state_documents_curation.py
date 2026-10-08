"""Recorded original-page decisions for the October 2026 state-document batch.

The input audit remains unchanged. Publication additionally requires an explicit
QA sheet ledger; running this module alone never approves unviewed evidence.
"""
import argparse
import copy
import json
import re
from pathlib import Path
from .state_documents import clean, digest, key, first_heading
from book_data_release import canonical

TITLES = {
    1: ('人民政协重要文献选编',3), 2: ('十七大以来重要文献选编',2),
    3: ('十五大以来重要文献选编',1), 4: ('十二大以来重要文献选编',1),
    5: ('十四大以来重要文献选编',3), 6: ('十六大以来重要文献选编',3),
    7: ('三中全会以来重要文献选编',1), 8: ('三中全会以来重要文献选编',2),
    9: ('十三大以来重要文献选编',3), 10: ('十三大以来重要文献选编',2),
    11: ('十二大以来重要文献选编',3),12: ('十二大以来重要文献选编',2),
    13: ('十五大以来重要文献选编',3),14: ('十五大以来重要文献选编',2),
    15: ('十四大以来重要文献选编',1),16: ('深入学习实践科学发展观活动领导干部学习文件选编',1),
    17: ('人民政协重要文献选编',1),18: ('人民政协重要文献选编',2),
    19: ('十七大以来重要文献选编',1),20: ('十七大以来重要文献选编',3),
    21: ('十三大以来重要文献选编',1),22: ('十六大以来重要文献选编',1),23: ('十六大以来重要文献选编',2),
}
SETS = {
    '三中全会以来重要文献选编': ('9787507332629',8,3),
    '十二大以来重要文献选编': ('9787507332636',11,3),
    '十三大以来重要文献选编': ('9787507332643',9,3),
    '十四大以来重要文献选编': ('9787507332650',5,3),
    '十五大以来重要文献选编': ('9787507332667',13,3),
    '十六大以来重要文献选编': ('9787507332674',6,4),
}
TITLE_FIXES = {
    (6,482): '国务院关于完善粮食流通体制改革政策措施的意见',
    (9,635): '进一步搞好党的建设，保证经济建设和改革开放的顺利进行',
    (10,20): '中共中央、国务院关于近期做几件群众关心的事的决定',
    (10,117): '中华人民共和国集会游行示威法',
    (10,667): '中共中央批转中央纪委《关于加强党风和廉政建设的意见》的通知',
    (12,356): '附：中共中央办公厅、国务院办公厅关于贯彻执行《中共中央、国务院关于进一步制止党政机关和党政干部经商、办企业的规定》几个问题的说明',
    (13,420): '中共中央、国务院关于做好二〇〇二年农业和农村工作的意见',
    (14,689): '中共中央、国务院关于做好二〇〇一年农业和农村工作的意见',
    (19,426): '国务院关于加强市县政府依法行政的决定',
    (19,711): '关于深入贯彻落实科学发展观的若干重大问题',
    (19,849): '国务院关于珠江三角洲地区改革发展规划纲要（二〇〇八——二〇二〇年）的批复',
}
ATTACHMENTS = {
    1: [(84,'附一：关于《中国人民政治协商会议章程修正案（草案）》的说明','郑万通'),
        (95,'附二：中国人民政治协商会议章程修正案','')],
    17:[(35,'附：《关于参加新政治协商会议的单位及其代表名额的规定（草案）》的说明','李维汉'),
        (84,'附：《中华人民共和国中央人民政府组织法》的草拟经过及其基本内容','董必武'),
        (229,'附：关于《中国人民政治协商会议章程（草案）》的说明','章伯钧')],
}
INFERRED = {(9,17),(11,20),(12,18)}
MIDDLE_AUTHORS = {
    **{p:'邓小平' for p in [4,5,7,11,15,19,22,23,25,30,108,119]},
    28:'陈云',72:'刘澜涛',81:'邓颖超',87:'邓颖超',90:'陈云',92:'邓颖超',100:'邓颖超',
    110:'李先念',124:'李先念',128:'李先念',143:'江泽民',147:'江泽民',160:'江泽民',
    166:'李先念',171:'江泽民',174:'江泽民',178:'李瑞环',189:'李瑞环',216:'江泽民',
    222:'江泽民',225:'李瑞环',232:'江泽民',238:'江泽民',240:'李瑞环',250:'江泽民',
    258:'李瑞环',279:'郑万通',
}


def compact(value):
    return re.sub(r'\s+', '', clean(value))


def article_date(row):
    """Keep the printed wording, including adoption/revision qualification."""
    blocks = [compact(b['text']) for b in row['blocks']
              if b['kind'] not in {'header','number','footer','footnote'}]
    for i,s in enumerate(blocks[:9]):
        if s.startswith('(') and '年' in s and len(s)<512:
            while ')' not in s and i+1<len(blocks) and len(s)<512:
                i+=1;s+=blocks[i]
            if ')' in s:
                return s[:s.index(')')+1]
    return ''


def bibliography(n, inventory):
    title,volume=TITLES[n]
    label='' if n==16 else ('下' if n==8 else {1:'上',2:'中',3:'下'}[volume])
    m=dict(book_key=title,citation_title=title,volume=volume,volume_label=f'（{label}）' if label else '',
           display_title=title+(f'（{label}）' if label else ''),single_volume=n==16,
           authors=[],editors=['中共中央文献研究室'],collection='party_state_documents',
           publisher='中央文献出版社',place='北京',edition='第1版',impression='',year='2011',
           source_edition='2011年6月第1版（再版套书）')
    if title in SETS:
        isbn,book,page=SETS[title]
        m.update(isbn=isbn,isbn_scope='set')
        if n==book:m['impression']='2011年6月第1次印刷'
        evidence=[dict(source_sha256=inventory[book-1]['source_sha256'],pdf_page=page,
                       fields=['publisher','isbn','edition','year'],scope='matched_set_copyright_page'),
                  dict(source_sha256=inventory[n-1]['source_sha256'],pdf_page=437 if n==4 else 2,
                       fields=['title','volume','editors','publisher'],scope='own_title_page')]
        m['bibliography_note']='套书 ISBN 和版次据同套下册版权页，并与本册题名页核对；无本册印次证据时不填写印次。'
    else:
        evidence=[dict(source_sha256=inventory[n-1]['source_sha256'],pdf_page=4 if n==2 else 3,
                       fields=['publisher','isbn','edition','impression','year'],scope='own_copyright_page')]
        if n in [1,17,18]:
            m.update(year='2009',isbn='9787503424861',isbn_scope='set',publisher='中央文献出版社、中国文史出版社',
                     editors=['政协全国委员会办公厅','中共中央文献研究室'],
                     impression='2009年9月第1次印刷',source_edition='2009年9月第1版',cip_year='2009.8')
        elif n==16:
            m.update(year='2008',isbn='9787507326277；9787509900161',isbn_scope='joint_publishers',
                     publisher='中央文献出版社、党建读物出版社',impression='2008年9月第1次印刷',source_edition='2008年9月第1版')
        elif n==19:
            m.update(year='2009',isbn='9787507328523',isbn_scope='volume',impression='2009年8月第1次印刷',source_edition='2009年8月第1版')
        elif n==2:
            m.update(isbn='9787507332568',isbn_scope='volume',impression='2011年4月第1次印刷',source_edition='2011年4月第1版')
    m['bibliography_evidence']=evidence
    return m


def curate(n, audit, inventory, qa):
    if n==20:raise ValueError('Truncated lower volume cannot be released')
    d=copy.deepcopy(audit);decisions=[]
    if qa:
        if qa['source_sha256']!=d['source_sha256']:
            raise ValueError('QA belongs to another original')
        checks=qa['checks'];samples={r['pdf_page'] for r in checks['samples']}
        if len(samples)<10 or not {d['body_start'],d['body_end']}<=samples:
            raise ValueError('QA lacks ten samples or body endpoints')
        if not set(d['toc_pages'])<={r['pdf_page'] for r in checks['toc']}:
            raise ValueError('Unreviewed paper contents page')
        if not {r['page'] for r in d['folio_review']}<={r['pdf_page'] for r in checks['folios']}:
            raise ValueError('Unreviewed pagination ambiguity')
    for r in d['pages']:
        if r['kind']=='body' and r['label_evidence']['method']=='needs_review':
            method='bounded_sequence_interpolation' if (n,r['page']) in INFERRED else 'image_review'
            r['label_evidence'].update(method=method,source_pdf_page=r['page'],review='state-documents-20261008')
        if r['page'] in d['toc_pages']:
            r.update(segment_id='contents',segment_title='目录')
            # Repeated front-matter folios are preserved as evidence; unqualified
            # page-number lookup remains reserved for the main body.
            r['label_evidence']=dict(method='front_matter_image_review',observed=r['observed_labels'],
                                    printed_labels=r['observed_labels'],source_pdf_page=r['page'])
    if n==4:
        for p in [437,438,440,441]:d['pages'][p-1].update(kind='front',segment_id='misplaced_front_matter',segment_title='错置前置页')
    if n==18:
        d['toc']=[]
        for row in d['pages']:
            if row['kind']=='body' and any(b['kind']=='doc_title' for b in row['blocks']):
                page=row['page'];title=compact(first_heading(row))
                if page in [72,205,279]:title=('附一：' if page==279 else '附：')+title
                d['toc'].append(dict(title=title,author=MIDDLE_AUTHORS.get(page,''),pdf_page=page,
                                     printed_page=row['label'],level=2 if page in [72,205,279] else 1,
                                     kind='body',evidence_method='body_heading_image_review'))
        decisions.append(dict(kind='missing_paper_toc',resolution='52篇正文标题逐项图像核对补建'))
    for page,title,author in ATTACHMENTS.get(n,[]):
        if not any(t['pdf_page']==page for t in d['toc']):
            d['toc'].append(dict(title=title,author=author,pdf_page=page,printed_page=d['pages'][page-1]['label'],
                                 level=2,kind='body',evidence_method='paper_attachment_and_body_image_review'))
    for entry in d['toc']:
        page=entry['pdf_page'];entry['title']=compact(entry['title']);entry['author']=compact(entry.get('author',''))
        replacement=TITLE_FIXES.get((n,page))
        if replacement:
            decisions.append(dict(kind='toc_title',pdf_page=page,before=entry['title'],after=replacement,
                                  basis='paper_toc_and_body_image_review'))
            entry['title']=replacement;entry['evidence_method']='paper_toc_and_body_image_review'
            if (n,page)==(9,635):entry['author']='宋平'
            if (n,page)==(19,711):entry['author']='温家宝'
        if entry['title'].startswith('附'):entry['level']=2
        entry.update(authors=[entry['author']] if entry['author'] else [],date=article_date(d['pages'][page-1]),
                     provenance_verified=True,evidence_pdf_page=page)
        dated_notes={(11,290):'1987年4月13日签署；1988年1月15日生效',
                     (12,94):'1984年12月19日签署；1985年5月27日生效',
                     (12,416):'1986年4月12日原则批准；1986年4月15日发表摘要'}
        if (n,page) in dated_notes:
            entry.update(date=dated_notes[n,page],date_evidence='body_footnote_image_review')
        elif (n,page)==(7,369):
            entry['date_note']='原页仅载：中国共产党第十一届中央委员会第五次全体会议通过；未补写具体日期。'
        entry.setdefault('evidence_method','paper_toc_with_exact_body_heading')
    d['toc'].sort(key=lambda t:t['pdf_page'])
    for i,t in enumerate(d['toc']):
        t['sort_order']=i;t['end_pdf_page']=d['toc'][i+1]['pdf_page']-1 if i+1<len(d['toc']) else d['body_end']
        assert 1<=t['pdf_page']<=t['end_pdf_page']<=d['page_count']
    resolved_titles={p for b,p in TITLE_FIXES if b==n}
    unresolved=[x for x in d['issues'] if not (
        x['kind']=='toc_title' and x['pdf_page'] in resolved_titles or
        x['kind']=='unpaged_attachment' and n in ATTACHMENTS or x['kind']=='no_paper_toc' and n==18)]
    if unresolved:raise ValueError(f'B{n:02d} unresolved: {unresolved}')
    source=d['source_sha256']
    result=dict(book_id=source[:32],source_sha256=source,source_file='pdfs/自动入库/'+source+'.pdf',
                page_count=d['page_count'],metadata=bibliography(n,inventory),pages=d['pages'],toc=d['toc'],
                paddle_source=dict(schema=1,json_sha256=__import__('hashlib').sha256(canonical(d['parts'])).hexdigest(),
                                   parts=d['parts'],coordinates_verified=False),issues=[],
                audit=dict(release_reviewed=bool(qa),source_pdf=d['pdf'],decisions=decisions,
                           resolved_issues=d['issues'],qa=qa,body_range=[d['body_start'],d['body_end']],
                           original_page_count=d['page_count'],pagination_offset=d['offset']))
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--audit',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--qa-ledger',type=Path);a=p.parse_args()
    inventory=json.loads((a.audit/'inventory.json').read_text('utf-8'))
    qa=json.loads(a.qa_ledger.read_text('utf-8')) if a.qa_ledger else {}
    packages=[curate(n,json.loads((a.audit/e['audit']).read_text('utf-8')),inventory,qa.get(str(n)))
              for n,e in enumerate(inventory,1) if n!=20]
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_bytes(canonical(packages))
    print(json.dumps(dict(books=len(packages),pages=sum(p['page_count'] for p in packages),
                         articles=sum(len(p['toc']) for p in packages),sha256=digest(a.output))))


if __name__=='__main__':main()
