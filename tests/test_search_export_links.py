"""The downloadable HTML and Word files must advertise usable reader links."""
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT

from _test_env import APPDATA as _TEST_APPDATA  # noqa: F401
import app as site
import search_exports


class SourceLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and values.get("class") == "source":
            self.links.append(values["href"])


def test_current_wenji_and_quanji_links_match_across_formats(tmp_path):
    # Up to ten real "健康" pages from each live volume, recorded 2026-10-10.
    source_pages = [
        ("pdfs/文集/马克思恩格斯文集[第1卷]马克思恩格斯1843-1848年著作.pdf",
         [150, 394, 410, 430, 431, 432, 435, 436, 439, 440]),
        ("pdfs/马克思恩格斯全集（第二版）/马克思恩格斯全集（第二版）第44卷（资本论第一卷）.pdf",
         [73, 212, 214, 216, 283, 298, 305, 311, 312, 314]),
    ]
    job = {"base_url": "https://mazhuzuojiansuo.com", "query_text": "健康",
           "search_mode": "exact"}
    hits = []
    for number, (source, pages) in enumerate(source_pages, 1):
        assert source in site.ALLOWED_SOURCE_FILES
        for page in pages:
            hit = {"book": "文集" if number == 1 else "全集二版",
                   "volume": 1 if number == 1 else 44,
                   "source_file": source, "pdf_pages": [page],
                   "printed_pages": [str(page)], "context": "人的[[H]]健康[[/H]]",
                   "section_title": "资本的利润", "citation": "人民出版社，2009年。"}
            hit["viewer_url"] = site._search_export_viewer_url(job, hit)
            hits.append(hit)

    meta = {"title": "健康引文检索汇编", "query": "健康", "citation_label": "GB/T 7714",
            "citation_style": "gbt7714", "total": len(hits), "start_index": 1}
    html_path, docx_path = tmp_path / "export.html", tmp_path / "export.docx"
    search_exports._render_html(html_path, hits, meta)
    search_exports._render_docx(docx_path, hits, meta)
    parser = SourceLinks()
    parser.feed(html_path.read_text(encoding="utf-8"))
    document = Document(docx_path)
    docx_links = [rel.target_ref for rel in document.part.rels.values()
                  if rel.reltype == RT.HYPERLINK]
    assert parser.links == docx_links == [hit["viewer_url"] for hit in hits]
    expected = [(source, page) for source, pages in source_pages for page in pages]
    for link, (source, page) in zip(parser.links, expected):
        parts = urlsplit(link)
        assert (parts.scheme, parts.netloc, parts.path) == ("https", "mazhuzuojiansuo.com", "/viewer")
        query = parse_qs(parts.query)
        assert query["file"] == [source]
        assert query["page"] == [str(page)]
        assert query["q"] == ["健康"]


def test_removed_source_does_not_create_dead_or_unsafe_link(tmp_path):
    hit = {"book": "文集", "volume": 1, "source_file": "pdfs/old/removed.pdf",
           "pdf_pages": [1], "context": "健康", "citation": "引文出处"}
    job = {"base_url": "https://mazhuzuojiansuo.com", "query_text": "健康",
           "search_mode": "exact"}
    assert site._search_export_viewer_url(job, hit) == ""
    hit["viewer_url"] = ""
    meta = {"title": "健康引文检索汇编", "query": "健康", "citation_label": "GB/T 7714",
            "citation_style": "gbt7714", "total": 1, "start_index": 1}
    path = tmp_path / "export.html"
    search_exports._render_html(path, [hit], meta)
    parser = SourceLinks()
    parser.feed(path.read_text(encoding="utf-8"))
    assert not parser.links
    assert "引文出处" in path.read_text(encoding="utf-8")
