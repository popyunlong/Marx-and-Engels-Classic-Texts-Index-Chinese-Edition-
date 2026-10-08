"""Server builds and independently verifies append-only data on an explicit parent."""
import json
import os
import shutil
import sqlite3
from pathlib import Path
import yaml
from book_data_release import Bundle, canonical, file_hash, safe_path


def verify_append(before,after,packages,before_config,after_config):
    sources={p['source_file'] for p in packages}
    if not sources or len(sources)!=len(packages):
        raise ValueError('new book sources must be unique')
    with sqlite3.connect(Path(after).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.execute('ATTACH DATABASE ? AS old',(str(before),))
        if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('new book database is corrupt')
        for table in ('pages','toc_entries','page_label_evidence'):
            exists=c.execute('SELECT 1 FROM old.sqlite_master WHERE type="table" AND name=?',(table,)).fetchone()
            if not exists:
                continue
            if c.execute(f'SELECT 1 FROM (SELECT * FROM old.{table} EXCEPT SELECT * FROM main.{table}) LIMIT 1').fetchone():
                raise ValueError('old book data changed: '+table)
            rows=c.execute(f'SELECT * FROM main.{table} EXCEPT SELECT * FROM old.{table}').fetchall()
            columns=[r[1] for r in c.execute(f'PRAGMA table_info({table})')]
            pos=columns.index('source_file')
            if any(r[pos] not in sources for r in rows):
                raise ValueError('undeclared old book additions: '+table)
            # EXCEPT has set semantics; also reject duplicate old rows.
            marks=','.join('?' for _ in sources)
            if c.execute(f'SELECT count(*) FROM main.{table} WHERE source_file NOT IN ({marks})',tuple(sources)).fetchone()!=c.execute(f'SELECT count(*) FROM old.{table}').fetchone():
                raise ValueError('old book row multiplicity changed: '+table)
        placeholders=','.join('?' for _ in sources)
        if c.execute(f'SELECT 1 FROM old.pages WHERE source_file IN ({placeholders}) LIMIT 1',tuple(sources)).fetchone():
            raise ValueError('PDF already published; changed JSON requires a revision release')
        for package in packages:
            rows=c.execute('SELECT pdf_page,printed_page,raw_text FROM pages WHERE source_file=? ORDER BY pdf_page',
                           (package['source_file'],)).fetchall()
            expected=[(r['page'],r['label'] or None,r['text']) for r in package['pages']]
            if rows!=expected:
                raise ValueError('candidate text/page sequence differs from declared package')
        # No other stored data or schema can change in an append-only import.
        old_schema=c.execute("SELECT name,type,sql FROM old.sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        new_schema=c.execute("SELECT name,type,sql FROM main.sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        # Page-evidence storage may be added to a legacy DB by its normal writer.
        allowed_new={'page_label_evidence','idx_page_label_evidence_source_page'}
        if any(row not in new_schema for row in old_schema) or any(row[0] not in allowed_new for row in new_schema if row not in old_schema):
            raise ValueError('old database schema modified')
    old_books=yaml.safe_load((Path(before_config)/'books.yaml').read_text('utf-8'))
    new_books=yaml.safe_load((Path(after_config)/'books.yaml').read_text('utf-8'))
    old={b['key']:b for b in old_books['books']}
    new={b['key']:b for b in new_books['books']}
    if any(new.get(k)!=v for k,v in old.items()):
        raise ValueError('old bibliography modified')
    declared={p['metadata']['book_key'] for p in packages}
    if set(new)-set(old)!=declared or declared&set(old):
        raise ValueError('new book keys conflict with existing bibliography')
    for name in ('manifest.yaml','volumes.yaml'):
        a=yaml.safe_load((Path(before_config)/name).read_text('utf-8')) or {}
        b=yaml.safe_load((Path(after_config)/name).read_text('utf-8')) or {}
        if any(b.get(k)!=v for k,v in a.items()) or set(b)-set(a)!=declared:
            raise ValueError('old configuration modified: '+name)
    for path in Path(before_config).iterdir():
        if path.is_file() and path.name not in {'books.yaml','manifest.yaml','volumes.yaml'}:
            target=Path(after_config)/path.name
            if not target.is_file() or file_hash(target)!=file_hash(path):
                raise ValueError('old configuration modified: '+path.name)
    return True


def build(packages,app,output,*,baseline,config,pdfs,parent,old_catalog=None,text_only_sources=()):
    from .candidate import build_candidate
    output=Path(output)
    if output.exists():
        raise ValueError('data version already exists')
    size=Path(baseline).stat().st_size
    if shutil.disk_usage(output.parent).free < size*2+(5<<30):
        raise OSError('数据盘空间不足：构建和回退需要两倍正文库加5 GiB余量')
    record=build_candidate(packages,Path(app),output,data_root=Path(baseline).parent,
                           config_root=config,pdf_root=pdfs)
    verify_append(baseline,output/'data/corpus.sqlite',packages,config,output/'config')
    if (Path(baseline).parent/'subject_index.sqlite').is_file():
        shutil.copy2(Path(baseline).parent/'subject_index.sqlite',output/'data/subject_index.sqlite')
    old_books=yaml.safe_load((Path(config)/'books.yaml').read_text('utf-8'))['books']
    generations_path=Path(baseline).parent/'ingestion-generations.json'
    generations=json.loads(generations_path.read_text('utf-8')) if generations_path.is_file() else {}
    if generations and (generations.get('schema')!=1 or generations.get('current')!=record['base_sha256']):
        raise ValueError('parent citation compatibility manifest mismatch')
    ancestors=dict(generations.get('ancestors',{}))
    ancestors[record['base_sha256']]={'books':[b['key'] for b in old_books]}
    (output/'data/ingestion-generations.json').write_bytes(canonical(dict(schema=1,
        current=record['candidate_sha256'],ancestors=ancestors)))
    from catalog_release import canonical as catalog_canonical, inventory, digest, read_db_toc, Catalog
    catalog=output/'catalog'/output.name
    catalog.mkdir(parents=True)
    for name in ('static_library','stream_library'):
        source=old_catalog.root/name if old_catalog else Path(app)/name
        if source.is_dir():
            # Do not hard-link mutable legacy assets into an immutable data version.
            shutil.copytree(source,catalog/name)
        else:
            (catalog/name).mkdir()
    with sqlite3.connect(output/'data/corpus.sqlite') as c:
        rows=read_db_toc(c)
        sources=[dict(zip(('book','volume','source_file','page_count','last_page'),r)) for r in c.execute(
            'SELECT book,volume,source_file,count(*),max(pdf_page) FROM pages GROUP BY book,volume,source_file ORDER BY source_file')]
    # A reviewed catalogue can differ from the original DB catalogue. Preserve
    # those accepted old-book rows, while binding input_toc to the actual new DB.
    effective_rows = rows
    if old_catalog:
        added = {p['source_file'] for p in packages}
        effective_rows = old_catalog.rows + [r for r in rows if r['source_file'] in added]
    (catalog/'toc.json').write_bytes(catalog_canonical(effective_rows))
    (catalog/'sources.json').write_bytes(catalog_canonical(sources))
    manifest=dict(schema_version=1,id=output.name,parent=None,input_toc_sha256=digest(rows),
                  files=inventory(catalog),changes={},approvals={})
    (catalog/'catalog.json').write_bytes(catalog_canonical(manifest))
    checked=Catalog(catalog)
    (output/'packages.json').write_bytes(canonical(packages))
    previous_articles=Path(baseline).parent.parent/'articles.json'
    articles=json.loads(previous_articles.read_text('utf-8')) if previous_articles.is_file() else []
    articles += [dict(source_file=p['source_file'],**t) for p in packages for t in p['toc']]
    (output/'articles.json').write_bytes(canonical(articles))
    files={p.relative_to(output).as_posix():file_hash(p) for p in output.rglob('*') if p.is_file()}
    proof=dict(schema=1,id=output.name,parent=parent,baseline_sha256=file_hash(baseline),
        files=files,catalog={'id':output.name,'sha256':checked.sha256},
        text_only_sources=sorted(set(text_only_sources)|{p['source_file'] for p in packages}),
        added_sources=[p['source_file'] for p in packages],candidate=record)
    (output/'book-data.json').write_bytes(canonical(proof))
    for path in output.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o444)
    output.chmod(0o755)
    (output/'catalog').chmod(0o755)
    selected=dict(id=output.name,sha256=file_hash(output/'book-data.json'))
    Bundle(output,selected['sha256'])
    return selected
