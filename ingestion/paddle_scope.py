"""Keep bound works separate during research retrieval and passage expansion."""
from contextvars import ContextVar
from functools import wraps


class WorkScope(dict):
    def __init__(self, books, authors):
        super().__init__(books if isinstance(books, dict) else {b: None for b in books})
        self.authors = frozenset(authors)


def install(app):
    from .paddle_runtime import bibliography
    corpus = app.corpus
    metadata = bibliography()
    bound = {book: volumes for book, volumes in metadata.items()
             if any(v.get('work_ranges') for v in volumes.values())}
    if not bound or getattr(corpus, '_paddle_scope_installed', False):
        return
    corpus._paddle_scope_installed = True
    passage_part = ContextVar('bound_work_passage', default=None)
    get_volume = corpus.get_volume_by_source_file
    scoped_volumes = corpus._scoped_volumes
    chapter_segments = corpus._chapter_segments
    resolve_scope = app._resolve_search_scope
    cache = {}

    def parts(volume):
        volumes = bound.get(volume.book, {})
        return volumes.get(volume.volume, volumes.get(str(volume.volume), {})).get('work_ranges', [])

    def segment(volume, part):
        key = (volume.source_file, part['start'], part['end'])
        if key not in cache:
            pages = [p for p in volume.pages if part['start'] <= p.pdf_page <= part['end']]
            selected = type(volume).build(volume.book, volume.volume, volume.source_file, part['title'], pages)
            selected._paddle_work_segment = True
            cache[key] = selected
        return cache[key]

    def selected_volumes(book, book_scope=None):
        result = []
        for volume in scoped_volumes(book, book_scope):
            ranges = parts(volume)
            if not ranges:
                result.append(volume)
                continue
            authors = getattr(book_scope, 'authors', None)
            for part in ranges:
                if authors is None or authors.intersection(part['authors']):
                    result.append(segment(volume, part))
        return result

    def segments(volume):
        if not getattr(volume, '_paddle_work_segment', False):
            return chapter_segments(volume)
        # The legacy segment cache is keyed only by physical PDF. Never store
        # a partial work there or reuse offsets from another part of that PDF.
        if not hasattr(volume, '_paddle_segments'):
            volume._paddle_segments = corpus._build_chapter_segments(volume)
        return volume._paddle_segments

    def volume_for_passage(source_file):
        volume = get_volume(source_file)
        active = passage_part.get()
        if volume is not None and active and volume.source_file == active[0]:
            return segment(volume, active[1])
        return volume

    def scoped(raw_scope, gist, plan):
        books, scope_id, manual = resolve_scope(raw_scope, gist, plan)
        tokens = scope_id.split(',')
        # Explicit book/volume selection keeps the user's whole-book choice.
        # An author collection restricts mixed physical books to that author.
        if books is not None and 'mill_works' in tokens and not any(t.startswith(('book:', 'vol:')) for t in tokens):
            authors = set()
            for group in app.CORPUS_SCOPES:
                if group['id'] not in tokens:
                    continue
                for book in group['books']:
                    if book not in bound:
                        authors.update(corpus.get_book_config(book).authors)
            books = WorkScope(books, authors)
        return books, scope_id, manual

    def restrict_passage(function):
        @wraps(function)
        def call(hit, payload, *args, **kwargs):
            volume = get_volume(payload.get('source_file', ''))
            pages = [int(p) for p in payload.get('pdf_pages', [])]
            part = next((r for r in parts(volume) if pages and all(r['start'] <= p <= r['end'] for p in pages)), None) if volume else None
            token = passage_part.set((volume.source_file, part) if part else None)
            try:
                return function(hit, payload, *args, **kwargs)
            finally:
                passage_part.reset(token)
        return call

    corpus._scoped_volumes = selected_volumes
    corpus._chapter_segments = segments
    corpus.get_volume_by_source_file = volume_for_passage
    app._resolve_search_scope = scoped
    for name in ('_chat_grounding_source_text', '_research_review_passage_text'):
        setattr(app, name, restrict_passage(getattr(app, name)))
    # Citation verification can search the whole physical volume to retarget a
    # quoted sentence. Keep that verification inside the originating work too.
    def restrict_evidence(function, is_payload):
        @wraps(function)
        def call(value, *args, **kwargs):
            volume = function(value, *args, **kwargs)
            numbers = value.get('pdf_pages', []) if is_payload else [p.pdf_page for p in value.pages]
            numbers = [int(p) for p in numbers]
            part = next((r for r in parts(volume) if numbers and all(r['start'] <= p <= r['end'] for p in numbers)), None) if volume else None
            return segment(volume, part) if part else volume
        return call
    for name, is_payload in (('_payload_volume', True), ('_hit_volume', False)):
        if hasattr(app, name):
            setattr(app, name, restrict_evidence(getattr(app, name), is_payload))
