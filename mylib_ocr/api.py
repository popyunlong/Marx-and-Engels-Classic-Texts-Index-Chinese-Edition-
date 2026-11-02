"""Authenticated queue API; this process never imports an OCR engine."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request

from .queue import Conflict, Paused, Queue
from .storage import Storage


def create_app(root=None, *, token=None, storage=None):
    app=Flask(__name__)
    queue=Queue(Path(root or os.environ.get('MYLIB_OCR_ROOT','/opt/mylib-ocr/shared')))
    expected=os.environ.get('MYLIB_WORKER_TOKEN','') if token is None else token
    app.config['MAX_CONTENT_LENGTH']=4096

    @app.before_request
    def auth():
        got=request.headers.get('Authorization','')
        if not expected or not secrets.compare_digest(got,'Bearer '+expected):
            abort(401)

    @app.get('/ocr/health')
    def health():
        return jsonify(ok=True,protocol_version=2,dispatch_enabled=queue.dispatch_enabled())

    @app.post('/ocr/start')
    def start():
        try:
            if request.args.get('force') not in (None,'0'):
                raise Conflict('Forced rebuild is not supported by the resume service')
            result=queue.enqueue(int(request.args.get('uid','0')),int(request.args.get('sid','0')),
                request.args.get('run_id',''),request.args.get('source_sha256',''),int(request.args.get('max_pages','0')))
        except Paused as exc:
            return jsonify(error=str(exc)),423
        except Conflict as exc:
            return jsonify(error=str(exc)),409
        except (ValueError,TypeError) as exc:
            return jsonify(error=str(exc)),400
        return jsonify(ok=True,**result)

    def current_job():
        try:
            uid=int(request.args.get('uid','0'));sid=int(request.args.get('sid','0'))
        except ValueError:
            abort(400)
        result=queue.status(uid,sid,request.args.get('run_id',''))
        if result is None:
            abort(404,description='Task not found; no automatic restart')
        return result

    @app.get('/ocr/status')
    def status():
        return jsonify(ok=True,**current_job())

    @app.get('/ocr/text')
    def text():
        job=current_job()
        if job['state']!='done' or job['processed_pages']!=job['total']:
            abort(409,description='Only completed run-specific text can be read')
        client=storage or Storage(queue.root)
        payload=client.get(client.result_key(job))
        if payload is None:
            abort(404)
        return Response(payload,content_type='application/x-ndjson; charset=utf-8')

    return app


if __name__=='__main__':
    from waitress import serve
    serve(create_app(),host='127.0.0.1',port=int(os.environ.get('MYLIB_OCR_API_PORT','8801')),threads=2)
