"""Coordinator-only, locked one-time public sample preparation. Defaults to read-only."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    import research_updates as r
    import research_preferences as preferences
    if not args.apply:
        print(json.dumps({'database': str(r.DB_PATH), 'action': 'prepare fixed public sample', 'apply': False}))
        return
    # This is a separate release-coordinator transaction, not nested in release.ps1.
    if os.name != 'posix':
        raise RuntimeError('Production preparation must run on the release host under its release lock.')
    import fcntl
    with open('/run/lock/marx-search-release.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = r.DB_PATH.parent / ('research-before-membership-refresh-' + stamp + '.sqlite3')
        if not r.DB_PATH.is_file() or backup.exists():
            raise RuntimeError('Expected existing journal database and a fresh backup path.')
        with sqlite3.connect(r.DB_PATH) as source, sqlite3.connect(backup) as target:
            source.backup(target)
        r.init_db()
        created = preferences.prepare_sample()
        print(json.dumps({'created': created, 'backup': str(backup), 'sample_items': len((preferences.sample() or {}).get('articles', []))}))


if __name__ == '__main__':
    main()
