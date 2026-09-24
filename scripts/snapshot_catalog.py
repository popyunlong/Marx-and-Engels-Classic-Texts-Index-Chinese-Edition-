"""Read-only SSH snapshot under the common release lock; local artifacts only."""
from pathlib import Path
import argparse
import subprocess
import tarfile

REMOTE = r'''
import io,json,tarfile,pathlib,sqlite3,urllib.request,datetime,sys
base=pathlib.Path('/opt/marx-search');app=(base/'current/app').resolve()
c=sqlite3.connect('file:/opt/marx-search/data/corpus.sqlite?mode=ro',uri=True)
c.row_factory=sqlite3.Row;c.execute('BEGIN')
def runtime():return json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/runtime',timeout=30))
before=runtime()
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz') as tar:
 def dump(name,value):
  data=json.dumps(value,ensure_ascii=False).encode();info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data))
 dump('baseline.json',{'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'runtime':before,'app':str(app)})
 dump('toc_entries.json',[dict(r) for r in c.execute('select * from toc_entries order by source_file,sort_order,rowid')])
 dump('sources.json',[dict(r) for r in c.execute('select book,volume,source_file,count(*) page_count,min(pdf_page) first_page,max(pdf_page) last_page from pages group by source_file order by book,volume,source_file')])
 for folder in ['static_library','stream_library']:
  for p in sorted((base/folder).rglob('*')):
   if p.is_symlink():raise ValueError('snapshot refuses symlink: '+str(p))
   if p.is_file():tar.add(p,arcname=str(p.relative_to(base)),recursive=False)
 for name in ['books.yaml','volumes.yaml','static_books.yaml','stream_books.yaml']:
  if (app/'config'/name).is_file():tar.add(app/'config'/name,arcname='config/'+name,recursive=False)
 after=runtime()
 if before['app_release']!=after['app_release']:raise ValueError('live release changed')
 dump('end_runtime.json',after)
c.rollback()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--host', default='root@38.76.174.234')
    parser.add_argument('--key', type=Path, default=Path.home()/'.ssh/id_marx_cloud_ed25519')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    archive = args.output/'snapshot.tar.gz'
    with archive.open('wb') as handle:
        result = subprocess.run(['ssh','-i',str(args.key),'-o','BatchMode=yes','-o','ConnectTimeout=15',args.host,
                                 'flock -s /run/lock/marx-search-release.lock nice -n 15 /opt/marx-search/.venv/bin/python -B -'],
                                input=REMOTE.encode(), stdout=handle, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace'))
    target = args.output/'snapshot'
    target.mkdir()
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if not member.isfile() or not (target/member.name).resolve().is_relative_to(target.resolve()):
                raise ValueError('unsafe snapshot member')
        tar.extractall(target)
    print('Read-only production snapshot:', target)


if __name__ == '__main__':
    main()
