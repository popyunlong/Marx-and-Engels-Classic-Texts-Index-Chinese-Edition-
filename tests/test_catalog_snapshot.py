import json
from pathlib import Path

import pytest

from scripts.snapshot_catalog import BATCH_BYTES, batches, capture


def test_batches_limit_file_count_and_bytes():
    files = {f"static_library/{index}.html": {"size": 1, "mtime_ns": index}
             for index in range(201)}
    grouped = list(batches(files))
    assert [len(group) for group in grouped] == [100, 100, 1]
    with pytest.raises(ValueError, match="single source"):
        list(batches({"static_library/large": {"size": BATCH_BYTES + 1,
                                                "mtime_ns": 0}}))


class FakeRemote:
    def __init__(self, fail=False):
        self.fail = fail
        self.payload = b"safe snapshot payload"
        self.files_meta = {"static_library/book/index.html": {
            "size": len(self.payload), "mtime_ns": 1}}
        self.runtime = {"ok": True, "app_release": {"id": "app1"},
                        "catalog_release": {"id": "legacy", "sha256": None}}
        self.counter = 0

    def terminate_active(self):
        pass

    def json(self, action):
        if action == "metrics":
            self.counter += 1000
            return {"p95": 0.01, "five_xx": 0,
                    "probes": {"/": {"status": 200, "seconds": 0.01}},
                    "cpu_total": self.counter, "cpu_iowait": self.counter // 100}
        database = {"sha256": "a" * 64, "toc_count": 1, "source_count": 1}
        common = {"runtime": self.runtime, "app": "/release/app",
                  "database": database, "files": self.files_meta,
                  "ledger_bytes": 12}
        if action == "metadata":
            database.update(toc_entries=[{
                "book": "B", "volume": 1, "source_file": "pdfs/b.pdf",
                "title": "T", "pdf_page": 1, "printed_page": "1",
                "level": 1, "kind": "body", "sort_order": 1}],
                sources=[{"book": "B", "volume": 1,
                          "source_file": "pdfs/b.pdf", "page_count": 1,
                          "first_page": 1, "last_page": 1}])
        return common

    def files(self, records, destination, monitor):
        if self.fail:
            raise RuntimeError("simulated transfer failure")
        for name in records:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.payload)


class SlowFinalRemote(FakeRemote):
    def json(self, action):
        value = super().json(action)
        if action == "metrics" and self.counter > 1000:
            value["p95"] = 0.2
        return value


def test_capture_is_verified_and_compressed_locally(tmp_path):
    output = tmp_path / "capture"
    snapshot = capture(output, FakeRemote(), baseline_seconds=0,
                       sample_seconds=3600)
    assert snapshot == output / "snapshot"
    assert (snapshot / "static_library/book/index.html").read_bytes() == b"safe snapshot payload"
    assert (output / "snapshot.tar.gz").is_file()
    manifest = json.loads((output / "snapshot_manifest.json").read_text(encoding="utf-8"))
    assert manifest["runtime"]["app_release"]["id"] == "app1"
    assert manifest["baseline"]["windows"] == 1


def test_failed_capture_removes_incomplete_output(tmp_path):
    output = tmp_path / "capture"
    with pytest.raises(RuntimeError, match="simulated"):
        capture(output, FakeRemote(fail=True), baseline_seconds=0,
                sample_seconds=3600)
    assert not output.exists()


def test_final_latency_regression_discards_capture(tmp_path):
    output = tmp_path / "capture"
    with pytest.raises(RuntimeError, match="latency"):
        capture(output, SlowFinalRemote(), baseline_seconds=0,
                sample_seconds=3600)
    assert not output.exists()


def test_output_must_be_new(tmp_path):
    with pytest.raises(ValueError, match="new directory"):
        capture(tmp_path, FakeRemote(), baseline_seconds=0,
                sample_seconds=3600)
