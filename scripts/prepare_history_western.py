"""Build review drafts and bounded original-page evidence sheets."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ingestion.history_western import prepare


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--render',action='store_true')
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    specs=json.loads((Path(__file__).resolve().parents[1]/'config/history_western_sources.json').read_text('utf8'))
    packages=[];tasks={'copyright':[],'toc':[],'folios':[],'samples':[]}
    for n,spec in enumerate(specs['books'],1):
        pdf=next(a.source.glob(spec['stem']+'*.pdf'));data=prepare(pdf,spec);packages.append(data)
        for kind,pages in [('copyright',spec['copyright_pages']),('toc',spec['toc_pages']),
                ('folios',data['audit']['folio_review']),('samples',sorted({x for s in spec['segments'] for x in [s['start'],s['end']]}))]:
            tasks[kind].extend(dict(book=n,pdf=str(pdf),page=x) for x in pages)
        print(json.dumps(dict(book=n,title=spec['metadata']['display_title'],toc=len(data['toc']),
            folios=len(data['audit']['folio_review']),issues=len(data['issues'])),ensure_ascii=False),flush=True)
    (a.output/'draft-packages.json').write_text(json.dumps(packages,ensure_ascii=False),encoding='utf8')
    (a.output/'issues.json').write_text(json.dumps([dict(book=i+1,title=p['metadata']['display_title'],issues=p['issues']) for i,p in enumerate(packages)],ensure_ascii=False,indent=2),encoding='utf8')
    (a.output/'qa-tasks.json').write_text(json.dumps(tasks,ensure_ascii=False,indent=2),encoding='utf8')
    if not a.render:return
    import pymupdf as fitz
    from PIL import Image,ImageDraw
    dest=a.output/'qa';dest.mkdir(exist_ok=True)
    for kind,items in tasks.items():
        w,h,count=(720,260,12) if kind=='folios' else (720,1060,4)
        for start in range(0,len(items),count):
            group=items[start:start+count];canvas=Image.new('RGB',(w*2,h*((len(group)+1)//2)),'white');draw=ImageDraw.Draw(canvas)
            for i,t in enumerate(group):
                with fitz.open(t['pdf']) as doc:
                    page=doc[t['page']-1];x=i%2*w;y=i//2*h;draw.text((x+5,y+5),f"B{t['book']:02} PDF {t['page']}",fill='black')
                    regions=[page.rect] if kind!='folios' else [fitz.Rect(0,0,page.rect.width,page.rect.height*.15),fitz.Rect(0,page.rect.height*.87,page.rect.width,page.rect.height)]
                    yy=y+26
                    for clip in regions:
                        pix=page.get_pixmap(matrix=fitz.Matrix(1.6,1.6),clip=clip,alpha=False)
                        im=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
                        im.thumbnail((w-10,h-30 if kind!='folios' else 108));canvas.paste(im,(x+5,yy));yy+=im.height+5
            canvas.save(dest/f'{kind}-{start//count+1:02}.png')


if __name__=='__main__':main()
