"""Freeze only citation configuration for an executor retained for older jobs."""
import json
import re
from pathlib import Path


def install(app_module, path):
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    if value.get('schema')!=1 or not all(re.fullmatch('[a-f0-9]{64}',str(value.get(k,'')))
                                      for k in ['corpus_sha256','template_version']):
        raise ValueError('Invalid pinned executor identity')
    if value['corpus_sha256']!=app_module._citation_corpus_sha256():
        raise ValueError('Pinned executor corpus does not match its frozen release')
    formats=value.get('citation_formats');approved=value.get('gb2025_approved')
    if not isinstance(formats,dict) or not isinstance(approved,bool):
        raise ValueError('Invalid pinned citation configuration')
    if not all(k in app_module._CITATION_FORMAT_KEYS and isinstance(v,str)
               and 0<len(v)<=app_module._CITATION_TEMPLATE_MAXLEN for k,v in formats.items()):
        raise ValueError('Invalid pinned citation format')
    previous=(app_module._load_citation_formats,app_module._gb2025_template_approved)
    app_module._load_citation_formats=lambda:dict(formats)
    app_module._gb2025_template_approved=lambda:approved
    try:
        if app_module._citation_template_version()!=value['template_version']:
            raise ValueError('Pinned template does not match this executor code')
        app_module.corpus.set_citation_templates(dict(formats))
    except BaseException:
        app_module._load_citation_formats,app_module._gb2025_template_approved=previous
        raise
    return dict(corpus_sha256=value['corpus_sha256'],template_version=value['template_version'])
