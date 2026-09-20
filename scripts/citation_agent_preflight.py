from __future__ import annotations

"""Production-data-free preflight for the citation export runtime."""

import argparse
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import citation_assistant as public_tasks


def _write_anonymous_probe_docx(path: Path) -> None:
    """Write a tiny standards-compliant DOCX using only the Python standard library."""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
    package_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
</w:styles>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>匿名预检文本：“社会生活在本质上是实践的。”</w:t></w:r></w:p>
    <w:p><w:r><w:t>CITATION PDF PREFLIGHT 2026</w:t></w:r></w:p>
    <w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
  </w:body>
</w:document>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/_rels/document.xml.rels", document_rels)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline citation Agent preflight.")
    parser.add_argument(
        "--require-pdf", action="store_true",
        help="Run a real anonymous DOCX-to-PDF conversion and validate the result.",
    )
    return parser.parse_args()


def main() -> int:
    args = _args()
    with tempfile.TemporaryDirectory(prefix="citation-agent-preflight-") as raw:
        temporary = Path(raw)
        paper = temporary / "anonymous.docx"
        _write_anonymous_probe_docx(paper)
        if args.require_pdf:
            office = public_tasks._soffice_binary()
            assert office, "LibreOffice executable is unavailable"
            pdf = temporary / "anonymous.pdf"
            public_tasks._convert_annotated_docx_to_pdf(paper, pdf)
            assert pdf.is_file() and pdf.stat().st_size >= 1000
            with public_tasks.fitz.open(pdf) as rendered:
                assert rendered.page_count >= 1
                text = "\n".join(page.get_text("text") for page in rendered)
            assert "CITATION PDF PREFLIGHT 2026" in text
            assert not list(temporary.glob(".lo-profile-*"))
            assert not list(temporary.glob(".lo-output-*"))
            print(f"citation PDF probe: PASS ({office})")

    print("citation Agent isolated preflight: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
