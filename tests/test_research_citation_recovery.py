"""Reported research quotation, exact-only recovery and immutable history."""
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import ai_citations as C
import ai_citation_runtime as R
from ai import ZAIClient
from ai_evidence import exact_quote


# Visually checked against Wenji 8, PDF page 37 / printed page 23, 2026-10-02.
# The printed source includes (1), (2), (3); the reported answer omitted them.
CLAUSES = [
    '如果没有分工，不论这种分工是自然发生的或者本身已经是历史的结果，也就没有交换；',
    '私人交换以私人生产为前提；',
    '交换的深度、广度和方式都是由生产的发展和结构决定的。',
]
ANSWER = ''.join(CLAUSES)
SOURCE = '但是，' + ''.join(f'（{i}）{s}' for i, s in enumerate(CLAUSES, 1))
PASSAGE = {'index': 1, 'text': SOURCE, 'quote_segments': [SOURCE],
           'source_file': 'pdfs/文集/马克思恩格斯文集[第8卷]马克思《资本论》手稿选编.pdf',
           'pdf_page': 37, 'printed_page': '23'}


def test_reported_numbered_original_is_recovered_without_changing_argument():
    analysis = '这里揭示了交换对生产的多重依赖，论述应完整保留。'
    result = ZAIClient.repair_grounded_answer(ANSWER + '\n\n' + analysis, [PASSAGE])
    assert result['status'] == 'repaired'
    assert result['used_indices'] == [1]
    assert analysis in result['answer_markdown']
    records = result['citation_records']
    quotes = [q for r in records for q in r['quotes']]
    assert all(any(exact_quote(clause, quote) for quote in quotes) for clause in CLAUSES)
    assert result['citation_stats']['direct_quotes'] == 3
    assert C.repair_missing_references(result['answer_markdown'], [PASSAGE])[0] == result['answer_markdown']


@pytest.mark.parametrize('prefix', ['（３）', '(3)', '但是，（3）', '但是，(3)', '但是，'])
def test_source_clause_boundaries(prefix):
    text = CLAUSES[-1]
    repaired, _ = C.repair_missing_references(text, [{'index': 1, 'text': prefix + text}])
    assert repaired == f'“{text}”[1]'


def test_parenthetical_aside_is_not_a_new_source_clause():
    text = CLAUSES[-1]
    repaired, _ = C.repair_missing_references(text, [{'index': 1, 'text': '这种关系（包含交换）' + text}])
    assert repaired == text


def test_duplicate_windows_and_editions_keep_local_reference_priority():
    text = CLAUSES[-1]
    sources = [PASSAGE, {**PASSAGE, 'index': 2}, {**PASSAGE, 'index': 3, 'source_file': 'other-edition.pdf'}]
    assert C.repair_missing_references(text, sources)[0] == f'“{text}”[1]'
    assert C.repair_missing_references(text + '[3]', sources)[0] == f'“{text}”[3]'
    assert sources[2]['source_file'] == 'other-edition.pdf'


def test_internal_emphasis_does_not_hide_original():
    text = CLAUSES[-1].replace('广度', '**广度**')
    result = ZAIClient.repair_grounded_answer(text, [PASSAGE])
    assert result['used_indices'] == [1]
    assert result['citation_stats']['direct_quotes'] == 1
    assert '广度' in result['answer_markdown']


@pytest.mark.parametrize('text', [
    '交换的深度、广度和形式都是由生产的发展和结构决定的。',
    '交换的深度……都是由生产的发展和结构决定的。',
    '生产关系', '`' + CLAUSES[-1] + '`',
    '```\n' + CLAUSES[-1] + '\n```',
    '[' + CLAUSES[-1] + '](https://example.test)',
])
def test_changed_words_ellipsis_terms_and_literals_are_not_promoted(text):
    assert C.repair_missing_references(text, [PASSAGE])[0] == text


def test_unsafe_ocr_barrier_is_not_bridged():
    text = CLAUSES[-1]
    p = {'index': 1, 'text': text, 'quote_segments': [text[:12], text[12:]]}
    assert C.repair_missing_references(text, [p])[0] == text


def test_nested_quotes_and_physical_newline_stay_continuous():
    text = '这段原文把“生产关系”作为分析社会生活的一个重要规定。'
    p = {'index': 1, 'text': text[:15] + '\n' + text[15:]}
    repaired, _ = C.repair_missing_references(text, [p])
    assert '[1]' in repaired
    assert C.ledger(repaired, [p])['citation_stats']['direct_quotes'] >= 1


def recovery_app(text=CLAUSES[-1]):
    vol = SimpleNamespace(source_file='allowed.pdf', norm_full=text)
    hit = SimpleNamespace(to_dict=lambda: {'source_file': 'allowed.pdf'})
    calls = []
    corpus = SimpleNamespace(
        _scoped_book_keys=lambda books: list(books),
        _scoped_volumes=lambda book, books: [vol],
        _make_hit=lambda *args: (calls.append(args), hit)[1],
        enrich_hit_document=lambda hit: hit,
    )
    A = SimpleNamespace(corpus=corpus, normalize=lambda s: s,
                        _research_review_passage_text=lambda *args, **kwargs: text)
    return A, calls


def test_recovery_respects_scope_cancel_and_deadline(monkeypatch):
    A, calls = recovery_app()
    args = dict(question='交换与生产', allowed_books={'文集'})
    monkeypatch.setattr(R, 'passage', lambda A, h, b, t, i: {'index': i, 'text': t, 'source_segments': [{'pdf_page': 1}]})
    passages, bases = [], {}
    assert R.recover_answer_sources(A, CLAUSES[-1], passages, bases, **args) == 1
    assert bases[1]['source_file'] == 'allowed.pdf'
    assert len(calls) == 1
    assert R.recover_answer_sources(A, CLAUSES[-1], [], {}, cancelled=lambda: True, **args) == 0
    assert R.recover_answer_sources(A, CLAUSES[-1], [], {}, document_scopes=[SimpleNamespace(source_file='denied.pdf')], **args) == 0
    ticks = iter([0, 6])
    monkeypatch.setattr(R.time, 'monotonic', lambda: next(ticks))
    assert R.recover_answer_sources(A, CLAUSES[-1], [], {}, **args) == 0
    assert len(calls) == 1


def test_recovery_checks_at_most_eight_fragments(monkeypatch):
    A, calls = recovery_app(''.join(CLAUSES[-1] + str(i) + '；' for i in range(12)))
    probes = '\n'.join(CLAUSES[-1][:-1] + str(i) + '。' for i in range(12))
    seen = []
    A.normalize = lambda s: (seen.append(s), s)[1]
    assert R.recover_answer_sources(A, probes, [], {}, question='', allowed_books={'文集'}) == 0
    assert len(seen) == 8


def test_saved_history_display_and_export_projection():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed for frontend behavior tests')
    source = (Path(__file__).resolve().parents[1] / 'static/ai-page/ai-page.js').read_text(encoding='utf-8')
    functions = source[source.index('  function quoteDisplayMap('):source.index('  function renderAnswerMarkdown(')]
    fixture = {'content': ANSWER, 'citations': [{'review_index': 7, 'evidence': [
        {'kind': 'quote', 'quote': q, 'text_verified': True} for q in CLAUSES]}]}
    js = functions + '\nconst message = ' + json.dumps(fixture, ensure_ascii=False) + r''';
const assert = require('node:assert/strict');
const original = JSON.stringify(message);
const result = quoteDisplayContent(message);
assert.equal((result.match(/\[7\]/g)||[]).length, 3);
assert.equal(JSON.stringify(message), original);
assert.equal(quoteDisplayContent({...message,content:result}), result);
assert.equal(quoteDisplayContent({...message,citations:[]}), message.content);
const code = '```\n'+message.content+'\n```';
assert.equal(quoteDisplayContent({...message,content:code}), code);
const altered = message.content.replace('广度','宽度');
assert(!quoteDisplayContent({...message,content:altered}).includes('“交换的深度、宽度'));
const clause = message.citations[0].evidence[2].quote;
for (const text of ['“**'+clause+'**”[7]', '**'+clause+'**', clause.replace('广度','广度\n')]) {
  const once = quoteDisplayContent({...message,content:text});
  assert.equal((once.match(/\[7\]/g)||[]).length, 1);
  assert.equal(quoteDisplayContent({...message,content:once}), once);
}
const alreadyCited = '“'+clause+'”[2,7]';
assert.equal(quoteDisplayContent({...message,content:alreadyCited}), alreadyCited);
console.log('history-display-ok');
'''
    result = subprocess.run([node, '-e', js], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_visually_checked_corpus_page_recovers_reported_original():
    fixture = json.loads((Path(__file__).parent / 'fixtures/research-numbered-wenji8.json').read_text(encoding='utf-8'))
    passage = {**fixture, 'index': 1, 'text': fixture['raw_text']}
    result = ZAIClient.repair_grounded_answer(ANSWER, [passage])
    assert result['citation_stats']['direct_quotes'] == 3
    assert result['used_indices'] == [1]
    assert fixture['pdf_page'] == 37 and fixture['printed_page'] == '23'
