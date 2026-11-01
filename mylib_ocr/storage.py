"""Same private S3 namespace and host-side configuration as the storage node."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

MAX_SOURCE=100*1024*1024
MAX_RESULT=64*1024*1024


class Storage:
    def __init__(self, root: Path):
        import boto3
        from botocore.config import Config
        self.root=Path(root)
        self.bucket=os.environ.get('MYLIB_S3_BUCKET','mylib')
        self.s3=boto3.client('s3',endpoint_url=os.environ.get('MYLIB_S3_ENDPOINT','http://127.0.0.1:9000'),
            aws_access_key_id=os.environ['MINIO_ROOT_USER'],aws_secret_access_key=os.environ['MINIO_ROOT_PASSWORD'],
            config=Config(signature_version='s3v4',retries={'max_attempts':3}),region_name='us-east-1')

    @staticmethod
    def prefix(job):
        return f"mylib/{int(job['uid'])}/{int(job['sid'])}"

    def get(self, key, limit=MAX_RESULT):
        from botocore.exceptions import ClientError
        try:
            response=self.s3.get_object(Bucket=self.bucket,Key=key)
        except ClientError as exc:
            if exc.response.get('Error',{}).get('Code') in ('NoSuchKey','404'):
                return None
            raise
        with response['Body'] as stream:
            data=stream.read(limit+1)
        if len(data)>limit:
            raise ValueError('Stored object exceeds allowed size')
        return data

    def source(self, job):
        directory=self.root/'sources'/str(job['uid'])/str(job['sid'])
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        target=directory/(job['source_sha256']+'.pdf')
        if not target.exists():
            legacy=Path(os.environ.get('MYLIB_CACHE_DIR','/opt/mylib/cache'))/'pdf'/f"{job['uid']}_{job['sid']}.pdf"
            blob=legacy.read_bytes() if legacy.is_file() and legacy.stat().st_size<=MAX_SOURCE else self.get(self.prefix(job)+'/source.pdf',MAX_SOURCE)
            if not blob or hashlib.sha256(blob).hexdigest()!=job['source_sha256']:
                raise ValueError('Original PDF fingerprint mismatch; no checkpoint discarded')
            temp=directory/(uuid.uuid4().hex+'.tmp')
            try:
                temp.write_bytes(blob)
                temp.chmod(0o600)
                os.replace(temp,target)
            finally:
                temp.unlink(missing_ok=True)
        with target.open('rb') as stream:
            digest=hashlib.file_digest(stream,'sha256').hexdigest() if hasattr(hashlib,'file_digest') else None
        if digest is None:
            digest=hashlib.sha256(target.read_bytes()).hexdigest()
        if digest!=job['source_sha256']:
            raise ValueError('Pinned source fingerprint mismatch')
        return target

    def legacy_pages(self,job):
        blob=self.get(self.prefix(job)+'/ocr.jsonl')
        pages=[]
        for line in (blob or b'').decode('utf-8').splitlines():
            value=json.loads(line)
            if not value.get('text'):
                continue
            if int(value.get('layout_schema') or 0)!=job['layout_schema']:
                raise ValueError('Legacy checkpoint schema mismatch; review required')
            if not 1<=int(value['page'])<=job['total']:
                continue
            value['ocr_profile']='legacy-v3'
            pages.append(value)
        return pages

    def result_key(self,job):
        return self.prefix(job)+'/ocr-runs/'+job['run_id']+'/text.jsonl'

    def publish_result(self,job,payload):
        # An immutable run-specific candidate. Never overwrite the original sidecar/text.
        self.s3.put_object(Bucket=self.bucket,Key=self.result_key(job),Body=payload,ContentType='application/x-ndjson')
