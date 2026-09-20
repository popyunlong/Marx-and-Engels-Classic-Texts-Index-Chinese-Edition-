from __future__ import annotations

"""Memory-only bootstrap for the explicitly approved local Agent preview.

This helper is not used by production services.  It temporarily reuses the local
site's DeepSeek credential without printing it or copying it to another file, then
replaces itself with the isolated queue-only Agent worker.
"""

import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    from ai import load_ai_config

    config = load_ai_config()
    host = (urlparse(str(config.base_url)).hostname or "").lower()
    if str(config.provider).lower() != "deepseek" or host != "api.deepseek.com":
        raise SystemExit("local preview requires the existing DeepSeek configuration")
    if len(str(config.api_key or "").strip()) < 12:
        raise SystemExit("local preview DeepSeek credential is unavailable")

    queue_path = Path(
        os.environ.get("CITATION_AGENT_QUEUE_PATH")
        or (Path(os.environ.get("LOCALAPPDATA") or PROJECT_ROOT) / "marx-citation-agent" / "queue.sqlite3")
    )
    child_env = os.environ.copy()
    child_env.update(
        {
            "CITATION_AGENT_TEST_MODE": "admin_live",
            "CITATION_AGENT_QUEUE_PATH": str(queue_path),
            "CITATION_AGENT_API_URL": "https://api.deepseek.com",
            "CITATION_AGENT_ALLOWED_HOSTS": "api.deepseek.com",
            "CITATION_AGENT_MODEL": "deepseek-v4-flash",
            "CITATION_AGENT_THINKING": "enabled",
            "CITATION_AGENT_API_KEY": str(config.api_key).strip(),
            "CITATION_AGENT_REQUIRE_PROXY": "1",
            "HTTPS_PROXY": "http://127.0.0.2:18080",
        }
    )
    worker = PROJECT_ROOT / "scripts" / "citation_agent_worker.py"
    worker_args = [sys.executable, str(worker), "--poll-seconds", "1"]
    if "--once" in sys.argv[1:]:
        worker_args.append("--once")
    if os.name == "nt":
        # The Windows py.exe launcher does not reliably preserve an os.execve
        # replacement's lifetime.  Keep this tiny bootstrap waiting for the
        # queue-only child, while immediately dropping its own key references.
        process = subprocess.Popen(worker_args, cwd=str(PROJECT_ROOT), env=child_env)
        child_env["CITATION_AGENT_API_KEY"] = ""
        config = None
        raise SystemExit(process.wait())
    os.execve(sys.executable, worker_args, child_env)


if __name__ == "__main__":
    main()
