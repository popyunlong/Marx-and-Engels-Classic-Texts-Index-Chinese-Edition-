"""Carry pagination evidence into candidate databases without changing text."""
import json
from .page_tokens import parse_label

VERIFIED = {"ocr_number_with_sequence", "ocr_roman_with_sequence", "image_review",
            "verified_toc_landing", "native_and_ocr", "ocr_margin", "native_margin"}
INFERRED = {"bounded_sequence_interpolation", "interpolated_between_consistent_anchors"}


def persist(conn, source, pages):
    conn.execute("CREATE TABLE IF NOT EXISTS page_label_evidence(source_file TEXT NOT NULL,pdf_page INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(source_file,pdf_page))")
    count = 0
    for row in pages:
        evidence = row.get("label_evidence") or row.get("mapping_evidence") or {}
        basis = evidence.get("method") or row.get("mapping_basis") or evidence.get("basis")
        if not basis:
            continue
        old = conn.execute("SELECT payload FROM page_label_evidence WHERE source_file=? AND pdf_page=?",(source,row["page"])).fetchone()
        if old and json.loads(old[0]).get("status") in {"manual", "verified", "inferred"}:
            continue
        parsed = parse_label(row.get("label"))
        status = "verified" if basis in VERIFIED else "inferred" if basis in INFERRED else "needs_review"
        label = parsed[0] if parsed and status != "needs_review" else None
        payload = {"printed_page":label,"status":status,"basis":basis,"evidence":evidence,
                   "segment_id":row.get("segment_id", ""),"segment_title":row.get("segment_title", "")}
        conn.execute("INSERT OR REPLACE INTO page_label_evidence VALUES(?,?,?)",(source,row["page"],json.dumps(payload,ensure_ascii=False)))
        count += 1
    return count
