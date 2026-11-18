"""The new release preflight is offline, isolated, and fails closed on stale routing."""
from pathlib import Path
import shutil
import subprocess

import pytest

import runtime_env
from scripts import citation_agent_test_worker as bridge
from scripts import citation_agent_worker as model


def test_stale_mimo_route_cannot_open_the_production_queue(monkeypatch):
    monkeypatch.setattr(model, "_production", False)
    monkeypatch.setattr(model.sys, "argv", ["worker", "--production"])
    monkeypatch.setenv("CITATION_AGENT_TEST_MODE", "admin_live")
    monkeypatch.setenv("CITATION_AGENT_API_URL", "https://api.xiaomimimo.com")
    monkeypatch.setenv("CITATION_AGENT_ALLOWED_HOSTS", "api.xiaomimimo.com")
    monkeypatch.setenv("CITATION_AGENT_MODEL", "mimo-v2.5")
    monkeypatch.setattr(model, "run", lambda **_: pytest.fail("production queue was opened"))
    with pytest.raises(SystemExit, match="unapproved_production_model"):
        model.main()


def test_deepseek_uses_only_systemd_credential_and_fixed_proxy(monkeypatch, tmp_path):
    monkeypatch.setattr(model, "_production", True)
    monkeypatch.setenv("CITATION_AGENT_API_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("CITATION_AGENT_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("CITATION_AGENT_ALLOWED_HOSTS", "api.deepseek.com")
    monkeypatch.setenv("CITATION_AGENT_API_KEY", "obsolete-key-should-not-be-used")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    with pytest.raises(model.AgentWorkerError, match="missing_citation_deepseek_credential"):
        model._endpoint()
    (tmp_path / "citation-deepseek").write_text("deepseek-fixture-secret", encoding="utf-8")
    assert model._endpoint() == (
        "https://api.deepseek.com/v1/chat/completions", "deepseek-fixture-secret", "deepseek-v4-flash",
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.2:18080")
    monkeypatch.setenv("CITATION_AGENT_REQUIRE_PROXY", "yes")
    monkeypatch.setenv("NO_PROXY", "api.deepseek.com")
    monkeypatch.setenv("no_proxy", "*")
    model._opener()
    assert "NO_PROXY" not in model.os.environ and "no_proxy" not in model.os.environ
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:18081")
    with pytest.raises(model.AgentWorkerError, match="unapproved_production_proxy"):
        model._opener()


def test_startup_probe_checks_isolated_store_before_corpus_or_queue(monkeypatch):
    home = Path("/run/marx-citation-startup-fixture").resolve()
    monkeypatch.setenv("CITATION_STARTUP_PROBE_HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(runtime_env, "APPDATA_DIR", home.parent / "other-db")
    events = []
    monkeypatch.setattr(bridge, "_initialize_worker", lambda: events.append("initialize"))
    monkeypatch.setattr(bridge.tasks, "claim_next_job", lambda *a, **k: pytest.fail("claimed a job"))
    monkeypatch.setattr(bridge.citation_agent_queue, "write_worker_ready", lambda: pytest.fail("published a heartbeat"))
    with pytest.raises(SystemExit, match="access live application data"):
        bridge._check_startup()
    assert events == []
    monkeypatch.setattr(runtime_env, "APPDATA_DIR", home / ".marx_search_full")
    bridge._check_startup()
    assert events == ["initialize"]


def test_corpus_replaced_during_cold_start_never_unlocks_queue(monkeypatch):
    monkeypatch.setattr(bridge, "corpus", None)
    versions = iter([(1, 1, 100, 1), (1, 2, 100, 2)])
    monkeypatch.setattr(bridge, "_corpus_file_fingerprint", lambda: next(versions))
    monkeypatch.setattr(bridge.runtime, "load_corpus", object)
    monkeypatch.setattr(bridge.runtime, "corpus_sha256", lambda: "irrelevant-generation")
    monkeypatch.setattr(bridge.runtime, "template_version", lambda: "fixture-template")
    monkeypatch.setattr(bridge.tasks, "init_db", lambda: None)
    monkeypatch.setattr(bridge.tasks, "recover_jobs_for_loaded_runtime", lambda *a, **kw: {"analysis": 0, "export": 0})
    monkeypatch.setattr(bridge.tasks, "recover_pdf_position_failures", lambda *a, **kw: 0)
    monkeypatch.setattr(bridge.tasks, "claim_next_job", lambda *a, **kw: pytest.fail("claimed a job"))
    monkeypatch.setattr(bridge.citation_agent_queue, "write_worker_ready", lambda: pytest.fail("published a heartbeat"))
    with pytest.raises(RuntimeError, match="citation corpus changed"):
        bridge.run(once=True)


def test_candidate_preflight_blocks_on_model_and_runs_cold_start_with_bounded_resources(tmp_path):
    git = shutil.which("git")
    git_bash = Path(git).parents[1] / "bin/bash.exe" if git else None
    bash = str(git_bash) if git_bash and git_bash.is_file() else shutil.which("bash")
    if not bash:
        pytest.skip("Bash is unavailable")
    root = Path(__file__).resolve().parents[1]
    deploy = tmp_path / "release/app/deploy"
    deploy.mkdir(parents=True)
    shutil.copy(root / "deploy/citation_workers.sh", tmp_path / "workers.sh")
    shutil.copy(root / "deploy/marx-search-citation-agent-test-worker.service", deploy)
    scenario = r"""
set -eu
source ./workers.sh
stage_citation_model() { :; }
systemd-run() {
    for argument; do printf '%s\n' "$argument" >> trace; done
    printf '%s\n' '===' >> trace
    if [[ "${FAIL_MODEL:-0}" == 1 && "$*" == *--check-provider ]]; then return 3; fi
}
FAIL_MODEL=1
if citation_candidate_checks "$PWD/release" "$PWD/shared" /usr/bin/python; then exit 9; fi
mv trace rejected
FAIL_MODEL=0
citation_candidate_checks "$PWD/release" "$PWD/shared" /usr/bin/python
"""
    subprocess.run([bash, "-s"], executable=bash, cwd=tmp_path, input=scenario.encode(), check=True)
    rejected = (tmp_path / "rejected").read_text(encoding="utf-8").splitlines()
    assert rejected.count("===") == 1 and "--check-startup" not in rejected
    recorded = (tmp_path / "trace").read_text(encoding="utf-8")
    model_call, bridge_call, rest = recorded.split("===\n")
    assert not rest and "--check-provider\n" in model_call
    assert "--check-startup\n" in bridge_call
    assert "--property=RuntimeMaxSec=300s\n" in bridge_call
    assert "--property=CPUQuota=75%\n" in bridge_call
    assert "HOME=/run/marx-citation-startup-release\n" in bridge_call and "CITATION_READY_FILE=\n" in bridge_call
