"""Per-volume citation responsibility, with no changes to unrelated book configs."""
from dataclasses import replace
from functools import lru_cache
from functools import wraps
from contextvars import ContextVar
from pathlib import Path
import yaml


@lru_cache(maxsize=1)
def bibliography():
    from runtime_env import CONFIG_DIR
    data=yaml.safe_load((Path(CONFIG_DIR)/'books.yaml').read_text(encoding='utf-8'))
    return {b['key']:b.get('volume_bibliography',{}) for b in data['books'] if b.get('volume_bibliography')}


def resolve(base, metadata, volume, pages):
    values=metadata.get(volume,metadata.get(str(volume),{}))
    if not values:return base
    updates={k:tuple(values[k]) for k in ('authors','translators','editors') if k in values}
    updates.update({k:values[k] for k in ('place','edition_note','citation_title') if k in values})
    page_numbers=[p.pdf_page for p in pages]
    for part in values.get('work_ranges',[]):
        if page_numbers and all(part['start']<=p<=part['end'] for p in page_numbers):
            updates.update(citation_title=part['title'],authors=tuple(part['authors']),translators=tuple(part['translators']))
            break
    return replace(base,**updates)


def install_citations(cls, metadata_loader=bibliography):
    if cls.__dict__.get('_paddle_citations_installed'):return
    active=ContextVar('paddle_citation_volume',default=None)
    original_config=cls.get_book_config
    def get_config(self,book):
        base=original_config(self,book);context=active.get()
        if context is None or context[0]!=book:return base
        return resolve(base,metadata_loader().get(book,{}),context[1],context[2])
    cls.get_book_config=get_config
    def wrap(method):
        @wraps(method)
        def call(self,book,volume,pages,*args,**kwargs):
            token=active.set((book,volume,pages))
            try:return method(self,book,volume,pages,*args,**kwargs)
            finally:active.reset(token)
        return call
    for name in ('_make_citation','_make_citation_gb','_make_citations','_citation_parts'):
        setattr(cls,name,wrap(getattr(cls,name)))
    cls._paddle_citations_installed=True


def install(app):
    if app.corpus is None or getattr(app,'_paddle_runtime_installed',False):return
    app._paddle_runtime_installed=True
    install_citations(type(app.corpus))
    from .paddle_scope import install as install_bound_scopes
    install_bound_scopes(app)
    from .paddle_retrieval import install as install_topic_retrieval
    install_topic_retrieval(app,bibliography)
    original=app._get_page_context_payload
    @wraps(original)
    def context(source_file,page_number):
        result=original(source_file,page_number)
        metadata=bibliography().get(result.get('book'),{})
        if metadata:
            from types import SimpleNamespace
            cfg=resolve(app.corpus.get_book_config(result['book']),metadata,result['volume'],[SimpleNamespace(pdf_page=page_number)])
            result=dict(result,display_title=cfg.citation_title,source_authors=list(cfg.authors),
                        source_quality='识别文本持续校对，请以 PDF 原文为准')
            from flask import url_for
            result['source_url']=url_for('pdf_viewer',file=source_file,page=page_number)
            # Do not carry adjacent text across the author boundary of a bound book.
            values=metadata.get(result['volume'],metadata.get(str(result['volume']),{}))
            for part in values.get('work_ranges',[]):
                if page_number==part['start']:result['previous_excerpt']=''
                if page_number==part['end']:result['next_excerpt']=''
        return result
    app._get_page_context_payload=context
    from .paddle_reader import install as install_reader_sources
    install_reader_sources(app,bibliography)
    from flask import request
    @app.app.after_request
    def quality_notice(response):
        if request.path!='/viewer' or response.status_code!=200 or 'text/html' not in response.content_type:return response
        source=(request.args.get('file') or '').replace('\\','/')
        volume=app.corpus.get_volume_by_source_file(source)
        if volume is None or volume.book not in bibliography():return response
        import re
        html=response.get_data(as_text=True)
        html=re.sub(r'(<h1 class="reader-title">.*?</h1>)',
                    r'\1<span role="note" style="font-size:12px;color:#64748b">识别文本持续校对，请以 PDF 原文为准</span>',html,count=1,flags=re.S)
        response.set_data(html);return response
