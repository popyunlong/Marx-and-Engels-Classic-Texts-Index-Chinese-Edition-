from __future__ import annotations

from app import DEPLOYMENT, run_waitress


def main() -> None:
    if not DEPLOYMENT.is_server:
        raise SystemExit("Set APP_MODE=server before running serve.py.")
    run_waitress()


if __name__ == "__main__":
    main()
