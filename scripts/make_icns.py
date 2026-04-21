from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from PIL import Image


ICONSET_FILES = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}


def build_icns(source: Path, target: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"Icon source not found: {source}")

    target.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source) as base_image:
        image = base_image.convert("RGBA")

        with tempfile.TemporaryDirectory(prefix="iconset-") as tmp_dir:
            iconset_dir = Path(tmp_dir) / "AppIcon.iconset"
            iconset_dir.mkdir(parents=True, exist_ok=True)

            for filename, size in ICONSET_FILES.items():
                resized = image.resize((size, size), Image.Resampling.LANCZOS)
                resized.save(iconset_dir / filename, format="PNG")

            subprocess.run(
                ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(target)],
                check=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert the project ICO file into a macOS ICNS icon.")
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()

    build_icns(args.source, args.target)


if __name__ == "__main__":
    main()
