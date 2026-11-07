"""New boundary scenarios; kept separate from already executed acceptance."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import ai_quote_variants as V
import ai_citation_runtime as R

SOURCE='交换的深度、广度和方式都是由生产的发展和结构决定的。'
NEAR=SOURCE.replace('方式','形式')
P={'index':1,'text':SOURCE,'quote_segments':[SOURCE],'source_file':'edition-a.pdf'}


def test_exhausted_budget_never_claims_unique_near_match():
    matcher=V.Matcher([P],seconds=-1)
    result=matcher.match(NEAR,[1])
    assert result['kind']=='pending_quote' and result['text_verified'] is False
    assert '预算' in result['verification_reason']


def test_located_variant_page_never_implies_verbatim_text():
    record=V.variants('“'+NEAR+'”[1]',[P])[0]
    app=SimpleNamespace(_payload_volume=lambda _:object(),
        _volume_page_evidence=lambda vol,page,literal,*args,**kwargs:{'pdf_page':4,'printed_page':'2','span':literal,'source_file':'edition-a.pdf','citation':'测试出处'},
        _review_evidence_context=lambda text,*args,**kwargs:text)
    passage={**P,'source_segments':[{'start':0,'end':len(SOURCE),'page_index':0,'pdf_page':4}]}
    card=R.card(app,{'pdf_pages':[4]},passage,[record],False,'问题')
    assert card['location_status']=='verified'
    assert card['evidence'][0]['kind']=='approximate_quote'
    assert card['evidence'][0]['text_verified'] is False
    assert card['evidence'][0]['answer_text']==NEAR


def test_cloud_request_preserves_variant_metadata_without_network():
    import personal_library_store as store
    ev={'kind':'approximate_quote','answer_text':NEAR,'source_text':SOURCE,'text_verified':False,'differences':[{'answer':'形','source':'方'}]}
    conversation={'messages':[{'role':'assistant','content':NEAR,'citations':[{'evidence':[ev]}]}]}
    with patch.object(store,'_request',return_value={'ok':True}) as request:
        store.put_conversation(1,'fixture-polish',conversation)
    body=json.loads(request.call_args.kwargs['body'])
    assert body['conversation']==conversation


def test_display_markers_respect_code_links_and_emphasis_boundaries():
    root=Path(__file__).resolve().parents[1]
    js=(root/'static/ai-page/quote-variants.js').read_text(encoding='utf-8')
    fixture=json.dumps({'text':NEAR,'cards':[{'grounding_index':1,'evidence':[{'kind':'approximate_quote','answer_text':NEAR}]}]},ensure_ascii=False)
    js+='\nconst f='+fixture+';\n'+r'''
const assert=require('node:assert/strict');
const formatted='“**'+f.text+'**”[1]';
const code='`'+f.text+'`';
const link='['+f.text+'](https://example.test)';
const fenced='```\n'+f.text+'\n```';
const text=[formatted,code,link,fenced].join('\n\n');
const out=AipQuoteVariants.mark(text,f.cards);
assert.equal((out.match(/近似引文/g)||[]).length,1);
assert(out.includes(code)&&out.includes(link)&&out.includes(fenced));
assert(out.includes('**”〔近似引文〕[1]'));
assert.equal(AipQuoteVariants.mark(out,f.cards),out);
'''
    result=subprocess.run(['node','-e',js],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stderr
