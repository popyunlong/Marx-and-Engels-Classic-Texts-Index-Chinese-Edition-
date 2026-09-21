from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .store import sha256


def build_candidate(packages, app_root: Path, directory: Path, checkpoint=lambda: None, *, unit_checkpoint=None):
    """Append only, preserving every old row ID and every existing config entry.

    Caller owns the common publish lock. SQLite backup makes a coherent snapshot;
    progress checks let citation demand cancel work without touching production.
    """
    app_root, directory = Path(app_root).resolve(), Path(directory).resolve()
    unit_checkpoint = unit_checkpoint or checkpoint
    next_poll = 0.0
    def poll():
        # SQLite VM and file-hash callbacks can fire thousands of times per
        # second. Poll cancellable long work every two seconds; explicit unit
        # admission checks below still run before each insertion transaction.
        nonlocal next_poll
        if time.monotonic() >= next_poll:
            checkpoint()
            next_poll = time.monotonic() + 2
    if directory == app_root or app_root in directory.parents:
        raise ValueError("候选输出必须位于生产应用目录之外")
    directory.mkdir(parents=True, exist_ok=True)
    data = directory / "data"
    config = directory / "config"
    data.mkdir(exist_ok=True)
    config.mkdir(exist_ok=True)
    base = app_root / "data/corpus.sqlite"
    base_sha = sha256(base, poll)
    configs = {name: sha256(app_root / "config" / name) for name in ["books.yaml", "manifest.yaml", "volumes.yaml"]}
    books_config = yaml.safe_load((app_root / "config/books.yaml").read_text(encoding="utf-8"))
    books = books_config["books"]
    manifest = yaml.safe_load((app_root / "config/manifest.yaml").read_text(encoding="utf-8"))
    volumes = yaml.safe_load((app_root / "config/volumes.yaml").read_text(encoding="utf-8"))
    candidate_db = data / "corpus.sqlite"
    with sqlite3.connect(base.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(candidate_db) as dst:
        src.backup(dst, pages=256, progress=lambda *_: poll(), sleep=.01)
    # Import the production normalizer only; never call its destructive build().
    import sys
    sys.path.insert(0, str(app_root))
    from build_index import normalize
    sources, added = [], []
    expected_text_updates = {}
    with sqlite3.connect(candidate_db) as c:
        for package in packages:
            unit_checkpoint()
            if not re.fullmatch(r"[0-9a-f]{64}", package["source_sha256"]) or not re.fullmatch(r"[0-9a-f]{32}", package["book_id"]):
                raise ValueError("候选身份或哈希无效")
            if [r["page"] for r in package["pages"]] != list(range(1, package["page_count"] + 1)):
                raise ValueError("候选页码有重复或遗漏")
            metadata = package["metadata"]
            source = package["source_file"]
            expected = "pdfs/自动入库/" + package["source_sha256"] + ".pdf"
            if source != expected or not 1 <= len(package["pages"]) == package["page_count"]:
                raise ValueError("候选清单无效")
            if sha256(app_root / source, poll) != package["source_sha256"]:
                raise ValueError("生产 PDF 与验收哈希不符")
            if c.execute("SELECT 1 FROM pages WHERE source_file=? LIMIT 1", (source,)).fetchone():
                if not package.get('economics28_revision') or not package.get('economics28'):
                    raise ValueError("该 PDF 已存在于生产语料，拒绝重复插入")
                from .economics_revision import refine
                expected_text_updates.update(refine(c, package, normalize))
                added.append(dict(key=metadata['book_key'],source_file=source,pages=package['page_count'],id=package['book_id']))
                sources.append(source)
                c.commit()
                continue
            economic = bool(package.get('economics28') or package.get('paddle_source'))
            if economic:
                if package.get('paddle_source'):
                    from .paddle_catalog import register
                else:
                    from .economics_catalog import register
                book_key, volume_number = register(package, books, manifest, volumes)
            else:
                book_key, volume_number = metadata["title"], 1
            if not economic and any(row["key"] == book_key for row in books):
                book_key += "（" + metadata["year"] + "年版·" + package["source_sha256"][:8] + "）"
            sources.append(source)
            added.append({"key": book_key, "source_file": source, "pages": package["page_count"], "id": package["book_id"]})
            for offset in range(0, len(package["pages"]), 100):
                unit_checkpoint()
                c.executemany("INSERT INTO pages(book,volume,source_file,pdf_page,printed_page,raw_text,normalized_text) VALUES(?,?,?,?,?,?,?)",
                              [(book_key, volume_number, source, row["page"], row["label"] or None, row["text"], normalize(row["text"]))
                               for row in package["pages"][offset:offset + 100]])
                c.commit()
            from .page_evidence import persist as persist_page_evidence
            persist_page_evidence(c, source, package["pages"])
            unit_checkpoint()
            c.executemany("INSERT INTO toc_entries(book,volume,source_file,title,pdf_page,printed_page,level,kind,sort_order) VALUES(?,?,?,?,?,?,?,?,?)",
                          [(book_key, volume_number, source, row["title"], row["pdf_page"], row["printed_page"], row["level"], row["kind"], row["sort_order"])
                           for row in package["toc"]])
            c.commit()
            if economic:
                continue
            trial = package.get('release_quality') == 'provisional_text'
            books.append(dict(key=book_key, title="《" + metadata["title"] + "》", short_title="《" + metadata["title"] + "》" + ('（试用）' if trial else ''),
                              citation_title=metadata["title"], folder="pdfs/自动入库", single_volume=True,
                              publisher=metadata["publisher"], available=True, sort_order=max(int(b.get("sort_order", 0)) for b in books) + 1,
                              collection="xi_thought" if "习近平" in metadata["title"] else "other", tag_class="xi-gangyao" if "习近平" in metadata["title"] else "default"))
            manifest[book_key] = [dict(file=source, volume=1, display_title=metadata["title"], sha256=package["source_sha256"], page_count=package["page_count"])]
            volumes[book_key] = {1: int(metadata["year"])}
        checkpoint()
        c.set_progress_handler(lambda: _interrupt(poll), 10000)
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("候选库完整性检查失败")
        # Exact preservation is checked in SQL, including IDs/text/folio mappings.
        c.execute("ATTACH DATABASE ? AS original", (str(base),))
        missing = c.execute("SELECT * FROM original.pages EXCEPT SELECT * FROM main.pages").fetchall()
        id_index = [r[1] for r in c.execute('PRAGMA main.table_info(pages)')].index('id')
        if any(row[id_index] not in expected_text_updates for row in missing):
            raise ValueError("候选库改变了已有页面")
        for row_id, expected_row in expected_text_updates.items():
            if c.execute('SELECT * FROM main.pages WHERE id=?', (row_id,)).fetchone() != expected_row:
                raise ValueError('精校改变了授权文字以外的页面字段')
        if c.execute('SELECT COUNT(*) FROM (SELECT * FROM original.toc_entries EXCEPT SELECT * FROM main.toc_entries)').fetchone()[0]:
            raise ValueError('候选库改变了已有目录')
    for path in (app_root / "config").iterdir():
        if path.is_file():
            shutil.copy2(path, config / path.name)
    for name, value in [("books.yaml", books_config), ("manifest.yaml", manifest), ("volumes.yaml", volumes)]:
        (config / name).write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if sha256(base, poll) != base_sha or any(sha256(app_root / "config" / n) != h for n, h in configs.items()):
        raise ValueError("生产基线已经变化，需要重新合并")
    digest = sha256(candidate_db, poll)
    (data / "corpus.sqlite.sha256").write_text(digest + "\n", encoding="ascii")
    record = dict(schema=1, base_sha256=base_sha, base_config=configs, candidate_sha256=digest,
                  candidate_config={n: sha256(config / n) for n in configs}, books=added,
                  append_only_verified=not bool(expected_text_updates), protected_revision_verified=bool(expected_text_updates), created=time.time())
    record['quality'] = {p['book_id']: {'release_quality': p.get('release_quality', 'reviewed'),
                                      'pending_text_pages': p.get('pending_text_pages', 0),
                                      'quality_note': p.get('quality_note', '')} for p in packages}
    record['packages_sha256'] = hashlib.sha256(json.dumps(packages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    (directory / "candidate.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def _interrupt(checkpoint):
    try:
        checkpoint()
        return 0
    except Exception:
        return 1


def validate_prepared(directory, app_root, packages, checkpoint=lambda: None):
    """Reuse an immutable candidate only while both input and baseline match."""
    directory, app_root = Path(directory), Path(app_root)
    next_poll=0.0
    def poll():
        nonlocal next_poll
        if time.monotonic() >= next_poll:
            checkpoint()
            next_poll=time.monotonic()+2
    record = json.loads((directory / 'candidate.json').read_text(encoding='utf-8'))
    if not (record.get('append_only_verified') or record.get('protected_revision_verified')):
        raise ValueError('候选缺少原有数据保留验收')
    expected = [(p['book_id'], p['source_file'], p['page_count']) for p in packages]
    if [(b['id'], b['source_file'], b['pages']) for b in record['books']] != expected:
        raise ValueError('候选书目与本次任务不符')
    # A changed correction/TOC package must never silently reuse old content.
    fingerprint = hashlib.sha256(json.dumps(packages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if record.get('packages_sha256') != fingerprint:
        raise ValueError('候选输入内容未登记或已经变化，需要重新准备')
    for root, db_hash, config_hash in [(app_root, record['base_sha256'], record['base_config']),
                                      (directory, record['candidate_sha256'], record['candidate_config'])]:
        if sha256(root / 'data/corpus.sqlite', poll) != db_hash:
            raise ValueError('候选或生产数据库已变化，需要重新合并')
        if any(sha256(root / 'config' / n, poll) != h for n, h in config_hash.items()):
            raise ValueError('候选或生产配置已变化，需要重新合并')
    return record


def stamp_public_windows(directory, packages, record, *, now=None):
    """Finalize timed-book visibility immediately before candidate startup.

    Prepared candidates deliberately contain empty timestamps so a failed or
    abandoned preflight cannot consume a reader-facing publication window.
    This step changes candidate configuration only, refreshes its recorded
    digest, and is safe to repeat on a later publish attempt.
    """
    targets = {
        str(package.get('metadata', {}).get('book_key') or ''): int(
            package.get('metadata', {}).get('public_window_days') or 0
        )
        for package in packages
        if package.get('metadata', {}).get('collection') == 'user_recommended'
    }
    if not targets:
        return record
    if any(not key or days != 30 for key, days in targets.items()):
        raise ValueError('用户荐书公开窗口配置无效')
    started = now or datetime.now(timezone.utc)
    started = started.astimezone(timezone.utc).replace(microsecond=0)
    ended = started + timedelta(days=30)
    config_path = Path(directory) / 'config/books.yaml'
    payload = yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}
    books = payload.get('books') or []
    seen = set()
    for book in books:
        key = str(book.get('key') or '') if isinstance(book, dict) else ''
        if key not in targets:
            continue
        book['public_from'] = started.isoformat()
        book['public_until'] = ended.isoformat()
        seen.add(key)
    if seen != set(targets):
        raise ValueError('候选配置缺少待启用的用户荐书')
    stat = config_path.stat()
    temporary = config_path.with_name(config_path.name + '.timed-window.tmp')
    temporary.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding='utf-8'
    )
    os.chmod(temporary, stat.st_mode)
    if hasattr(os, 'chown'):
        os.chown(temporary, stat.st_uid, stat.st_gid)
    os.replace(temporary, config_path)
    record['candidate_config']['books.yaml'] = sha256(config_path)
    record['public_window'] = {
        'books': sorted(seen),
        'public_from': started.isoformat(),
        'public_until': ended.isoformat(),
        'duration_days': 30,
    }
    (Path(directory) / 'candidate.json').write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    return record
