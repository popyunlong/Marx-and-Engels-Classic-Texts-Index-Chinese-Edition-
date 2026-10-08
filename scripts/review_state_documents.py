"""Render bounded QA sheets from original PDFs, never from OCR text."""
import argparse
import json
from pathlib import Path
import fitz
from PIL import Image,ImageDraw,ImageFont


def sheets(source,audit,output,kind):
    output.mkdir(parents=True,exist_ok=True)
    entries=json.loads((audit/'inventory.json').read_text('utf-8'));tasks=[]
    for n,e in enumerate(entries,1):
        d=json.loads((audit/e['audit']).read_text('utf-8'))
        if '13349425' in d['pdf']:continue
        if kind=='folios':numbers=[x['page'] for x in d['folio_review']]
        elif kind=='toc':numbers=d['toc_pages']
        elif kind=='titles':numbers=sorted({x['pdf_page'] for x in d['issues'] if x.get('pdf_page')})
        elif kind=='front':numbers=sorted({1,2,3,4} | ({437,438,440,441} if '12960440' in d['pdf'] else set()))
        elif kind=='details':
            numbers={1:[84,95],2:[517,518],9:[16,17,18],11:[19,20,21],12:[17,18,19],17:[35,84,229]}.get(n,[])
            if n==18:numbers=[r['page'] for r in d['pages'] if r['kind']=='body' and any(b['kind']=='doc_title' for b in r['blocks'])]
        else:numbers=sorted({d['body_start']+round(i*(d['body_end']-d['body_start'])/9) for i in range(10)})
        for page in numbers:tasks.append(dict(book=n,file=d['pdf'],page=page,expected=page-d['offset']))
    font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',17)
    width,height=(650,220) if kind=='folios' else (560,850)
    per=16 if kind=='folios' else 6
    log=[]
    for batch in range(0,len(tasks),per):
        chosen=tasks[batch:batch+per];canvas=Image.new('RGB',(width*2,height*((len(chosen)+1)//2)), 'white')
        draw=ImageDraw.Draw(canvas)
        for i,t in enumerate(chosen):
            x,y=(i%2)*width,(i//2)*height
            draw.text((x+5,y+3),f"B{t['book']:02d} PDF {t['page']} / printed {t['expected']}",font=font,fill='black')
            with fitz.open(source/t['file']) as doc:
                p=doc[t['page']-1]
                if kind=='folios':
                    regions=[fitz.Rect(0,0,p.rect.width,p.rect.height*.16),fitz.Rect(0,p.rect.height*.91,p.rect.width,p.rect.height)]
                else:regions=[p.rect]
                at=y+30
                for rect in regions:
                    pix=p.get_pixmap(matrix=fitz.Matrix(1.5,1.5),clip=rect)
                    im=Image.frombytes('RGB',[pix.width,pix.height],pix.samples)
                    scale=min((width-12)/im.width,(height-35)/im.height)
                    im=im.resize((int(im.width*scale),int(im.height*scale)))
                    canvas.paste(im,(x+6,at));at+=im.height+3
            log.append(dict(t,sheet=f'{kind}-{batch//per+1:02d}.png'))
        canvas.save(output/f'{kind}-{batch//per+1:02d}.png')
    (output/f'{kind}-pages.json').write_text(json.dumps(log,ensure_ascii=False,indent=2),encoding='utf-8')
    print(kind,len(tasks),'pages', (len(tasks)+per-1)//per,'sheets')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('kind',choices=['folios','toc','titles','front','samples','details']);a=p.parse_args()
    sheets(Path('D:/claudecode文件夹/【增强】马恩《文集》《全集》检索/pdfs/国家文献'),
        Path('D:/CodexData/outputs/state-documents-20261008/audit'),Path('D:/CodexData/temp/state-documents-qa'),a.kind)
