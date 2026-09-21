from __future__ import annotations

import json
import statistics
from collections import Counter


def report_data(store):
    report = store.snapshot()
    with store.connect() as c:
        calls = [dict(r) for r in c.execute("SELECT provider,usage,cost,reserved,seconds,status FROM calls")]
        free_rows=[dict(r) for r in c.execute('SELECT status,owner,result,error FROM free_ocr')]
        details = []
        for b in report["books"]:
            rows = [dict(r) for r in c.execute("SELECT page,stage,pilot,kind,label,checked,repairs,error FROM pages WHERE book=? ORDER BY page", (b["id"],))]
            detail = {"id": b["id"], "name": b["name"], "sha256": b["sha"], "pages": b["pages"],
                            "pilot_pages": [r for r in rows if r["pilot"]],
                            "reviewed_pages": [r["page"] for r in rows if r["checked"]],
                            "unresolved": [r for r in rows if r["stage"] == "review"],
                            "metadata": json.loads(b["metadata"]), "toc": json.loads(b["toc"])}
            # Keep original recognition history distinct from the reviewed
            # version; a provisional publication never upgrades checked flags.
            for path in [store.root / (b['id']+'.json'), store.root/'packages'/b['id']/'book.json']:
                try:
                    package=json.loads(path.read_text(encoding='utf-8'))
                except (OSError,ValueError):
                    continue
                if package.get('source_sha256')!=b['sha'] or package.get('book_id')!=b['id']:
                    continue
                detail['release_reference']={
                    'status':'published' if b['status']=='published' else 'prepared',
                    'metadata':package['metadata'],'toc':package['toc'],
                    'quality':package.get('release_quality','reviewed'),
                    'page_map':[{'pdf_page':p['page'],'printed_page':p['label'],'kind':p['kind'],
                                 'basis':p.get('mapping_basis','reviewed')} for p in package['pages']]}
                publication=report.get('publication',{})
                if b['id'] in publication.get('book_ids',[]) and publication.get('phase') in ('observing','committing'):
                    detail['release_reference']['status']=publication['phase']
                break
            details.append(detail)
    providers = {}
    for provider in ("glm", "mimo", "verify"):
        rows = [r for r in calls if r["provider"] == provider]
        times = [r["seconds"] for r in rows if r["seconds"] is not None]
        providers[provider] = {"calls": len(rows), "median_seconds": statistics.median(times) if times else None,
                               "estimated_yuan": sum(r["cost"] or 0 for r in rows),
                               "uncertain_reserved_yuan": sum(r["reserved"] for r in rows if r["cost"] is None)}
        providers[provider]['missing_usage_responses']=sum(r['status']=='complete' and r['cost'] is None for r in rows)
        providers[provider]['uncertain_requests']=sum(r['status']!='complete' and r['cost'] is None for r in rows)
    free_stats={'counts':dict(Counter(r['status'] for r in free_rows)),
                'errors':dict(Counter(r['error'] for r in free_rows if r['error'])),
                'executors':{},'api_cost_yuan':0}
    for executor in ('windows-local','server'):
        completed=[json.loads(r['result']) for r in free_rows if r['status']=='done'
                   and (r['owner'].startswith('local-'))==(executor=='windows-local')]
        times=[r.get('end_to_end_seconds',r.get('unit_seconds')) for r in completed]
        times=[v for v in times if isinstance(v,(int,float))]
        free_stats['executors'][executor]={'completed_pages':len(completed),
            'median_unit_seconds':statistics.median(times) if times else None}
    total = sum(b["pages"] for b in report["books"])
    pilot_done = sum(b["pilot_done"] for b in report["books"])
    # Full run review load changes with disagreements. Show a range, never a promise.
    mean_mimo = [r["cost"] for r in calls if r["provider"] in {"mimo", "verify"} and r["cost"] is not None]
    mean = statistics.mean(mean_mimo) if mean_mimo else None
    report.update(details=details, provider_stats=providers, pilot_done=pilot_done, total_pages=total,
                  free_ocr_stats=free_stats,
                  full_review_cost_range_yuan=[round(total * .1 * mean, 2), round(total * 3 * mean, 2)] if mean else None,
                  pricing_note="按服务配置费率估算，非账户账单；未知结果保留调用预留，核验升级会增加费用。",
                  accuracy_note="MiMo 抽检与多次读取不能证明全文零错误；明确列出已核验页和未决问题。")
    return report
