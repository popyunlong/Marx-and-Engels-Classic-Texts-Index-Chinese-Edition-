#!/usr/bin/env python3
"""Build a protected Kant/Feuerbach corpus candidate from a baseline copy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

try:  # package import in tests / direct script execution in operations
    from . import build_philosophy_toc, build_scan_volumes, ocr_philosophy_mimo
    from .philosophy_catalog import ROOT, load_volumes
    from .philosophy_corpus_gate import evaluate
except ImportError:  # pragma: no cover
    import build_philosophy_toc
    import build_scan_volumes
    import ocr_philosophy_mimo
    from philosophy_catalog import ROOT, load_volumes
    from philosophy_corpus_gate import evaluate


OUTPUT_ROOT = ROOT / "data" / "kant_feuerbach"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_baseline(path: Path) -> str:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not path.is_file() or not sidecar.is_file():
        raise FileNotFoundError(f"baseline DB/hash pair is incomplete: {path}")
    expected = sidecar.read_text(encoding="utf-8").strip().split()[0].lower()
    actual = _sha256(path)
    if actual != expected:
        raise ValueError(f"baseline SHA-256 mismatch: {actual} != {expected}")
    return actual


def _install_scan_contract(db_path: Path) -> None:
    build_scan_volumes.BUILD_DB_PATH = db_path
    build_scan_volumes.HASH_PATH = db_path.with_suffix(db_path.suffix + ".sha256")
    build_scan_volumes.VOLUMES = [
        {
            "id": item.id,
            "book": item.book,
            "volume": item.volume,
            "source_file": item.file,
            "sidecar": f"data/kant_feuerbach/{item.id}.jsonl",
            "toc": "refresh_printed",
            # The 2022 Feuerbach edition prints folios on the same short
            # running-head line (e.g. "2 1841年初版序言" / "序言 3").
            # Reuse the existing tightly bounded inline-header detector only
            # for these pinned volumes; Kant and every existing corpus stay on
            # their current page-number rules.
            "header_page_numbers": item.file.startswith("pdfs/费尔巴哈/"),
        }
        for item in load_volumes()
    ]


def _run_scan_injection(db_path: Path, only: list[str]) -> None:
    _install_scan_contract(db_path)
    old_argv = sys.argv
    try:
        sys.argv = ["build_scan_volumes.py", *( ["--only", *only] if only else [] )]
        build_scan_volumes.main()
    finally:
        sys.argv = old_argv


def build_candidate(*, baseline: Path, output: Path, only: list[str]) -> dict:
    if baseline.resolve() == output.resolve():
        raise ValueError("baseline and output must be different paths")
    if output.exists() or output.with_suffix(output.suffix + ".sha256").exists():
        raise FileExistsError(f"refusing to overwrite candidate: {output}")
    blank_audit = ocr_philosophy_mimo.audit_reviewed_blank_pages()
    if not blank_audit["passed"]:
        raise RuntimeError("reviewed blank evidence gate rejected candidate")
    ocr_audit = ocr_philosophy_mimo.audit_all_pages()
    if not ocr_audit["passed"]:
        raise RuntimeError("OCR completeness/evidence gate rejected candidate")
    baseline_sha = _verify_baseline(baseline)
    baseline_mode = stat.S_IMODE(baseline.stat().st_mode)
    incoming = output.with_name(f"{output.name}.incoming-{os.getpid()}")
    incoming_hash = incoming.with_suffix(incoming.suffix + ".sha256")
    if incoming.exists() or incoming_hash.exists():
        raise FileExistsError(incoming)
    incoming.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(baseline, incoming)
    # The protected baseline is deliberately 0444.  copy2 preserves that mode,
    # so grant write permission only to this owned candidate while SQLite is
    # building it, then restore the protected mode before promotion.
    incoming.chmod(baseline_mode | stat.S_IWUSR)
    incoming_hash.write_text(baseline_sha + "\n", encoding="utf-8")
    try:
        _run_scan_injection(incoming, only)
        toc_args = ["--db", str(incoming)]
        if only:
            toc_args.extend(["--only", *only])
        toc_status = build_philosophy_toc.main(toc_args)
        if toc_status:
            raise RuntimeError(f"TOC builder rejected candidate (exit {toc_status})")
        report = evaluate(baseline, incoming)
        if not report["passed"]:
            raise RuntimeError("corpus protection gate rejected candidate: " + "; ".join(report["errors"]))
        final_sha = _sha256(incoming)
        incoming_hash.write_text(final_sha + "\n", encoding="utf-8")
        incoming.chmod(baseline_mode)
        os.replace(incoming, output)
        os.replace(incoming_hash, output.with_suffix(output.suffix + ".sha256"))
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        (OUTPUT_ROOT / "corpus_gate.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return {**report, "candidate_sha256": final_sha}
    except Exception:
        print(f"failed candidate retained for inspection: {incoming}", file=sys.stderr)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args(argv)
    try:
        report = build_candidate(
            baseline=args.baseline.resolve(), output=args.output.resolve(), only=list(args.only)
        )
    except Exception as exc:  # noqa: BLE001 - CLI reports retained candidate and fails closed
        print(json.dumps({"passed": False, "errors": [f"{type(exc).__name__}: {exc}"]}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
