"""Render real scanned PDF pages to verify OCR highlights across page transforms."""
import io
import pytest
import fitz
from PIL import Image, ImageDraw
from _test_env import APPDATA
import app as app_module


@pytest.mark.parametrize('rotation',[0,90,180,270])
@pytest.mark.parametrize('cropped',[False,True])
@pytest.mark.parametrize('scale',[1,2])
def test_ocr_fallback_marks_correct_pixels_after_rotation_and_crop(tmp_path,monkeypatch,rotation,cropped,scale):
    scan=Image.new('RGB',(240,320),'white')
    ImageDraw.Draw(scan).line((40,73,180,73),fill='black',width=2)
    stream=io.BytesIO();scan.save(stream,format='PNG')
    pdf=tmp_path/'scan.pdf'
    with fitz.open() as doc:
        page=doc.new_page(width=240,height=320)
        page.insert_image(page.rect,stream=stream.getvalue())
        if cropped:page.set_cropbox(fitz.Rect(20,30,220,300))
        page.set_rotation(rotation)
        # A source scanner marker sits at these unrotated coordinates. Derive
        # the same normalized rendered-page box an offline OCR service returns.
        offset=page.cropbox_position
        native=fitz.Rect(40-offset.x,50-offset.y,180-offset.x,75-offset.y)
        rendered=native*page.rotation_matrix
        point=native.tl+(native.br-native.tl)*.5
        rendered_point=point*page.rotation_matrix
        normalized=(rendered.x0/page.rect.width,rendered.y0/page.rect.height,
                    rendered.x1/page.rect.width,rendered.y1/page.rect.height)
        doc.save(pdf)
    monkeypatch.setattr(app_module,'_resolve_pdf_path',lambda *a,**k:pdf)
    monkeypatch.setattr(app_module,'_locate_ocr_geometry_rects',lambda *a,**k:[normalized])
    monkeypatch.setattr(app_module,'PAGE_IMAGE_CACHE_DIR',tmp_path/'cache')
    monkeypatch.setattr(app_module,'PAGE_IMAGE_MIN_SCALE',scale)
    monkeypatch.setattr(app_module,'_render_profile',lambda _: {'display_min_px':0,'hard_max_scale':scale,
        'usm_gain':0,'usm_cap':0,'jpeg_quality':95,'tag':''})
    image=app_module._render_page_image_uncached('pdfs/scan.pdf',1,'测试高亮',scale)
    with Image.open(image) as result:
        rgb=result.convert('RGB').getpixel((round(rendered_point.x*scale),round(rendered_point.y*scale)))
        # The middle of the highlighted scanner region should be yellow. This
        # detects actual misplaced output, not merely a matrix round-trip.
        assert rgb[0]>220 and rgb[1]>190 and rgb[2]<210, (rotation,cropped,scale,rgb)
