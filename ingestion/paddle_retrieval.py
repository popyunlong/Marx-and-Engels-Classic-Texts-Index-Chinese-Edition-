"""Ground new economics scopes in concepts actually present in the question.

Model-generated book titles must not crowd out the question's literal subject.
This bounded vocabulary supplements the existing general retrieval engine; it
neither adds text to sources nor changes the website's model configuration.
"""
TERMS = ('使用价值','交换价值','劳动生产力','劳动生产率','生产资料','生产要素','政治经济学',
         '所有权','代议制','功利主义','分工','劳动','生产力','生产率','黄金','纸币','货币','价格',
         '价值','自由','妇女','平等','权利','财产','工资','利润','地租','税收','利息','资本',
         '生产','分配','交换','效用','功利','幸福','道德','正义','政府','民主','教育','贸易',
         '市场','银行','信用','垄断','贫困','贫穷','财富','竞争','利己','同情')


def topic_terms(question):
    matches=[t for t in TERMS if t in question]
    return tuple(t for t in matches if not any(t!=other and t in other for other in matches))[:6]


class TopicScope(dict):
    pass


def install(app, bibliography):
    resolve=app._resolve_search_scope
    locate=app.corpus.locate_associative
    corpus=app.corpus
    def scope(raw,gist,plan):
        books,sid,manual=resolve(raw,gist,plan)
        terms=topic_terms(gist)
        if books and set(books).issubset(bibliography()) and len(terms)>=2:
            if not hasattr(books,'__dict__'):
                books=TopicScope(books if isinstance(books,dict) else {b:None for b in books})
            books.literal_terms=terms
        return books,sid,manual
    def anchored(*args,**kwargs):
        books=kwargs.get('book_scope')
        terms=getattr(books,'literal_terms',())
        hits=locate(*args,**kwargs)
        if len(terms)<2:return hits
        direct=corpus.keyword_cooccurrence(list(terms),window=320,min_distinct=2,book_scope=books)
        if not direct:return hits
        # At least one real nearby co-occurrence exists. Prefer those passages
        # and omit title-only/promotional matches with no topical support.
        def support(hit):
            text=''.join(p.raw_text for p in hit.pages)
            return sum(term in text for term in terms)
        direct.sort(key=lambda h:(-support(h),-h.score))
        result=[];seen=set()
        for hit in [*direct,*[h for h in hits if support(h)>=2]]:
            key=(hit.source_file,tuple(p.pdf_page for p in hit.pages))
            if key not in seen:
                seen.add(key);result.append(hit)
        return result[:kwargs.get('candidate_cap',300)]
    app._resolve_search_scope=scope
    corpus.locate_associative=anchored
