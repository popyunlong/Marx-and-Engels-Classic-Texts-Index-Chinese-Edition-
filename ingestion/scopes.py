"""Register configured ingested books in existing research scope groups."""


def install(app_module):
    from .economics_catalog import install_scopes
    install_scopes(app_module)
    history='marxism_history'
    app_module._COLLECTION_LABELS[history]='马克思主义发展史'
    app_module._COLLECTION_DESCRIPTIONS[history]='马克思主义发展与经济学说史著作 · 按原书目录阅读、检索原文并生成引文'
    history_books=tuple(b.key for b in app_module.BOOK_CONFIGS
        if b.collection==history and b.available and b.key in app_module.corpus.books)
    if history_books:
        group=next((g for g in app_module.CORPUS_SCOPES if g['id']==history),None)
        if group is None:
            app_module.CORPUS_SCOPES=(*app_module.CORPUS_SCOPES,
                dict(id=history,label='马克思主义发展史',books=history_books,
                     hints=('马克思主义发展史','马克思主义史','马克思主义经济学说史','庄福龄','顾海良')))
        else:group['books']=history_books
    western=[b for b in app_module.BOOK_CONFIGS if b.collection=='western_marxism'
             and b.available and b.key in app_module.corpus.books]
    app_module._COLLECTION_DESCRIPTIONS['western_marxism']=(
        f'西方马克思主义经典著作 · {len(western)} 个书目独立编目，可按目录阅读、检索原文并生成规范引文')
    for group in app_module.CORPUS_SCOPES:
        if group['id']=='western_marxism':
            group['books']=tuple(b.key for b in western)
            group['hints']=tuple(dict.fromkeys((*group['hints'],'马尔库塞','弗洛姆','霍耐特','霍克海默','本雅明')))
    # 用户荐书是独立专题，不归入经济学旧批次或「其他入库文献」。公开窗口由
    # app._book_is_public 动态判定；这里仅登记稳定的专题结构与阅读页顺序。
    collection = 'user_recommended'
    label = '用户荐书'
    if hasattr(app_module, '_COLLECTION_LABELS'):
        app_module._COLLECTION_LABELS[collection] = label
    if hasattr(app_module, '_COLLECTION_LIBRARY_SORT_ORDERS'):
        app_module._COLLECTION_LIBRARY_SORT_ORDERS[collection] = 3
    if hasattr(app_module, '_COLLECTION_DESCRIPTIONS'):
        app_module._COLLECTION_DESCRIPTIONS[collection] = (
            '读者推荐并获授权公开的著作 · 可按目录阅读、检索原文并使用 AI 导读；识别文本持续校对'
        )
    additions = tuple(
        book.key for book in app_module.BOOK_CONFIGS
        if book.collection == collection and book.available and book.key in app_module.corpus.books
    )
    if additions:
        group = next((g for g in app_module.CORPUS_SCOPES if g['id'] == collection), None)
        if group is None:
            app_module.CORPUS_SCOPES = (
                *app_module.CORPUS_SCOPES,
                {'id': collection, 'label': label, 'books': additions,
                 'hints': ('用户荐书', '读者荐书')},
            )
        else:
            group['books'] = tuple(dict.fromkeys((*group['books'], *additions)))
    # The legacy xi group has a fixed list. Extend it from the same version's
    # bibliography so future imports do not require editing the production app.
    for group in app_module.CORPUS_SCOPES:
        if group['id'] not in {'xi', 'party_docs'}:
            continue
        existing = tuple(group['books'])
        additions = tuple(book.key for book in app_module.BOOK_CONFIGS
                          if book.collection == ('xi_thought' if group['id']=='xi' else 'party_state_documents') and book.available
                          and book.key in app_module.corpus.books and book.key not in existing)
        group['books'] = tuple(dict.fromkeys((*existing, *additions)))
    registered={key for group in app_module.CORPUS_SCOPES for key in group['books']}
    other=tuple(book.key for book in app_module.BOOK_CONFIGS
                if book.available and book.key in app_module.corpus.books and book.key not in registered
                and getattr(book,'folder','').replace('\\','/')=='pdfs/自动入库')
    if other:
        group=next((g for g in app_module.CORPUS_SCOPES if g['id']=='ingested_other'),None)
        if group is None:
            app_module.CORPUS_SCOPES=(*app_module.CORPUS_SCOPES,
                {'id':'ingested_other','label':'其他入库文献','books':other,'hints':()})
        else:
            group['books']=(*group['books'],*other)
    app_module._CORPUS_SCOPE_BY_ID = {group['id']: group for group in app_module.CORPUS_SCOPES}
