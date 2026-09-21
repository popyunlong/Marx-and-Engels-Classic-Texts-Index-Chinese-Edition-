"""Attach a real source link and explicit quote-check results to new-book AI reading."""
import json
import re


def finalize(payload, context):
    if not payload.get('ok'):
        return payload
    result=dict(payload)
    local=dict(citation=context['citation'],context=context['current_text'][:320],
               viewer_url=context['source_url'],source_kind='pdf_page',
               book=context['book'],volume=context['volume'],source_file=context['source_file'],
               pdf_pages=[context['page']],citations=context['citations'])
    result['citations']=[local]+[c for c in payload.get('citations',[]) if c.get('source_kind')!='pdf_page']
    compact=lambda s:re.sub(r'\s+','',s)
    text='\n'.join(context.get(k,'') for k in ('previous_excerpt','current_text','next_excerpt'))
    quotes=[m.group(1) or m.group(2) for m in re.finditer(r'“([^“”]+)”|「([^「」]+)」',payload.get('answer_markdown',''))]
    checked=[q for q in quotes if len(compact(q))>=12]
    unverified=[q for q in checked if compact(q) not in compact(text)]
    result['local_quote_check']={'checked':len(checked),'unverified':len(unverified),
                                 'basis':'current_page_and_supplied_neighbor_excerpts'}
    if unverified:
        result['warnings']=list(payload.get('warnings',[]))+['回答中有引号内容未能与所给原文逐字核准，请通过来源链接核对后再引用。']
    return result


def install(app, bibliography):
    from flask import request
    @app.app.after_request
    def reader_sources(response):
        if request.path not in ('/api/ai/pdf-chat','/api/ai/pdf-chat-stream') or response.status_code!=200:
            return response
        body=request.get_json(silent=True) or {}
        if body.get('personal_submission_id'):return response
        source=app._normalize_source_file(str(body.get('source_file') or ''))
        volume=app.corpus.get_volume_by_source_file(source)
        if volume is None or volume.book not in bibliography():return response
        context=app._get_page_context_payload(source,max(1,int(body.get('page') or 1)))
        if response.mimetype=='application/json':
            response.set_data(json.dumps(finalize(response.get_json(),context),ensure_ascii=False))
        elif response.mimetype=='text/event-stream':
            original=response.response
            def stream():
                try:
                    for chunk in original:
                        text=chunk.decode('utf-8') if isinstance(chunk,bytes) else chunk
                        if text.startswith('event: done\ndata: '):
                            payload=json.loads(text[len('event: done\ndata: '):].strip())
                            yield app._sse_event('done',finalize(payload,context))
                        else:yield chunk
                finally:
                    close=getattr(original,'close',None)
                    if close:close()
            response.response=stream()
        return response
