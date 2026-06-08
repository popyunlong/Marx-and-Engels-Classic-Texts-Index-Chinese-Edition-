from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_env import APP_VERSION, compute_sha256


def write_release_metadata(data_dir: Path, data_version: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "corpus.sqlite"
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    digest = compute_sha256(db_path)
    (data_dir / "corpus.sqlite.sha256").write_text(f"{digest}\n", encoding="utf-8")
    payload = {
        "app_version": APP_VERSION,
        "data_version": data_version,
        "db_sha256": digest,
    }
    (data_dir / "release.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Target data directory that contains corpus.sqlite",
    )
    parser.add_argument(
        "--data-version",
        default=APP_VERSION,
        help="Human-readable data version written into release.json",
    )
    args = parser.parse_args()
    write_release_metadata(Path(args.data_dir).resolve(), args.data_version)


if __name__ == "__main__":
    main()
