"""First acceptance of newly upgraded libraries and the restricted OCR decoder."""
from types import SimpleNamespace
import importlib.metadata
import io
from pathlib import Path

import pytest
from _test_env import APPDATA  # noqa: F401


def test_new_crypto_wheel_rsa2_sign_verify_and_tamper():
    assert importlib.metadata.version("cryptography") == "50.0.2"
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from alipay import AlipayClient
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    client = AlipayClient(SimpleNamespace(enabled=True, charset="utf-8", app_private_key=private, alipay_public_key=public))
    payload = {"out_trade_no": "isolated-fixture", "total_amount": "1.00", "subject": "验证"}
    signature = client.sign(payload)
    assert client.verify(payload, signature)
    assert not client.verify(dict(payload, total_amount="100.00"), signature)
    assert not client.verify(payload, "invalid")


def test_new_lxml_defaults_and_application_parser_do_not_expand_local_entities(tmp_path):
    assert importlib.metadata.version("lxml") == "6.1.3"
    from lxml import etree
    from citation_assistant import _safe_xml_parser
    marker = tmp_path / "marker.txt"
    marker.write_text("PRIVATE_TEST_MARKER")
    xml = f'<!DOCTYPE a [<!ENTITY x SYSTEM "{marker.as_uri()}">]><a>&x;</a>'.encode()
    for parser in (etree.ETCompatXMLParser(), _safe_xml_parser()):
        try:
            root = etree.fromstring(xml, parser=parser)
            assert b"PRIVATE_TEST_MARKER" not in etree.tostring(root)
        except etree.XMLSyntaxError:
            pass
    try:
        nodes = list(etree.iterparse(io.BytesIO(xml)))
        assert all(b"PRIVATE_TEST_MARKER" not in etree.tostring(node) for _, node in nodes)
    except etree.XMLSyntaxError:
        pass


def test_native_pdf_image_decodes_without_pillow_plugin_probe(tmp_path, monkeypatch):
    import fitz
    from PIL import Image
    from ingestion.free_ocr import render
    pix = fitz.Pixmap(fitz.csRGB, (0, 0, 8, 8), 0)
    pix.clear_with(180)
    path = tmp_path / "synthetic-large-coordinate-page.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=3000, height=3000)
        page.insert_image(page.rect, stream=pix.tobytes("png"))
        doc.save(path)
    monkeypatch.setattr(Image, "open", lambda *a, **k: pytest.fail("Pillow automatic decoder reached"))
    png = render(path, 1)
    output = fitz.Pixmap(png)
    assert (output.width, output.height) == (8, 8)
    assert b"native_scan_pixels" in png


def test_ocr_accepts_only_internal_png():
    import fitz
    from PIL import UnidentifiedImageError
    from ingestion.free_ocr import recognize
    pytest.importorskip("numpy")
    pix = fitz.Pixmap(fitz.csRGB, (0, 0, 8, 8), 0)
    pix.clear_with(180)
    ocr = lambda _: SimpleNamespace(txts=[], scores=None, boxes=None)
    with pytest.raises(UnidentifiedImageError):
        recognize(ocr, pix.tobytes("jpeg"))
    result = recognize(ocr, pix.tobytes("png"))
    assert result["width"] == 8 and result["height"] == 8
