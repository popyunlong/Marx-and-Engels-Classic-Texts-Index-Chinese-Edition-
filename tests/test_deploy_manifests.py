from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _parse_powershell(path: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        return
    command = (
        "$tokens=$null; $errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errors) | Out-Null; "
        'if ($errors) { $errors | ForEach-Object { "$($_.Extent.StartLineNumber):$($_.Message)" }; exit 1 }'
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert result.returncode == 0, f"{path.name}:\n{result.stdout}"


def test_release_powershell_entrypoints_parse() -> None:
    for relative in (
        "deploy/release.ps1",
        "deploy/rollback_release.ps1",
        "deploy/update_cloud.ps1",
        "deploy/upload_reader_patch.ps1",
        "deploy/upload_dictionary_patch.ps1",
        "deploy/upload_to_server.ps1",
    ):
        _parse_powershell(ROOT / relative)


def test_release_builder_fails_locally_before_server_contact() -> None:
    source = (ROOT / "deploy" / "release.ps1").read_text(encoding="utf-8")
    assert 'if ($branch -ne "production")' in source
    assert '"status", "--porcelain=v1", "--untracked-files=all"' in source
    assert '"fetch", "--no-tags", "origin", "main", "production"' in source
    assert '"rev-parse", "origin/production"' in source
    assert "git archive" not in source  # Invocation is structured, not shell-expanded.
    assert '"archive", "--format=zip"' in source
    assert "build_release_archive.py" in source
    assert "Build deterministic immutable release archive" in source
    assert "$head -ne $remoteHead" in source
    assert '"merge-base", "--is-ancestor", $head, "origin/main"' in source
    assert "PYTHONPYCACHEPREFIX" in source
    assert "Reverify source after compilation" in source
    assert "AllowDirty" not in source
    assert 'if ($DryRun)' in source
    dry_run_at = source.index('if ($DryRun)')
    assert source.index('Require-Command "ssh"') > dry_run_at
    assert source.index('Require-Command "scp"') > dry_run_at


def test_release_archive_and_remote_names_are_unique_and_commit_bound() -> None:
    source = (ROOT / "deploy" / "release.ps1").read_text(encoding="utf-8")
    assert '[Guid]::NewGuid()' in source
    assert '$releaseId = "$head-$utcStamp-$nonce"' in source
    assert '"marx-search-$releaseId.tar.gz"' in source
    assert '"/var/tmp/marx-search-$releaseId.tar.gz"' in source
    assert '"--parent-release-id", $ExpectedLive' in source
    assert "app/deploy/promote_release.sh" in source


def test_server_promotion_has_one_lock_and_compare_and_swap() -> None:
    source = (ROOT / "deploy" / "promote_release.sh").read_text(encoding="utf-8")
    assert 'LOCK_FILE="/run/lock/marx-search-release.lock"' in source
    assert "flock -n 9" in source
    assert 'LIVE_BEFORE="$(current_release_id)"' in source
    assert '[ "$LIVE_BEFORE" != "$EXPECTED_LIVE" ]' in source
    assert '"parent_release_id"' in source
    assert '[ "$META_PARENT" = "$EXPECTED_LIVE" ]' in source
    assert 'RELEASES="$APP_ROOT/releases"' in source
    assert 'FINAL="$RELEASES/$RELEASE_ID"' in source
    assert 'mv -Tf -- "$APP_ROOT/.current-$RELEASE_ID" "$APP_ROOT/current"' in source
    assert 'ln -sfn -- "$OLD_CURRENT" "$APP_ROOT/previous"' in source
    assert 'release-ledger.jsonl' in source
    assert '"event": "promote"' in source
    assert 'MARX_RUNTIME_DATA_DIR="$APP_ROOT/data"' in source
    assert 'MARX_RUNTIME_PDF_DIR="$APP_ROOT/pdfs"' in source
    assert 'rm -rf -- "$FINAL/app/$shared"' not in source
    assert 'ln -s -- "$APP_ROOT/$shared"' not in source
    assert '"${MANAGED_SUPPORT_UNITS[@]}"' in source


def test_candidate_is_healthy_before_any_cutover_or_service_replacement() -> None:
    source = (ROOT / "deploy" / "promote_release.sh").read_text(encoding="utf-8")
    candidate = source.index("systemd-run")
    candidate_health = source.index('wait_health "$CANDIDATE_PORT"', candidate)
    caddy_cutover = source.index(
        'switch_caddy "$PRIMARY_PORT" "$CANDIDATE_PORT"', candidate_health
    )
    unit_install = source.index("/etc/systemd/system/marx-search.service")
    assert candidate < candidate_health < caddy_cutover < unit_install
    for endpoint in ("/api/runtime", "/pricing", "/ai", "/v2/ai", "/v2/read"):
        assert endpoint in source
    assert 'actual = ((payload.get("app_release") or {}).get("id") or "")' in source
    assert 'wait_health "$CANDIDATE_PORT" "$RELEASE_ID"' in source
    assert 'wait_health "$PRIMARY_PORT" "$RELEASE_ID"' in source
    assert "rollback_primary" in source
    assert "restoring the direct predecessor" in source


def test_both_cutovers_drain_connections_before_restarting_or_stopping() -> None:
    source = (ROOT / "deploy" / "promote_release.sh").read_text(encoding="utf-8")
    assert 'DRAIN_TIMEOUT_SECONDS="${MARX_DEPLOY_DRAIN_TIMEOUT_SECONDS:-720}"' in source
    assert 'ss -Htn state established "( sport = :${port} )"' in source
    assert 'echo "unable to inspect active connections' in source  # socket inspection fails closed

    first_cutover = source.index('switch_caddy "$PRIMARY_PORT" "$CANDIDATE_PORT"')
    primary_drain = source.index(
        'drain_port "$PRIMARY_PORT" "previous primary"', first_cutover
    )
    current_swap = source.index(
        'mv -Tf -- "$APP_ROOT/.current-$RELEASE_ID" "$APP_ROOT/current"',
        primary_drain,
    )
    primary_restart = source.index('systemctl restart "$MAIN_SERVICE"', current_swap)
    assert first_cutover < primary_drain < current_swap < primary_restart

    final_cutover = source.index(
        'if ! switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT"', primary_restart
    )
    final_retirement = source.index(
        'retire_candidate_if_drained "promoted release candidate"', final_cutover
    )
    assert primary_restart < final_cutover < final_retirement

    retire_helper = source.index("retire_candidate_if_drained()")
    helper_drain = source.index('drain_port "$CANDIDATE_PORT" "$label"', retire_helper)
    helper_stop = source.index('if ! systemctl stop "$CANDIDATE_UNIT"', helper_drain)
    helper_active_check = source.index(
        'systemctl is-active --quiet "$CANDIDATE_UNIT"', helper_stop
    )
    assert retire_helper < helper_drain < helper_stop < helper_active_check
    assert source.count('CANDIDATE_RETIREMENT_DEFERRED=$CANDIDATE_UNIT') >= 3


def test_primary_drain_timeout_aborts_before_commit_and_restores_old_route() -> None:
    source = (ROOT / "deploy" / "promote_release.sh").read_text(encoding="utf-8")
    timeout_guard = source.index('if ! drain_port "$PRIMARY_PORT" "previous primary"')
    abort_call = source.index("if abort_before_commit", timeout_guard)
    current_swap = source.index(
        'mv -Tf -- "$APP_ROOT/.current-$RELEASE_ID" "$APP_ROOT/current"',
        timeout_guard,
    )
    assert timeout_guard < abort_call < current_swap

    abort_helper = source.index("abort_before_commit()")
    abort_helper_end = source.index("systemctl stop \"$CANDIDATE_UNIT\"", abort_helper)
    abort_body = source[abort_helper:abort_helper_end]
    assert 'switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT"' in abort_body
    assert 'wait_health "$PRIMARY_PORT" "$EXPECTED_LIVE"' in abort_body
    assert 'switch_caddy "$PRIMARY_PORT" "$CANDIDATE_PORT"' in abort_body
    assert "systemctl restart" not in abort_body


def test_candidate_cutover_and_primary_start_failures_use_safe_paths() -> None:
    source = (ROOT / "deploy" / "promote_release.sh").read_text(encoding="utf-8")
    candidate_failure = source.index('if ! wait_health "$CANDIDATE_PORT" "$RELEASE_ID"')
    first_cutover = source.index(
        'if ! switch_caddy "$PRIMARY_PORT" "$CANDIDATE_PORT"', candidate_failure
    )
    current_swap = source.index(
        'mv -Tf -- "$APP_ROOT/.current-$RELEASE_ID" "$APP_ROOT/current"', first_cutover
    )
    candidate_failure_body = source[candidate_failure:first_cutover]
    cutover_failure_body = source[first_cutover:current_swap]
    assert 'retire_candidate_if_drained "unhealthy candidate"' in candidate_failure_body
    assert "exit 4" in candidate_failure_body
    assert 'retire_candidate_if_drained "uncut candidate"' in cutover_failure_body
    assert "primary is unchanged" in cutover_failure_body
    assert "exit 5" in cutover_failure_body

    primary_start = source.index('if ! systemctl restart "$MAIN_SERVICE"', current_swap)
    final_cutover = source.index(
        'if ! switch_caddy "$CANDIDATE_PORT" "$PRIMARY_PORT"', primary_start
    )
    assert "rollback_primary" in source[primary_start:final_cutover]
    assert "rollback_primary" in source[final_cutover:]


def test_drain_timeout_is_documented_in_production_environment_example() -> None:
    env_example = (ROOT / "deploy" / "marx-search.env.example").read_text(encoding="utf-8")
    assert "MARX_DEPLOY_DRAIN_TIMEOUT_SECONDS=720" in env_example


def test_rollback_is_separate_locked_and_audited() -> None:
    source = (ROOT / "deploy" / "rollback_release.sh").read_text(encoding="utf-8")
    assert 'LOCK_FILE="/run/lock/marx-search-release.lock"' in source
    assert "flock -n 9" in source
    assert '[ "$CURRENT" = "$EXPECTED_CURRENT" ]' in source
    assert '[ -f "$TARGET/release.json" ]' in source
    assert '[ "$TARGET_META_ID" = "$TARGET_RELEASE" ]' in source
    assert 'build_release_manifest.py" verify' in source
    assert 'install -o root -g root -m 0644 "$TARGET/app/deploy/marx-search.service"' in source
    assert 'target_healthy' in source
    assert '"event": "rollback"' in source
    assert "target failed health checks; current release restored" in source


def test_legacy_application_mutators_are_disabled_or_delegate() -> None:
    update = (ROOT / "deploy" / "update_cloud.ps1").read_text(encoding="utf-8")
    assert 'Join-Path $PSScriptRoot "release.ps1"' in update
    assert "AllowDirty" not in update
    for relative in (
        "deploy/push_code_patch.sh",
        "deploy/stage_release.sh",
        "deploy/zero_downtime_restart.sh",
        "deploy/restart_verify.sh",
        "deploy/_swap_corpus_remote.sh",
        "deploy/promote_new_corpus_202609.sh",
        "deploy/promote_corpus_repair_candidate.sh",
        "deploy/install_corpus_repair_service.sh",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "exit 64" in source
    assert "Direct corpus uploads are frozen" in (
        ROOT / "deploy" / "upload_corpus_db.ps1"
    ).read_text(encoding="utf-8")
    assert "Direct recursive source upload is retired" in (
        ROOT / "deploy" / "upload_to_server.ps1"
    ).read_text(encoding="utf-8")


def test_all_remaining_production_mutators_share_the_global_lock() -> None:
    for relative in (
        "deploy/promote_release.sh",
        "deploy/rollback_release.sh",
        "deploy/restore.sh",
        "deploy/health_watchdog.sh",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "/run/lock/marx-search-release.lock" in source
        assert "flock -n" in source


def test_canonical_service_runs_only_the_current_immutable_release() -> None:
    unit = (ROOT / "deploy" / "marx-search.service").read_text(encoding="utf-8")
    assert "WorkingDirectory=/opt/marx-search/current/app" in unit
    assert "APP_RELEASE_FILE=/opt/marx-search/current/release.json" in unit
    assert "MARX_RUNTIME_LOG_DIR=/opt/marx-search/logs" in unit
    assert "MARX_RUNTIME_DATA_DIR=/opt/marx-search/data" in unit
    assert "MARX_RUNTIME_PDF_DIR=/opt/marx-search/pdfs" in unit
    assert "MARX_RUNTIME_STATIC_LIBRARY_DIR=/opt/marx-search/static_library" in unit
    assert "MARX_AI_CONFIG_FILE=/opt/marx-search/config/ai.yaml" in unit
    assert "PYTHONPATH=/opt/marx-search/current/app:/opt/marx-search/current/.deps" in unit
    assert "ExecStart=/opt/marx-search/runtime-python -m ingestion.runtime" in unit
    assert "WorkingDirectory=/opt/marx-search\n" not in unit


def test_all_managed_app_units_use_the_immutable_current_source() -> None:
    forbidden = re.compile(
        r"WorkingDirectory=/opt/marx-search$|"
        r"/opt/marx-search/(?:\.venv|scripts|deploy)/|"
        r"PYTHONPATH=/opt/marx-search(?:[:\s]|$)|"
        r"--project-root /opt/marx-search(?:\s|$)",
        re.MULTILINE,
    )
    unit_paths = sorted((ROOT / "deploy").glob("*.service"))
    unit_paths += sorted((ROOT / "deploy" / "production_units").glob("*.service"))
    offenders = [
        str(path.relative_to(ROOT))
        for path in unit_paths
        if forbidden.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders


def test_app_local_imports_exist_in_the_committed_tree() -> None:
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    local_modules = {path.stem for path in ROOT.glob("*.py")}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".", 1)[0])
    missing = sorted(name for name in imported if name in local_modules and not (ROOT / f"{name}.py").is_file())
    assert not missing
    assert (ROOT / "release_metadata.py").is_file()
    assert (ROOT / "ingestion" / "runtime.py").is_file()


def test_pytest_collection_is_limited_to_the_official_tests_tree() -> None:
    config = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "testpaths = tests" in config
    for excluded in ("artifacts", "release", "build", ".codex-*", ".security-work"):
        assert excluded in config


def test_deploy_gates_and_mimo_are_safe_by_default() -> None:
    env_example = (ROOT / "deploy" / "marx-search.env.example").read_text(encoding="utf-8")
    assert "MIMO_API_KEY=" in env_example
    assert "MIMO_MIGRATION_ENABLED=0" in env_example
    assert "MIMO_ADMIN_GRAY_ENABLED=0" in env_example


def test_pdf_preflight_has_no_optional_docx_dependency() -> None:
    source = (ROOT / "scripts" / "citation_agent_preflight.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "docx" not in imports
    assert "citation_agent_queue" not in imports
    assert "citation_agent_test_backend" not in imports


def test_citation_agent_web_entrypoint_and_https_proxy_are_packaged_safely() -> None:
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    proxy = (ROOT / "deploy" / "citation-agent-tinyproxy.conf.example").read_text(encoding="utf-8")
    proxy_filter = (ROOT / "deploy" / "citation-agent.filter.example").read_text(encoding="utf-8")
    appnav = (ROOT / "templates" / "_appnav.html").read_text(encoding="utf-8")
    agent_web = (ROOT / "citation_agent_test_web.py").read_text(encoding="utf-8")
    agent_template = (ROOT / "templates" / "citation_agent_test.html").read_text(encoding="utf-8")
    test_worker = (ROOT / "deploy" / "marx-search-citation-agent-test-worker.service").read_text(encoding="utf-8")

    assert "import citation_agent_test_web" in app_source
    assert "citation_agent_test_web.register_routes(app, globals())" in app_source
    assert "FilterURLs Off" in proxy
    assert "FilterDefaultDeny Yes" in proxy
    assert proxy_filter.strip() == r"^api\.deepseek\.com$"
    assert "UMask=0007" in test_worker
    assert "UMask=0077" not in test_worker
    assert "url_for('citation_agent_page')" in appnav
    assert appnav.count('href="{{ _citation_href }}"') == 2
    assert '@app.get("/citation-agent")' in agent_web
    assert '@app.post("/api/citation-agent/jobs")' in agent_web
    assert 'web["_citation_assistant_enabled_for_user"](user)' in agent_web
    assert "论文插注校注 Agent 正式版仅对有效会员开放" in agent_web
    assert "data-access=\"{{ '1' if citation_access else '0' }}\"" in agent_template
    assert "游客和普通账号可查看完整流程" in agent_template
    assert "查看旧版历史任务" in agent_template
