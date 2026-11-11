"""Small real Corpus with nested works, same-page siblings and repeated titles."""
from build_index import normalize
from search import Corpus, Volume, Page, TocEntry


def make_corpus():
    corpus = Corpus()
    raw = ('外部前文不得泄漏。第一作品\n劳动创造社会财富。劳动创造社会财富。'
           '\n内部子节\n劳动创造社会财富。\n第二作品\n边界外部秘密。劳动影响生产关系。')
    books, toc, by_source = {}, {}, {}
    for book, number, text in [('文集', 1, raw), ('文集', 2, raw), ('全集', 1, raw)]:
        source = f'pdfs/fixture/{book}-{number}.pdf'
        vol = Volume.build(book, number, source, f'{book}第{number}卷',
                           [Page(1, '10', text, normalize(text), 100 + number)])
        books.setdefault(book, []).append(vol)
        by_source[source] = vol
        toc[source] = [TocEntry('第一作品', 1, sort_order=1),
                       TocEntry('内部子节', 1, level=2, sort_order=2),
                       TocEntry('第二作品', 1, sort_order=3)]
    corpus.books = books
    corpus._volumes_by_source_file = by_source
    corpus.get_toc_entries = lambda source, *args, **kwargs: toc.get(source, [])
    corpus._layout_scan = lambda *args, **kwargs: ({}, True, '')
    return corpus, toc
