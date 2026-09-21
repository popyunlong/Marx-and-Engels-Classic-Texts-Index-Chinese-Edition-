"""Register configured ingested books in existing research scope groups."""


def install(app_module):
    from .economics_catalog import install_scopes
    install_scopes(app_module)
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
        if group['id'] != 'xi':
            continue
        existing = tuple(group['books'])
        additions = tuple(book.key for book in app_module.BOOK_CONFIGS
                          if book.collection == 'xi_thought' and book.available
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
