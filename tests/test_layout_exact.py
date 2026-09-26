from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import runtime_env
import build_index
from layout_exact import LayoutIndex, RUN, VERSION, volume_fingerprint


class _Corpus:
    def __init__(self, volume):
        self.volume = volume

    def get_volume_by_source_file(self, source_file):
        if source_file == self.volume.source_file:
            return self.volume
        return None

    @staticmethod
    def _chapter_segments(_volume):
        return []


def test_layout_index_uses_configured_shared_pdf_root(tmp_path, monkeypatch):
    pdf_root = tmp_path / "shared-pdfs"
    pdf_path = pdf_root / "book.pdf"
    pdf_path.parent.mkdir()
    pdf_path.write_bytes(b"shared pdf")
    monkeypatch.setattr(runtime_env, "PDF_ROOT", pdf_root)

    volume = SimpleNamespace(source_file="pdfs/book.pdf", pages=[])
    corpus = _Corpus(volume)
    projection_id = "a" * 64
    text_bytes = "x".encode("utf-32-le")
    run_bytes = RUN.pack(0, 0, 1)
    index_root = tmp_path / "layout-index"
    index_root.mkdir()
    (index_root / f"{projection_id}.text").write_bytes(text_bytes)
    (index_root / f"{projection_id}.runs").write_bytes(run_bytes)
    stat = pdf_path.stat()
    manifest = {
        "version": VERSION,
        "volumes": {
            volume.source_file: {
                "id": projection_id,
                "fingerprint": volume_fingerprint(corpus, volume),
                "pdf_stat": [stat.st_size, stat.st_mtime_ns],
                "text_sha256": hashlib.sha256(text_bytes).hexdigest(),
                "runs_sha256": hashlib.sha256(run_bytes).hexdigest(),
                "removed": [],
            }
        },
    }
    (index_root / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    index = LayoutIndex(corpus, root=index_root)
    try:
        assert index.error == ""
        assert list(index.projections) == [volume.source_file]
    finally:
        for projection in index.projections.values():
            projection.close()

    # A stale host EnvironmentFile must not override the immutable release's
    # configured index when the candidate service starts.
    config = tmp_path / "config"
    config.mkdir()
    (config / "layout_exact_runtime.json").write_text(
        json.dumps({"directory": str(index_root)}), encoding="utf-8"
    )
    monkeypatch.setattr(build_index, "_EXEDIR", tmp_path)
    monkeypatch.setenv("MARX_LAYOUT_EXACT_DIR", str(tmp_path / "old-index"))
    candidate_index = LayoutIndex(corpus)
    try:
        assert candidate_index.error == ""
        assert list(candidate_index.projections) == [volume.source_file]
    finally:
        for projection in candidate_index.projections.values():
            projection.close()
