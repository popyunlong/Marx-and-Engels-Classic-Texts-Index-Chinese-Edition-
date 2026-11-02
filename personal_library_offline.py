"""Administrator-only, generation-guarded import of complete local OCR bundles.

Original PDFs and node storage are never changed. Imports keep their exact input
and a durable one-shot receipt in the main site's private shared data directory.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
import threading
from pathlib import Path

from mylib_ocr.layout import validate_folios

MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024


class ImportConflict(ValueError):
    pass


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('离线OCR整数范围不正确')
    return value


def _fraction(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('离线OCR坐标或置信度超出归一化范围')
    return value


def _page(value, number):
    if not isinstance(value, dict) or _integer(value.get('page'), 1, 10000) != number:
        raise ValueError('必须包含连续且不重复的全部PDF页面，包括空文本页')
    if value.get('layout_schema') != 3 or value.get('ocr_engine', 'rapidocr-layout-v3') != 'rapidocr-layout-v3':
        raise ValueError('离线OCR页格式或引擎不匹配')
    text = value.get('text')
    if not isinstance(text, str) or len(text) > 262144:
        raise ValueError('离线OCR页文字无效或过大')
    blocks = value.get('layout_blocks', [])
    candidates = value.get('folio_candidates', [])
    if not isinstance(blocks, list) or len(blocks) > 5000 or not isinstance(candidates, list) or len(candidates) > 1000:
        raise ValueError('离线OCR布局条数无效')
    safe_blocks = []
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get('text'), str) or len(block['text']) > 65536:
            raise ValueError('离线OCR行文字无效')
        box = block.get('bbox')
        if not isinstance(box, list) or len(box) != 4:
            raise ValueError('离线OCR行坐标无效')
        x0, y0, x1, y1 = [_fraction(v) for v in box]
        if x1 <= x0 or y1 <= y0:
            raise ValueError('离线OCR行坐标为空或倒置')
        safe_blocks.append(dict(text=block['text'], bbox=box, confidence=_fraction(block.get('confidence', 0))))
    safe_candidates = []
    for item in candidates:
        if not isinstance(item, dict) or item.get('band') not in ('top', 'bottom') or item.get('edge') not in ('left', 'right', 'center') or item.get('source') not in ('standalone', 'fused-left', 'fused-right'):
            raise ValueError('离线OCR页码候选格式无效')
        safe_candidates.append(dict(value=_integer(item.get('value'), 1, 3000),
            band=item['band'], edge=item['edge'], source=item['source'], x=_fraction(item.get('x')),
            y=_fraction(item.get('y')), confidence=_fraction(item.get('confidence', 0))))
    profile = value.get('ocr_profile', 'legacy-v3')
    if profile not in ('legacy-v3', 'bounded-page-v1'):
        raise ValueError('离线OCR计算格式不匹配')
    result = dict(page=number, text=text, layout_schema=3, ocr_engine='rapidocr-layout-v3',
                  ocr_profile=profile, layout_blocks=safe_blocks, folio_candidates=safe_candidates)
    if 'image_width' in value or 'image_height' in value:
        width = _integer(value.get('image_width'), 1, 65536)
        height = _integer(value.get('image_height'), 1, 65536)
        if profile == 'bounded-page-v1' and (width > 2200 or width * height > 8_000_000):
            raise ValueError('离线OCR新页超过像素上限')
        result.update(image_width=width, image_height=height)
    return result


def read_bundle(path, row):
    """Validate one bounded file before creating a processing run or touching an index."""
    if path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError('离线OCR包超过32MiB')
    digest = hashlib.sha256()
    pages = []
    with path.open('rb') as stream:
        first = stream.readline(MAX_LINE_BYTES + 1)
        if len(first) > MAX_LINE_BYTES:
            raise ValueError('离线OCR单行超过1MiB')
        digest.update(first)
        manifest = json.loads(first.decode('utf-8'))
        if not isinstance(manifest, dict) or manifest.get('type') != 'mylib-offline-ocr' or manifest.get('format_version') != 1 or manifest.get('layout_schema') != 3 or manifest.get('engine') != 'rapidocr-layout-v3':
            raise ValueError('离线OCR包格式不匹配')
        uid = _integer(manifest.get('uid'), 1, 2**63-1)
        sid = _integer(manifest.get('sid'), 1, 2**63-1)
        total = _integer(manifest.get('total'), 1, 10000)
        if not isinstance(manifest.get('run_id'), str) or not re.fullmatch('[a-f0-9]{32}', manifest['run_id']):
            raise ValueError('离线OCR run_id无效')
        if not isinstance(manifest.get('source_sha256'), str) or not re.fullmatch('[a-f0-9]{64}', manifest['source_sha256']):
            raise ValueError('离线OCR原件指纹无效')
        if (uid, sid, manifest['source_sha256'], total) != (int(row['user_id']), int(row['id']), row['sha256'], int(row['page_count'])):
            raise ImportConflict('离线OCR的提交者、提交编号、原件指纹或总页数与原提交不符')
        for number in range(1, total + 1):
            line = stream.readline(MAX_LINE_BYTES + 1)
            digest.update(line)
            if len(line) > MAX_LINE_BYTES:
                raise ValueError('离线OCR单行超过1MiB')
            if not line:
                raise ValueError('离线OCR未包含全部PDF页面；未生成派生数据或验收')
            pages.append(_page(json.loads(line.decode('utf-8')), number))
        if stream.read(1):
            raise ValueError('离线OCR包含多余页面或内容')
    # Imported printed_pages cannot create evidence. Recompute from real v3 candidates.
    printed, quality = validate_folios({p['page']: p['folio_candidates'] for p in pages}, total)
    for page in pages:
        page['printed_pages'] = printed.get(page['page'], [])
    return manifest, pages, digest.hexdigest(), quality


class OfflineImports:
    def __init__(self, library, processor, *, thread=threading.Thread):
        self.library = library
        self.processor = processor
        self.thread = thread
        self.root = Path(library.PERSONAL_LIB_DIR) / 'offline-imports'

    def _dirs(self):
        for path in (self.root, self.root/'incoming', self.root/'runs'):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _receipt_path(self, run_id):
        if not re.fullmatch('[a-f0-9]{32}', run_id):
            raise ValueError('Invalid run ID')
        return self.root/'runs'/(run_id+'.json')

    def _update(self, receipt):
        path = self._receipt_path(receipt['run_id'])
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(receipt, stream, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def status(self, row, run_id):
        try:
            receipt = json.loads(self._receipt_path(run_id).read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        if (receipt['uid'], receipt['sid'], receipt['source_sha256']) != (int(row['user_id']), int(row['id']), row['sha256']):
            raise ImportConflict('该导入任务不属于当前提交或原件')
        if row.get('processing_run_id') == run_id:
            receipt = dict(receipt, processing_state=row.get('processing_run_state'), submission_status=row.get('status'))
            if row.get('processing_run_state') in ('completed', 'interrupted'):
                receipt['state'] = row['processing_run_state']
        return receipt

    def submit(self, sid, stream):
        row = self.library.get_submission(sid)
        if not row:
            raise ValueError('该提交不存在')
        if not row.get('license_attested') or not row.get('ocr_consent') or row.get('source_kind') != 'scanned':
            raise ValueError('该提交未授权扫描件OCR')
        self._dirs()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root/'incoming', delete=False) as output:
                temporary = Path(output.name)
                size = 0
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BUNDLE_BYTES:
                        raise ValueError('离线OCR包超过32MiB')
                    output.write(chunk)
                output.flush(); os.fsync(output.fileno())
            manifest, pages, bundle_sha, quality = read_bundle(temporary, row)
            run_id = manifest['run_id']
            with self.library.submission_lock(sid):
                current = self.library.get_submission(sid)
                if not current or (current['user_id'], current['sha256'], current['page_count']) != (row['user_id'], row['sha256'], row['page_count']):
                    raise ImportConflict('导入校验期间原提交已改变')
                old = self.status(current, run_id)
                if old:
                    if old['bundle_sha256'] != bundle_sha:
                        raise ImportConflict('run_id已用于不同导入包，不能覆盖')
                    return dict(old, duplicate=True)
                if current.get('status') not in ('failed', 'quality_review') or current.get('processing_run_state') in ('active', 'finalizing'):
                    raise ImportConflict('仅可导入待复核或失败的无活动任务扫描件')
                allowed, _why = self.library.ocr_quota_allowance(int(row['user_id']), manifest['total'])
                if allowed < manifest['total']:
                    raise ValueError('现有OCR额度不足；未导入部分结果或追加扣额')
                directory = self.root/str(manifest['uid'])/str(sid)
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                bundle = directory/(run_id+'.jsonl')
                receipt = dict(run_id=run_id, uid=manifest['uid'], sid=sid, source_sha256=manifest['source_sha256'],
                               total=manifest['total'], bundle_sha256=bundle_sha, state='prepared',
                               created_at=datetime.now(timezone.utc).isoformat())
                # Exclusive, global run ID reservation. Even a crash consumes this ID;
                # unknown outcomes are inspected rather than silently submitted again.
                try:
                    with self._receipt_path(run_id).open('x', encoding='utf-8') as file:
                        json.dump(receipt, file); file.flush(); os.fsync(file.fileno())
                except FileExistsError as exc:
                    raise ImportConflict('run_id正在被另一请求保留，请只查询原任务') from exc
                if bundle.exists():
                    raise ImportConflict('导入原包已存在，请核对审计，禁止覆盖')
                os.replace(temporary, bundle); temporary = None
                bundle.chmod(0o400)
                try:
                    self.library.begin_processing_run(sid, run_id=run_id, kind='offline')
                    receipt['state'] = 'queued'; self._update(receipt)
                    self.thread(target=self._run, args=(row, run_id, pages, quality, receipt),
                                name=f'mylib-offline-{sid}-{run_id[:8]}', daemon=True).start()
                except Exception as exc:
                    self.library.fail_processing_run(sid, run_id, str(exc))
                    receipt.update(state='interrupted', error=str(exc)[:300]); self._update(receipt)
                    raise
                return dict(receipt, duplicate=False)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _run(self, row, run_id, pages, quality, receipt):
        sid = int(row['id'])
        try:
            current = self.library.get_submission(sid)
            if not current or (current['user_id'], current['sha256'], current['page_count']) != (row['user_id'], row['sha256'], row['page_count']):
                raise ImportConflict('离线导入原提交已改变，未写入私有索引')
            self.processor.commit(current, run_id, pages, {'layout_quality': quality}, ocr=True)
            current = self.library.get_submission(sid)
            if not current or current.get('processing_run_id') != run_id or current.get('processing_run_state') != 'completed':
                raise ImportConflict('离线导入代次未完成，未再次尝试')
            receipt.update(state='completed', submission_status=current.get('status'))
        except Exception as exc:
            self.library.fail_processing_run(sid, run_id, str(exc))
            receipt.update(state='interrupted', error=str(exc)[:300])
        self._update(receipt)


def register_admin_routes(app, *, library, processor, require_admin, csrf, audit, flash, redirect):
    from flask import abort, jsonify, request

    @app.post('/admin/library/submissions/<int:submission_id>/import-ocr')
    def admin_mylib_import_ocr(submission_id):
        require_admin()
        csrf()
        upload = request.files.get('file')
        if upload is None or not upload.filename:
            abort(400, description='请选择完整的离线OCR JSONL文件。')
        try:
            result = OfflineImports(library, processor()).submit(submission_id, upload.stream)
        except ImportConflict as exc:
            abort(409, description=str(exc))
        except (ValueError, UnicodeError) as exc:
            abort(400, description=str(exc))
        audit(action='mylib.offline_import', target=str(submission_id), result='duplicate' if result['duplicate'] else 'queued',
              remote_admin=True, details={'run_id': result['run_id'], 'bundle_sha256': result['bundle_sha256']})
        flash('离线OCR原任务已登记，本次未重复处理。' if result['duplicate'] else '完整离线OCR包已登记，正在后台生成私有文库数据并验收一次。', 'success')
        return redirect(True, 'copy')

    @app.get('/admin/library/submissions/<int:submission_id>/offline-import/<run_id>')
    def admin_mylib_offline_import_status(submission_id, run_id):
        require_admin()
        row = library.get_submission(submission_id)
        if not row:
            abort(404)
        try:
            result = OfflineImports(library, processor()).status(row, run_id)
        except (ValueError, UnicodeError):
            abort(404)
        if result is None:
            abort(404)
        return jsonify(ok=True, **result)
