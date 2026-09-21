"""Catalog user-provided OCR without claiming Luna or full-page verification."""
import re
from .economics_catalog import COLLECTIONS as ECONOMICS_COLLECTIONS


COLLECTIONS = {
    **ECONOMICS_COLLECTIONS,
    'user_recommended': '用户荐书',
}


def register(package, books, manifest, volumes):
    proof=package.get('paddle_source',{})
    if proof.get('schema')!=1 or not re.fullmatch('[0-9a-f]{64}',proof.get('json_sha256','')):
        raise ValueError('Paddle source identity missing')
    m=package['metadata'];key=m['book_key'];v=m['volume']
    if m['collection'] not in COLLECTIONS or not isinstance(v,int) or v<1:
        raise ValueError('Invalid source catalog')
    if not m.get('authors') or not m.get('citation_title') or not re.fullmatch('[12][0-9]{3}',m['year']):
        raise ValueError('Bibliographic source fields missing')
    existing=next((b for b in books if b['key']==key),None)
    if existing:
        for field in ('citation_title','authors','publisher','single_volume','collection'):
            if existing.get(field)!=m[field]:raise ValueError('Catalog collision: '+field)
        if any(int(row['volume'])==v for row in manifest.get(key,[])):raise ValueError('Volume already exists')
    else:
        existing=dict(key=key,title='《'+m['citation_title']+'》',short_title='《'+m['citation_title']+'》',
                      citation_title=m['citation_title'],authors=m['authors'],publisher=m['publisher'],
                      place=m['place'],single_volume=m['single_volume'],collection=m['collection'],
                      translators=m.get('translators',[]),editors=m.get('editors',[]),
                      edition_note=m.get('edition',''),source_edition=m.get('source_edition',''),
                      folder='pdfs/自动入库',available=True,tag_class='default',
                      sort_order=max([b.get('sort_order',0) for b in books]+[0])+1)
        if m['collection'] == 'user_recommended':
            recommendation_id = int(m.get('recommendation_id') or 0)
            name = str(m.get('recommender_name') or '').strip()
            masked = str(m.get('recommender_email_masked') or '').strip()
            duration = int(m.get('public_window_days') or 0)
            if recommendation_id < 1 or not name or not re.fullmatch(r'[^@\s]+@[^@\s]+', masked) or 'x' not in masked.lower():
                raise ValueError('User recommendation attribution missing or not masked')
            if duration != 30:
                raise ValueError('User recommendation public window must be 30 days')
            existing.update(
                recommendation_id=recommendation_id,
                recommender_name=name,
                recommender_email_masked=masked,
                public_from='',
                public_until='',
                quality_note=str(package.get('quality_note') or '').strip(),
            )
        books.append(existing)
    if m.get('volume_label'):existing.setdefault('volume_labels',{})[v]=m['volume_label']
    existing.setdefault('volume_bibliography',{})[v]={field:m.get(field,[]) for field in ('translators','editors')}
    existing['volume_bibliography'][v].update(place=m['place'],edition_note=m.get('edition',''))
    if m.get('work_ranges'):existing['volume_bibliography'][v]['work_ranges']=m['work_ranges']
    manifest.setdefault(key,[]).append(dict(file=package['source_file'],volume=v,display_title=m['display_title'],
                                           sha256=package['source_sha256'],page_count=package['page_count']))
    manifest[key].sort(key=lambda x:x['volume'])
    volumes.setdefault(key,{})[v]=int(m['year'])
    return key,v
