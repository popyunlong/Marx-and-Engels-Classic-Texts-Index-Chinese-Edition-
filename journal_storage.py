from __future__ import annotations

"""Journal-only persistent storage.

Production mounts the server data disk at ``/var/www/.marx_search_full/journal``
and sets ``MARX_JOURNAL_DATA_ROOT`` to that mount point.  Keeping this small
module dependency-free makes every journal process agree on the same paths.
"""

import os
from pathlib import Path

from runtime_env import APPDATA_DIR


JOURNAL_DATA_ROOT = Path(
    os.environ.get("MARX_JOURNAL_DATA_ROOT")
    or (APPDATA_DIR / "journal")
).expanduser()
JOURNAL_DB_DIR = JOURNAL_DATA_ROOT / "db"
JOURNAL_DB_PATH = JOURNAL_DB_DIR / "journal.sqlite3"
JOURNAL_ARTICLES_DIR = JOURNAL_DATA_ROOT / "articles"
JOURNAL_ISSUES_DIR = JOURNAL_DATA_ROOT / "issues"
JOURNAL_TMP_DIR = JOURNAL_DATA_ROOT / "tmp"
JOURNAL_LOG_DIR = JOURNAL_DATA_ROOT / "logs"
JOURNAL_BACKUP_DIR = JOURNAL_DATA_ROOT / "backups"


def _enabled(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def ensure_journal_storage() -> Path:
    """Create journal directories and fail closed if production lost its data mount."""
    root = JOURNAL_DATA_ROOT.resolve()
    if _enabled("MARX_JOURNAL_REQUIRE_DATA_DISK"):
        # A bind mount is a mount point on Linux.  The explicit check prevents a
        # missing data disk from silently filling the system volume.
        if os.name != "posix" or not os.path.ismount(root):
            raise RuntimeError(
                f"journal data disk is not mounted at {root}; refusing to use the system disk"
            )
    for path in (
        root,
        JOURNAL_DB_DIR,
        JOURNAL_ARTICLES_DIR,
        JOURNAL_ISSUES_DIR,
        JOURNAL_TMP_DIR,
        JOURNAL_LOG_DIR,
        JOURNAL_BACKUP_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                path.chmod(0o700)
            except OSError:
                pass
    return root


def journal_storage_status() -> dict[str, object]:
    root = JOURNAL_DATA_ROOT.resolve()
    return {
        "root": str(root),
        "database": str(JOURNAL_DB_PATH.resolve()),
        "mounted": bool(os.name == "posix" and os.path.ismount(root)),
        "mount_required": _enabled("MARX_JOURNAL_REQUIRE_DATA_DISK"),
    }
