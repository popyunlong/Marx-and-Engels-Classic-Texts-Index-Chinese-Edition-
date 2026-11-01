"""Build only committed OCR component files; no models/PDFs/dependencies in archives."""
import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path


def build(repo,sha,release_id,parent,output):
    if not re.fullmatch('[a-f0-9]{40}',sha) or not re.fullmatch('[A-Za-z0-9._-]{1,128}',release_id):
        raise ValueError('Invalid immutable release identity')
    paths=subprocess.check_output(['git','ls-tree','-r','--name-only',sha,'--','mylib_ocr',
        'deploy/mylib-ocr-api.service','deploy/mylib-ocr-worker.service','deploy/mylib_ocr_transaction.py'],cwd=repo,text=True).splitlines()
    if not {'mylib_ocr/api.py','mylib_ocr/worker.py','deploy/mylib_ocr_transaction.py'}<=set(paths):
        raise ValueError('OCR component is incomplete')
    contents={name:subprocess.check_output(['git','show',sha+':'+name],cwd=repo) for name in paths}
    if any(len(value)>2*1024*1024 for value in contents.values()):
        raise ValueError('Unexpected large OCR source file')
    metadata={'component':'mylib-ocr','protocol_version':2,'git_sha':sha,'release_id':release_id,
              'parent_release_id':parent,'files':{name:hashlib.sha256(data).hexdigest() for name,data in contents.items()}}
    contents['release.json']=json.dumps(metadata,sort_keys=True).encode()
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as raw, gzip.GzipFile(fileobj=raw,mode='wb',mtime=0) as zipped, tarfile.open(fileobj=zipped,mode='w') as archive:
        for name,data in sorted(contents.items()):
            info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o444;info.mtime=0
            archive.addfile(info,io.BytesIO(data))
    return metadata


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for key in ['repo','sha','release-id','parent','output']:
        parser.add_argument('--'+key,required=True)
    args=parser.parse_args()
    build(args.repo,args.sha,args.release_id,args.parent,args.output)
