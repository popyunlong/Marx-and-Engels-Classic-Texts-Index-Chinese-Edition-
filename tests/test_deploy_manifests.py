from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH_MANIFEST = ROOT / "deploy" / "cloud_patch_files.txt"
COMPILE_MANIFEST = ROOT / "deploy" / "cloud_compile_files.txt"


def _read_manifest(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_cloud_deploy_manifests_are_complete_and_consistent() -> None:
    patch_files = _read_manifest(PATCH_MANIFEST)
    compile_files = _read_manifest(COMPILE_MANIFEST)

    assert patch_files
    assert compile_files
    assert len(patch_files) == len(set(patch_files))
    assert len(compile_files) == len(set(compile_files))

    missing = [item for item in patch_files + compile_files if not (ROOT / item).is_file()]
    assert not missing
    assert set(compile_files).issubset(set(patch_files))

    for required in (
        "book_config.py",
        "config/books.yaml",
        "scripts/deployment_smoke.py",
        "deploy/cloud_patch_files.txt",
        "deploy/cloud_compile_files.txt",
    ):
        assert required in patch_files


def test_app_local_imports_are_in_cloud_patch_manifest() -> None:
    patch_files = set(_read_manifest(PATCH_MANIFEST))
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    local_modules: set[str] = set()
    top_level_modules = {path.stem for path in ROOT.glob("*.py")}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".", 1)[0]
                if name in top_level_modules:
                    local_modules.add(f"{name}.py")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            name = node.module.split(".", 1)[0]
            if name in top_level_modules:
                local_modules.add(f"{name}.py")

    missing = sorted(module for module in local_modules if module not in patch_files)
    assert not missing


def _local_import_names(py_path: Path) -> set[str]:
    """解析一个 .py，返回它直接 import 的顶层模块名集合（level==0）。"""
    tree = ast.parse(py_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):  # ast.walk 会进入函数体，能抓到函数内的 import app
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".", 1)[0])
    return names


# 本仓的本地模块：根目录 *.py 与 scripts/*.py。映射 模块名 -> 清单路径。
_ROOT_MODULES = {p.stem: f"{p.name}" for p in ROOT.glob("*.py")}
_SCRIPTS_MODULES = {p.stem: f"scripts/{p.name}" for p in (ROOT / "scripts").glob("*.py")}


def _resolve_local_module(name: str) -> str | None:
    """把 import 名解析为清单路径（根目录优先），非本地模块返回 None。"""
    if name in _ROOT_MODULES:
        return _ROOT_MODULES[name]
    if name in _SCRIPTS_MODULES:
        return _SCRIPTS_MODULES[name]
    return None


def test_deployment_smoke_local_imports_are_in_manifest() -> None:
    """deployment_smoke.py 直接 import 的本地模块（含 check_inline_js）必须在部署清单。"""
    patch_files = set(_read_manifest(PATCH_MANIFEST))
    smoke = ROOT / "scripts" / "deployment_smoke.py"
    resolved = {
        path for name in _local_import_names(smoke)
        if (path := _resolve_local_module(name)) is not None
    }
    missing = sorted(module for module in resolved if module not in patch_files)
    assert not missing, f"deployment_smoke.py 引用但缺失于 cloud_patch_files.txt: {missing}"


def test_transitive_local_imports_are_in_manifest() -> None:
    """从部署入口出发做传递闭包：凡被链式 import 到的本地模块都必须在部署清单。

    捕获 2026-06-05 502 / check_inline_js 那类"A→B，B 被引用却没进清单"的链式漂移。
    """
    patch_files = set(_read_manifest(PATCH_MANIFEST))
    entrypoints = ["app.py", "scripts/deployment_smoke.py", "scripts/check_inline_js.py"]
    seen: set[str] = set()
    queue = list(entrypoints)
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        fpath = ROOT / rel
        if not fpath.is_file():
            continue
        for name in _local_import_names(fpath):
            resolved = _resolve_local_module(name)
            if resolved and resolved not in seen:
                queue.append(resolved)

    missing = sorted(module for module in seen if module not in patch_files)
    assert not missing, f"被链式引用但缺失于 cloud_patch_files.txt: {missing}"


def test_update_cloud_powershell_parses() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        return

    script = ROOT / "deploy" / "update_cloud.ps1"
    command = (
        "$tokens=$null; $errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors) { $errors | ForEach-Object { \"$($_.Extent.StartLineNumber):$($_.Message)\" }; exit 1 }"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert result.returncode == 0, result.stdout


def test_update_cloud_keeps_cache_permission_fix_opt_in() -> None:
    script = (ROOT / "deploy" / "update_cloud.ps1").read_text(encoding="utf-8")
    assert "[switch]$FixCachePermissions" in script
    assert "install -d -o www-data -g www-data -m 0700 /var/www/.marx_search_full" in script
    assert "if ($FixCachePermissions)" in script
    assert "chown -R www-data:www-data /var/www/.marx_search_full" in script
