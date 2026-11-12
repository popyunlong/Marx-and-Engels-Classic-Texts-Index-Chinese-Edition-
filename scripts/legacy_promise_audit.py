"""Read-only July legacy cohort/cost report; never imports app or initializes stores."""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import legacy_ai


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args()
    with sqlite3.connect(args.database.resolve().as_uri()+"?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        print(json.dumps(legacy_ai.report(conn, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
