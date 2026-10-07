"""Real dictionary templates and graph routes, with no live application stores."""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, render_template, request
from werkzeug.serving import make_server

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import dictionary_map_web
from dictionary_graph import Graph, file_hash
from dictionary_store import dictionary_groups,dictionary_stats,dictionary_entry,dictionary_suggest

app=Flask(__name__,template_folder=str(ROOT/'templates'),static_folder=str(ROOT/'static'))
TEXT={'index.hero_title':'马克思主义理论研究辅助程序','dictionary.heading':'马克思主义大辞典',
      'dictionary.description':'逐条查阅释义，沿原文探索概念联系。','dictionary.search_label':'检索词条',
      'dictionary.search_placeholder':'输入词条名称','dictionary.search_hint':'搜索词条并查看原书页码',
      'citation.nav_label':'AI论文校注','citation.nav_mobile_label':'校注'}


def context():
    return dict(layout_v2=True,layout_page='dictionary',app_name='马克思主义理论研究辅助程序',
                app_version_display='本地验收',site_text=lambda key:TEXT.get(key,key),
                current_user=None if request.args.get('guest') else SimpleNamespace(display_name='地图验收用户',email='fixture@example.test'),
                citation_assistant_available=True,sponsor_enabled=False,csrf_token='fixture',account_center_label='会员中心')


@app.get('/dictionary')
def dictionary():
    return render_template('dictionary.html',**context(),groups=dictionary_groups(),stats=dictionary_stats(),dictionary_map_ready=True)


@app.get('/dictionary/entry/<path:slug>')
def dictionary_entry_page(slug):
    return render_template('dictionary_entry.html',**context(),entry=dictionary_entry(slug),dictionary_map_ready=True,related_dictionary=[])


@app.get('/api/dictionary/suggest')
def suggest():
    return {'ok':True,'results':[dict(row,url='/dictionary/entry/'+row['slug']) for row in dictionary_suggest(request.args.get('q',''))]}


def fake_graph():
    folder=Path(tempfile.mkdtemp(prefix='dictionary-browser-'))
    import atexit,shutil
    atexit.register(lambda:shutil.rmtree(folder,ignore_errors=True))
    source=ROOT/'data/dictionary.sqlite'
    database=folder/'graph.sqlite'
    c=sqlite3.connect(database)
    c.executescript('''CREATE TABLE nodes(slug TEXT PRIMARY KEY,title TEXT,kind TEXT,theme TEXT,start_page INTEGER,end_page INTEGER,needs_review INTEGER);
    CREATE TABLE edges(id TEXT PRIMARY KEY,source TEXT,target TEXT,kind TEXT,layer TEXT,explanation TEXT,evidence_json TEXT,model TEXT,review TEXT);
    CREATE TABLE metadata(key TEXT,value TEXT); CREATE INDEX source_idx ON edges(source); CREATE INDEX target_idx ON edges(target);''')
    capital=dictionary_suggest('资本')[0]
    for i in range(75):
        slug=capital['slug'] if i==0 else 'fixture-'+str(i)
        title='资本' if i==0 else '关联概念 '+str(i)
        if i==2:
            title='《在省部级主要领导干部学习贯彻党的十八届四中全会精神全面推进依法治国专题研讨班上的讲话》'
        c.execute('INSERT INTO nodes VALUES (?,?,?,?,?,?,?)',(slug,title,'concept','政治经济学',121,122,0))
        if i and i != 74:
            ev=[{'slug':capital['slug'],'paragraph':1,'quote':'在资本的词条中理解这些联系。','citation':'测试来源，第121–122页','start_page':121,'end_page':122}]
            c.execute('INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?)',(str(i),capital['slug'],slug,'related' if i==1 else 'mention','inference' if i==1 else 'evidence','测试关系',json.dumps(ev),'fixture','fixture'))
    c.execute('INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?)',('reverse', 'fixture-1', capital['slug'], 'related', 'inference', '反向测试关系', json.dumps(ev), 'fixture', 'fixture'))
    c.execute('INSERT INTO nodes VALUES (?,?,?,?,?,?,?)',('fixture-small','8条关系词条','concept','政治经济学',120,120,0))
    for i in range(3,11):
        source_slug,target_slug=('fixture-small','fixture-'+str(i)) if i<7 else ('fixture-'+str(i),'fixture-small')
        c.execute('INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?)',('small-'+str(i),source_slug,target_slug,'mention','evidence','小规模双向图测试',json.dumps(ev),'fixture','fixture'))
    meta={'id':'fixture-v1','schema_version':1,'source_sha256':file_hash(source),'coverage':{'entries':76,'analyzed_entries':76,'relations':82,'inferred_relations':2}}
    c.execute('INSERT INTO metadata VALUES (?,?)',('manifest',json.dumps(meta)))
    c.commit();c.close()
    return Graph(database,dict(id='fixture-v1',sha256=file_hash(database),source_sha256=file_hash(source)),source)


artifact=os.environ.get('DICTIONARY_GRAPH_FIXTURE')
graph=Graph(Path(artifact)/'graph.sqlite',json.loads((Path(artifact)/'binding.json').read_text('utf-8')),ROOT/'data/dictionary.sqlite') if artifact else fake_graph()
dictionary_map_web.current_graph=lambda:None if request.args.get('unavailable') else graph
app.register_blueprint(dictionary_map_web.create_blueprint(lambda _:None,context))
for endpoint,path in {'index':'/','layout_search':'/v2','layout_read':'/v2/read','layout_ai':'/v2/ai',
                      'layout_more':'/v2/more','citation_agent_page':'/citation-agent','research_updates.latest':'/research/latest',
                      'pricing':'/pricing','account':'/account','login':'/login','register':'/register','logout':'/logout'}.items():
    app.add_url_rule(path,endpoint,lambda:'<p>本地验收页面</p>')

if __name__=='__main__':
    server=make_server('127.0.0.1',int(os.environ.get('DICTIONARY_FIXTURE_PORT','0')),app,threaded=True)
    print('READY=http://127.0.0.1:'+str(server.server_port),flush=True)
    server.serve_forever()
