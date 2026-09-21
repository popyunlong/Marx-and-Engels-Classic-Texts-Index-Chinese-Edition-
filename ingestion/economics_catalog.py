"""Evidence-reviewed multivolume metadata for economics28; legacy imports unchanged."""
from __future__ import annotations
import re

COLLECTIONS={'smith_works':'亚当·斯密著作','ricardo_works':'大卫·李嘉图著作',
             'mill_works':'穆勒著作','proudhon_works':'蒲鲁东著作'}


def register(package,books,manifest,volumes):
    evidence=package.get('economics28',{})
    if (evidence.get('batch'),evidence.get('model'),evidence.get('reasoning'))!=('economics28-20260908','gpt-5.6-luna','medium'):
        raise ValueError('经济学书目缺少正确批次的 Luna 中度思考核验')
    m=package['metadata']; key=str(m.get('book_key','')).strip(); volume=m.get('volume')
    if not key or isinstance(volume,bool) or not isinstance(volume,int) or volume<1:
        raise ValueError('经济学书目缺少稳定书目键或卷册序号')
    if m.get('collection') not in COLLECTIONS or not isinstance(m.get('single_volume'),bool):
        raise ValueError('经济学书目分类或卷册格式无效')
    for field in ('title','citation_title','display_title','publisher','year','authors'):
        if not m.get(field): raise ValueError('经济学书目字段缺失: '+field)
    if not isinstance(m['authors'],list) or not all(isinstance(x,str) and x for x in m['authors']): raise ValueError('作者格式无效')
    if m['collection']=='mill_works' and not any(any(name in author for name in ('约翰','詹姆斯','John','James')) for author in m['authors']):
        raise ValueError('穆勒须核准全名，不能混淆詹姆斯与约翰·斯图亚特')
    if not re.fullmatch('[12][0-9]{3}',str(m['year'])): raise ValueError('出版年份无效')
    existing=next((b for b in books if b['key']==key),None)
    stable=dict(citation_title=m['citation_title'],publisher=m['publisher'],authors=m['authors'],collection=m['collection'],single_volume=m['single_volume'])
    if existing:
        if any(existing.get(k)!=v for k,v in stable.items()): raise ValueError('同名书库的作者或版本信息冲突')
        if any(int(v['volume'])==volume for v in manifest.get(key,[])): raise ValueError('卷册已存在，拒绝覆盖')
        # Citation rendering currently resolves these fields at book level.
        # Reject a differing volume before mutating any candidate configuration.
        for field, target, default in [('translators','translators',[]),('editors','editors',[]),
                                       ('edition','edition_note',''),('place','place','[出版地不详]')]:
            if (existing.get(target) or default) != (m.get(field) or default):
                raise ValueError('分卷责任者或版本信息冲突，须先支持按卷引文: '+field)
    else:
        existing=dict(key=key,title='《'+m['citation_title']+'》',short_title='《'+m['citation_title']+'》',
                      folder='pdfs/自动入库',available=True,sort_order=max([int(b.get('sort_order',0)) for b in books]+[0])+1,
                      tag_class='default',place=m.get('place') or '[出版地不详]',translators=m.get('translators',[]),editors=m.get('editors',[]),
                      edition_note=m.get('edition',''),**stable)
        books.append(existing)
    if m.get('volume_label'): existing.setdefault('volume_labels',{})[volume]=m['volume_label']
    manifest.setdefault(key,[]).append(dict(file=package['source_file'],volume=volume,display_title=m['display_title'],
                                          sha256=package['source_sha256'],page_count=package['page_count']))
    manifest[key].sort(key=lambda x:int(x['volume']))
    volumes.setdefault(key,{})[volume]=int(m['year'])
    return key,volume


def install_scopes(app):
    for i,(key,label) in enumerate(COLLECTIONS.items(),7):
        for attr,value in [('_COLLECTION_LABELS',label),('_COLLECTION_LIBRARY_SORT_ORDERS',i),
                           ('_COLLECTION_DESCRIPTIONS','按原书目录阅读、检索原文并定位引文；识别文本持续校对。')]:
            if hasattr(app,attr): getattr(app,attr)[key]=value
        additions=tuple(b.key for b in app.BOOK_CONFIGS if b.collection==key and b.available and b.key in app.corpus.books)
        if not additions: continue
        group=next((g for g in app.CORPUS_SCOPES if g['id']==key),None)
        if group is None:
            app.CORPUS_SCOPES=(*app.CORPUS_SCOPES,dict(id=key,label=label,books=additions,hints=()))
        else: group['books']=tuple(dict.fromkeys((*group['books'],*additions)))
