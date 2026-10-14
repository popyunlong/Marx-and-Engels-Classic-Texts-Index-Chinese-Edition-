"""Render the actual recommendation panel and styles with isolated status examples."""
from pathlib import Path
from flask import Flask, render_template_string
from werkzeug.serving import make_server

ROOT=Path(__file__).resolve().parents[2]
app=Flask(__name__)
source=(ROOT/'templates/control.html').read_text('utf8')
head=source[:source.index('</head>')+7]
start=source.index('<section class="card" id="book-recommendations">')
end=source.index('{% if control_mylib_enabled %}',start)
panel='{% if remote_admin %}'+source[start:end]


@app.get('/')
def page():
    rows=[]
    for n,state in enumerate(['','processing','failed','ready','published'],1):
        rows.append(dict(id=n,title=['待审核书籍','正文处理中','失败可重试','等待正式发布','已永久收录'][n-1],
            status='pending',user_email='reader@example.test',author='作者',original_filename='用户推荐书籍.pdf',
            byte_size=12000000,page_count=306,sha256='a'*64,created_at='2026-10-14',
            publication={} if not state else dict(state=state,completed_pages=150,total_pages=306,
                error='识别环境暂时不可用' if state=='failed' else '',book_key='public-book'),
            publication_label=state or '等待审核'))
    return render_template_string(head+'<body><main class="page">'+panel+'</main></body></html>',
        title='荐书后台预览',app_name='荐书验收',remote_admin=True,csrf_token='fixture-csrf',
        book_recommendations=rows,book_recommendation_storage_enabled=True,
        url_for=lambda endpoint,**kw:('/v2/read?book='+kw['book']) if endpoint=='layout_read' else
            '/admin/book-recommendations/'+str(kw['recommendation_id'])+'/'+kw.get('action','download'))


if __name__=='__main__':
    server=make_server('127.0.0.1',0,app,threaded=True)
    print(f'READY=http://127.0.0.1:{server.server_port}',flush=True)
    server.serve_forever()
