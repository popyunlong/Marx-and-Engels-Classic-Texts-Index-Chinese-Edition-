"""Replay fixed research questions against real evidence, preserving raw answers.

Uses existing local credentials without copying them. No application databases,
user conversations, or production routing are changed. Reports live API output
and before/after deterministic citation repair for human quality review.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ai
import ai_citations
from ai_evidence import clean_evidence


def prose(text):
    text = ai_citations.REF.sub('', text)
    return ''.join(c for c in text if c.isalnum())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--ai-config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-ref', default='origin/production')
    args = parser.parse_args()
    config = replace(ai.load_ai_config(config_path=args.ai_config), provider='deepseek',
                     base_url='https://api.deepseek.com', model='deepseek-v4-flash')
    if not config.api_key:
        raise RuntimeError('Existing DeepSeek credentials are required')
    client = ai.ZAIClient(config)
    conn = sqlite3.connect(args.corpus.resolve().as_uri() + '?mode=ro', uri=True)
    rows = conn.execute("SELECT source_file,pdf_page,printed_page,raw_text FROM pages WHERE book='文集' AND volume=8 AND pdf_page BETWEEN 35 AND 38 ORDER BY pdf_page").fetchall()
    passages = []
    for index, (source_file, page, printed, raw) in enumerate(rows, 1):
        clean = clean_evidence(raw)
        passages.append({'index': index, 'text': clean.text, 'quote_segments': list(clean.quote_segments),
            'citation': f'《马克思恩格斯文集》第8卷，第{printed}页。', 'source_file': source_file,
            'work_title': '《政治经济学批判》导言', 'work_authors': ['马克思'], 'provenance_verified': True,
            'pdf_page': page, 'printed_page': printed})
    conn.close()
    baseline = types.ModuleType('baseline_ai_citations')
    exec(compile(subprocess.check_output(['git', 'show', args.baseline_ref + ':ai_citations.py'], cwd=ROOT).decode('utf-8'), 'baseline_ai_citations.py', 'exec'), baseline.__dict__)
    cases = [
        ('production-exchange', '结合所给原文，分析生产怎样规定交换的深度、广度和方式，并区分生产的决定作用与各环节的相互作用。', 'off'),
        ('production-consumption', '论述生产、分配、交换和消费的总体关系，说明为什么消费与交换不能脱离生产独立理解。', 'off'),
        ('research-thinking', '分析生产对其他环节的支配作用与分配、交换、消费的反作用如何统一，避免把它理解为单向机械因果。', 'high'),
    ]
    report = {'baseline_ref': args.baseline_ref, 'wire_model': 'deepseek-flash', 'cases': [], 'passages': passages}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for name, question, effort in cases:
        started = time.monotonic()
        raw = client.generate_research_review(question, passages, model='deepseek-flash', reasoning_effort=effort)
        generated_seconds = time.monotonic() - started
        # Baseline and fix see exactly the same generated answer and evidence.
        before, before_issues = baseline.repair_missing_references(raw, passages)
        started = time.monotonic()
        after, after_issues = ai_citations.repair_missing_references(raw, passages)
        details = ai_citations.ledger(after, passages)
        row = {'id': name, 'question': question, 'effort': effort, 'raw': raw, 'before': before, 'after': after,
               'generation_seconds': round(generated_seconds, 3), 'repair_seconds': round(time.monotonic()-started, 3),
               'argument_preserved': prose(raw) == prose(after), 'before_issues': before_issues,
               'after_issues': after_issues, 'citation_stats': details['citation_stats']}
        report['cases'].append(row)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in row.items() if k not in {'raw','before','after','question'}}, ensure_ascii=False), flush=True)
        if not row['argument_preserved'] or not details['citation_stats']['direct_quotes']:
            raise RuntimeError('Live research output needs manual quality review')
    print('LIVE_RESEARCH_GATE_PASS', flush=True)


if __name__ == '__main__':
    main()
