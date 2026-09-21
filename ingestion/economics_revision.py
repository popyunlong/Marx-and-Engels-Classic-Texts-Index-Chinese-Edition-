"""Refine only this batch's published text, with exact prior-publication guards."""
import hashlib


def refine(c,package,normalize):
    previous=package['economics28_revision']['previous_package']
    for key in ['book_id','source_sha256','source_file','metadata','toc','page_count']:
        if package[key]!=previous[key]:raise ValueError('精校不得暗改已核准的书目、目录或页码')
    old={p['page']:p for p in previous['pages']}
    columns=[r[1] for r in c.execute('PRAGMA table_info(pages)')]
    expected={}
    for p in package['pages']:
        baseline=old[p['page']]
        if p['label']!=baseline['label']:raise ValueError('精校页码基线变化')
        raw=c.execute('SELECT * FROM pages WHERE source_file=? AND pdf_page=?',(package['source_file'],p['page'])).fetchall()
        if len(raw)!=1:raise ValueError('精校页面身份不唯一')
        row=dict(zip(columns,raw[0]))
        if row['raw_text']!=baseline['text'] or str(row['printed_page'] or '')!=baseline['label']:
            raise ValueError('已存在新的生产修订，保留人工修订并等待核对')
        if p['text']==baseline['text']:continue
        if not p.get('checked'):raise ValueError('文字修改缺少整页原图核验')
        row['raw_text']=p['text'];row['normalized_text']=normalize(p['text'])
        c.execute('UPDATE pages SET raw_text=?,normalized_text=? WHERE id=?',(row['raw_text'],row['normalized_text'],row['id']))
        expected[row['id']]=tuple(row[k] for k in columns)
    return expected
