"""New coverage for the browser's CRLF serialization of complete editor forms."""
from membership_refresh_fixture import A
import admin_store as S
import member_copy as C
from test_membership_admin import admin_login, isolation


def test_complete_multiline_form_roundtrip_and_real_conflict():
    defaults=A.get_site_text_map()
    group=C.editor(defaults)[0]
    data={'group':group['id'],'action':'save'}
    for key in group['keys']:
        data['original__'+key]=defaults[key].replace('\n','\r\n')
        data['site_text__'+key]=defaults[key].replace('\n','\r\n')
    key='pricing.plan.support_plus_20.features'
    data['site_text__'+key]='第一行\r\n第二行'
    with A.app.test_client() as client:
        headers=admin_login(client)
        assert client.post('/admin/membership-copy',headers=headers,data=data).status_code==302
        assert S.get_setting('site_texts')[key]=='第一行\n第二行'
        # A different editor really did change the text: preserve that edit.
        data['site_text__'+key]='不应覆盖'
        assert client.post('/admin/membership-copy',headers=headers,data=data).status_code==302
        assert S.get_setting('site_texts')[key]=='第一行\n第二行'
        page=client.get('/admin/members').get_data(as_text=True)
        assert '这些文案已被另一窗口修改' in page
