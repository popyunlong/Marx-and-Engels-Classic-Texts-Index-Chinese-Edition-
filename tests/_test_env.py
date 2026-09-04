"""Shared process-wide environment for tests that import application stores."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile


# Store modules cache their database paths at import time.  A single isolated
# directory prevents pytest collection order from splitting schemas across
# several databases while still keeping real user data completely untouched.
APPDATA = tempfile.mkdtemp(prefix="marx-search-tests-")
atexit.register(lambda: shutil.rmtree(APPDATA, ignore_errors=True))

os.environ["APPDATA"] = APPDATA
os.environ["APP_MODE"] = "server"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["TURNSTILE_ENABLED"] = "0"
os.environ.setdefault("ZPAY_PID", "test-pid")
os.environ.setdefault("ZPAY_KEY", "test-secret")
