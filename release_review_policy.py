"""Server-owned review policy for verified append-only book releases."""


def append_fast(metadata, parent):
    mode = metadata.get('review_mode', 'standard')
    if mode == 'standard':
        return False
    if mode != 'append-fast':
        raise ValueError('unknown release review mode')
    if (metadata.get('release_type') != 'new_books'
            or metadata.get('append_only_verified') is not True
            or metadata.get('parent_release_id') != parent.get('release_id')
            or not metadata.get('book_data_release')
            or metadata['book_data_release'] == parent.get('book_data_release')
            or not metadata.get('book_data_catalog')):
        raise ValueError('fast review requires a verified append on the exact live parent')
    for field in ('catalog_release', 'corpus_release', 'dictionary_graph_release'):
        if metadata.get(field) != parent.get(field):
            raise ValueError('fast book review cannot change another data binding')
    return True
