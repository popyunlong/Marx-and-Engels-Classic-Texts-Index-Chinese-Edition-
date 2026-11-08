"""Package the four WorkBuddy connector files; never include user credentials."""
import argparse
from pathlib import Path
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=root / "static/agent-api/workbuddy-connector.zip")
    args = parser.parse_args()
    source = root / "docs/agent-api/workbuddy"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("connector-meta.json", "mcp.json", "token-schema.json", "icon.svg"):
            # Fixed metadata makes repeated releases reproducible.
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 7, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, (source / name).read_bytes())
    print(args.output)


if __name__ == "__main__":
    main()
