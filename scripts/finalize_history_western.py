"""Apply the 2026-10-08 original-image review; retain every correction's basis."""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingestion.history_western import key

BLANKS = {1:[24,172,252],2:[25,223],3:[27,77,87,157,163,173],5:[17,117,119,185,231],
          7:[17,107,207,211,261],9:[11,13],10:[34],12:[18,104,204,206]}
FOLIOS = {1:[3,24,172,252],2:[24,25,222,223],3:[27,76,77,87,157,163,173],
 4:[10,15,43,93,144,192,232,270,281,321,366,415,462,511,549,586,621,663],
 5:[16,17,117,118,119,184,185,231],6:[4,7,21,32,75,96,143,166,192],
 7:[14,17,100,107,207,208,211,261],8:[],9:[11,12,13],
 10:[33,34,47,122,145,146,227],11:[12,28,49,77,105,110,137,164,198,236,290,326,329,330,332,333,335],
 12:[17,18,103,104,204,205,206]}
INDEX_319 = '''人名译名对照表
[德] 柯尔施 Karl Korsch
[法] 魁奈 Francois Quesnay
[法] 拉法格 Paul Lafargue
[波] 兰格 Oskar Lange
[英] 李嘉图 David Ricardo
[德] 李斯特 Friedrich List
[俄] 列宁 Vladimir Ilyich Lenin
[瑞典] 林德贝克 Assar Lindbeck
[匈] 卢卡奇 Georg Lukacs
[德] 卢森堡 Rosa Luxemburg
[美] 罗默 John Roemer
[英] 马尔萨斯 Thomas Robert Malthus
[德] 马克思 Karl Marx
[英] 马歇尔 Alfred Marshall
[比] 曼德尔 Ernest Mandel
[英] 米克 Ronald Meek
[奥] 米塞斯 Ludwig von Mises
[英] 莫尔 Thomas More
[英] 穆勒 James Mill
[意] 内格里 Antonio Negri
[英] 诺夫 Alec Nove
[英] 欧文 Robert Owen
[奥] 庞巴维克 Eugen von Bohm-Bawerk
[英] 配第 William Petty
[阿根廷] 普雷维什 Raul Prebisch
[法] 萨伊 Jean Baptiste Say
[日] 森岛通夫 Michio Morishima
[法] 圣西门 Claude Henri de Saint-Simon
[苏] 斯大林 Josef Vissarionovich Stalin
[英] 斯密 Adam Smith
[美] 斯威齐 Paul Marlor Sweezy
[苏] 瓦尔加 Eugen Varga'''


def finalize(packages, specs):
    if len(packages)!=12:raise ValueError('review applies to exactly twelve books')
    reports=[]
    for n,(b,spec) in enumerate(zip(packages,specs['books']),1):
        if b['metadata']!=spec['metadata'] or b['audit']['folio_review']!=FOLIOS[n]:
            raise ValueError('review inputs changed: '+str(n))
        rows=b['pages'];toc=b['toc'];corrections=[]
        def replace_page(p,text,reason,kind=None):
            row=rows[p-1]
            corrections.append(dict(pdf_page=p,before_sha256=hashlib.sha256(row['text'].encode()).hexdigest(),
                                    method='original_image_manual_transcription',reason=reason,text=text))
            row['original_blocks']=row['blocks']
            row.update(text=text,blocks=[dict(kind='text',text=text,bbox=None,order=0)] if text else [])
            if kind:row['kind']=kind
            if kind=='blank':row.update(label='',segment_id='blank',segment_title='原件空白页')
        def add(title,p,level=1,authors=None):
            row=rows[p-1]
            t=dict(title=title,pdf_page=p,printed_page=row['label'],level=level,kind=row['kind'],
                   sort_order=-1,authors=authors or [],date='',toc_pdf_page=None,
                   evidence_method='original_image_and_body_text',provenance_verified=True)
            toc.append(t);return t
        for p in BLANKS.get(n,[]):replace_page(p,'','原图确为空白；删除识别幻觉',kind='blank')
        if n==1:
            replace_page(3,'简明马克思主义史\nJianming Makesi zhuyi shi\n庄福龄 主编\n人民出版社','书名页漏识别',kind='front')
            for t in toc:
                if t['title'].startswith('系统化和多方面展开第一节'):t['title']=t['title'].removeprefix('系统化和多方面展开')
                if t['title'].startswith('——探索、深化和面向新世纪第一节'):t['title']=t['title'].removeprefix('——探索、深化和面向新世纪')
                if t['title'].startswith('第四章'):t['title']+='——系统化和多方面展开'
                t['level']=1 if re.match('^第.+章',t['title']) or t['title'] in ['绪言','结束语','后记'] else 2 if re.match('^第.+节',t['title']) else 3
            add('第五章博大精深的理论体系（下）——探索、深化和面向新世纪',141)
        elif n==2:
            for t in toc:
                if 26<=t['pdf_page']<357:t['level']+=1
            add('第一篇黑格尔哲学的基础',24);add('第二篇社会理论的兴起',222)
        elif n==3:
            toc[:]=[t for t in toc if t['pdf_page'] not in [8,28]]
            add('中译本代序：从“资产阶级世纪”中苏醒',8,authors=['张旭东'])
            add('导言：瓦尔特·本雅明：1892—1940',28,authors=['汉娜·阿伦特'])
            for t in toc:
                if t['title'].startswith('附录'):t['pdf_page']=284;t['printed_page']='277'
            corrections.append(dict(kind='paper_toc_erratum',toc_pdf_page=7,printed_toc_label='289',actual_pdf_page=284,actual_printed_page='277'))
        elif n==4:
            for t in toc:
                if t['title'].startswith('导论一、'):t['title']=t['title'].removeprefix('导论');t['level']=2
                if t['title'].startswith('马克思主义发展史第四章'):t['title']=t['title'].removeprefix('马克思主义发展史')
                if t['level']==3:t['level']=2
            add('导论',10)
        elif n==5:
            for t in toc:
                if re.match('^第.+章',t['title']):t['level']=2
        elif n==7:
            replace_page(14,'第一部分\n历史回顾：黑格尔的原始观念','原图无英文测试句，删除识别幻觉')
            for t in toc:
                if re.match('^第.+章',t['title']):t['level']=2
                if t['title']=='术语对照表':t['pdf_page']=266;t['printed_page']='253'
            add('校译者前言',5,authors=['曹卫东']);add('导言',8,authors=spec['metadata']['authors'])
        elif n==8:add('总序',5,authors=['郭为禄','叶青'])
        elif n==9:
            for t in toc:t['level']=1 if t['pdf_page']<24 or t['title']=='参考文献' or re.match('^[一二三四]、',t['title']) else 2
        elif n==10:
            toc[:]=[t for t in toc if t['title'] not in ['I','译文经典']]
            for t in toc:
                if re.match('^第.+章',t['title']):t['level']=2
            add('第一篇在现实原则的支配下',33);add('第二篇超越现实原则',145)
        elif n==11:
            toc[:]=[t for t in toc if t['title'] not in ['导论一、马克思主义经济学说史的研究对象和主要内容','第一节马克思主义经济学在中国的早期传播与','毛泽东经济思想的形成']]
            for t in toc:
                if t['pdf_page']<28 and t['title']!='导论':t['level']=2
            replace_page(330,INDEX_319,'原件有完整人名表；JSON漏识别，以原图逐行录入',kind='body')
            rows[329].update(label='319',segment_id='body',segment_title='正文')
            replace_page(335,'ISBN 978-7-04-054443-5\n定价43.00元','封底条码和定价漏识别',kind='front')
        elif n==12:
            for t in toc:
                if t['title'].startswith('25已'):t['title']=t['title'].replace('25已','25己',1)
                if re.match(r'^\d+',t['title']):t['level']=2
            # The paper contents already contain the three part headings.
            for title,p in [('第一部分（1944年）',17),('第二部分（1945年）',103),('第三部分（1946—1947年）',205)]:
                toc[:]=[t for t in toc if t['pdf_page']!=p];add(title,p)
        # Every unrecognized folio was checked against the original and its
        # adjacent sequence. Unprinted divider leaves remain explicitly inferred.
        for row in rows:
            if row['page'] in FOLIOS[n] and row['label']:
                row['label_evidence'].update(method='bounded_sequence_interpolation',
                    review='原图及相邻页序核对；缺印页码不声称原图可见',source_pdf_page=row['page'])
            row['checked']=True
            if row['segment_id']=='body':row['segment_title']=''
            row.setdefault('label_evidence',dict(method='unpaginated_image_review',source_pdf_page=row['page']))
        # Do not turn scanner-added metadata or paper contents into body quotes.
        excluded=[r['page'] for r in rows if not r['label'] and r['kind']!='blank']
        for p in spec['toc_pages']:
            rows[p-1]['label']=''
            rows[p-1]['label_evidence']=dict(method='front_matter_image_review',source_pdf_page=p)
        toc.sort(key=lambda t:(t['pdf_page'],t['level'],t['sort_order']))
        seen=set();toc[:]=[t for t in toc if not ((t['pdf_page'],key(t['title'])) in seen or seen.add((t['pdf_page'],key(t['title']))))]
        ranges=[]
        for t in toc:
            title=t['title'];p=t['pdf_page']
            if not t['authors']:
                t['authors']=spec['metadata']['authors']
                if '译后记' in title or (n==9 and p==8):t['authors']=spec['metadata']['translators']
            t.update(provenance_verified=True,evidence_method='paper_contents_body_and_original_image_review')
            segment=next((s for s in spec['segments'] if s['start']<=p<=s['end']),None)
            end=segment['end'] if segment else p
            next_page=next((x['pdf_page'] for x in toc if x['pdf_page']>p and x['level']<=t['level']),end+1)
            t['end_pdf_page']=min(end,next_page-1);t['sort_order']=len(ranges)
            if t['end_pdf_page']<p:raise ValueError('invalid article range')
            ranges.append(t)
        # Narrow author overrides precede broad book attribution.
        overrides={3:[(8,26,'中译本代序：从“资产阶级世纪”中苏醒',['张旭东'],[]),(28,75,'导言：瓦尔特·本雅明：1892—1940',['汉娜·阿伦特'],spec['metadata']['translators'])],
          7:[(5,7,'校译者前言',['曹卫东'],[])],8:[(5,8,'总序',['郭为禄','叶青'],[]),(246,249,'译后记（代跋）',['郑兴'],[])],
          9:[(8,10,'译者前言',['曹卫东'],[])],5:[(232,236,'译后记',['刘继'],[])],
          10:[(288,298,'译后记',spec['metadata']['translators'],[])]}.get(n,[])
        b['metadata']['work_ranges']=[dict(start=start,end=end,title=title,authors=authors,translators=translators) for start,end,title,authors,translators in overrides]
        report=dict(book=n,title=b['metadata']['display_title'],source_sha256=b['source_sha256'],
            physical_pages=len(rows),toc_entries=len(toc),segments=spec['segments'],copyright_pages=spec['copyright_pages'],
            copyright_review='书目与版权页核验；不代表网络传播授权',
            corrected=corrections,resolved_issues=b['issues'],folio_review=FOLIOS[n],non_body_unpaginated=excluded)
        b['audit'].update(review_record=report,release_reviewed=True,folio_review=[],
                          rights_authorization_verified=False,review_date='2026-10-08')
        b['issues']=[];reports.append(report)
    return packages,reports


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True);a=p.parse_args()
    specs=json.loads((Path(__file__).resolve().parents[1]/'config/history_western_sources.json').read_text('utf8'))
    packages=json.loads((a.directory/'draft-packages.json').read_text('utf8'))
    packages,reports=finalize(packages,specs)
    evidence={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted((a.directory/'qa').glob('*.png'))}
    for b in packages:b['audit']['image_evidence_sha256']=evidence
    for name,value in [('packages.json',packages),('review-records.json',reports)]:
        (a.directory/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(dict(books=len(packages),pages=sum(p['page_count'] for p in packages)),ensure_ascii=False))
