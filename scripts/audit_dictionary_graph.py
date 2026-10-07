"""Audit every graph quotation and benchmark isolated read-only endpoints locally."""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dictionary_graph import Graph, readonly
from scripts.build_dictionary_graph import evidence, load_entries


def audit(directory,source):
    selected=json.loads((directory/'binding.json').read_text('utf-8'))
    graph=Graph(directory/'graph.sqlite',selected,source)
    entries={e['slug']:e for e in load_entries(source)}
    count=0
    with readonly(graph.path) as c:
        assert c.execute('SELECT count(*) FROM nodes').fetchone()[0]==len(entries)
        for row in c.execute('SELECT * FROM edges'):
            edge=graph.edge(row)
            assert edge['source'] in entries and edge['target'] in entries
            assert not entries[edge['source']]['needs_review'] and not entries[edge['target']]['needs_review']
            assert edge['evidence'],edge['id']
            for ev in edge['evidence']:
                assert ev['slug']==edge['source'],edge['id']
                assert evidence(entries[ev['slug']],ev['quote'])==ev,edge['id']
                count+=1
    return graph,count


def benchmark(graph):
    # This test app has no user databases, queue, paid model clients or network requests.
    from flask import Flask
    import dictionary_map_web
    dictionary_map_web.current_graph=lambda:graph
    app=Flask('isolated-dictionary-benchmark')
    app.add_url_rule('/dictionary/entry/<slug>',endpoint='dictionary_entry_page',view_func=lambda slug:slug)
    app.register_blueprint(dictionary_map_web.create_blueprint(lambda feature:None,lambda:{}))
    client=app.test_client()
    with readonly(graph.path) as c:
        hubs=[r[0] for r in c.execute('SELECT source FROM edges GROUP BY source ORDER BY count(*) DESC LIMIT 20')]
    results={}
    for kind in ('overview','neighbors','path'):
        times=[]
        for n in range(105):
            slug=hubs[n%len(hubs)]
            params={} if kind=='overview' else {'center':slug,'limit':59,'inference':1} if kind=='neighbors' else {'from':slug,'to':hubs[(n+7)%len(hubs)],'inference':1}
            route='/api/dictionary/path' if kind=='path' else '/api/dictionary/graph'
            start=time.perf_counter();r=client.get(route,query_string=params)
            elapsed=(time.perf_counter()-start)*1000
            assert r.status_code==200
            assert len(r.json['nodes'])<=60 and len(r.json['edges'])<=120
            if kind=='path':assert len(r.json['edges'])<=4
            if n>=5:times.append(elapsed)
        results[kind]={'samples':len(times),'median_ms':statistics.median(times),'p95_ms':sorted(times)[94],'max_ms':max(times)}
    assert all(v['p95_ms']<=500 for v in results.values()),results
    return results


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path,required=True)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    graph,count=audit(args.directory,args.source)
    value={'graph_id':graph.meta['id'],'quotation_checks':count,'quotation_failures':0,
           'environment':platform.platform(),'processor':platform.processor(),
           'benchmark_scope':'local Flask test client, warmed cache; no production load test',
           'endpoints':benchmark(graph),'artifact_bytes':graph.path.stat().st_size}
    args.output.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8')
    print(json.dumps(value,ensure_ascii=False))


if __name__=='__main__':main()
