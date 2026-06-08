from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_env import build_activation_code


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fingerprint", help="Machine fingerprint shown in the app")
    args = parser.parse_args()
    print(build_activation_code(args.fingerprint))


if __name__ == "__main__":
    main()
