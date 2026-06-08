from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import platform
import socket
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml


APP_NAME = "马克思恩格斯文集全集检索程序"
APP_VERSION = "1.1.0"
APP_ID = "marx_search_full"
APP_TOKEN_HEADER = "X-App-Token"
ACTIVATION_SECRET = "marx-search-full-edition-v1"


if getattr(sys, "frozen", False):
    BUNDLE_ROOT = Path(sys._MEIPASS)
    RUNTIME_ROOT = Path(sys.executable).resolve().parent
else:
    BUNDLE_ROOT = Path(__file__).resolve().parent
    RUNTIME_ROOT = BUNDLE_ROOT

CONFIG_DIR = BUNDLE_ROOT / "config"
MANIFEST_PATH = CONFIG_DIR / "manifest.yaml"
VOLUMES_PATH = CONFIG_DIR / "volumes.yaml"

EXTERNAL_DATA_DIR = RUNTIME_ROOT / "data"
BUNDLED_DATA_DIR = BUNDLE_ROOT / "data"
PDF_ROOT = RUNTIME_ROOT / "pdfs"
LOG_DIR = RUNTIME_ROOT / "logs"
LOG_FILE = LOG_DIR / "app.log"


def _appdata_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or RUNTIME_ROOT)
        return base / APP_ID
    return Path.home() / f".{APP_ID}"


APPDATA_DIR = _appdata_root()
ACTIVATION_FILE = APPDATA_DIR / "activation.json"


@dataclass(frozen=True)
class ActivationStatus:
    valid: bool
    fingerprint: str
    expected_code: str
    saved_code: str = ""
    message: str = ""


@dataclass(frozen=True)
class RuntimeStatus:
    can_search: bool
    full_resources_ready: bool
    db_path: Path | None
    db_hash_path: Path | None
    db_status: str
    db_expected_hash: str
    db_actual_hash: str
    pdf_root: Path
    manifest_ok: bool
    volumes_ok: bool
    problems: tuple[str, ...]
    data_version: str


@dataclass(frozen=True)
class DeploymentSettings:
    app_mode: str
    bind_host: str
    port: int
    public_base_url: str
    public_scheme: str
    public_host: str
    enable_browser_autostart: bool
    enable_idle_shutdown: bool
    enable_remote_quit: bool

    @property
    def is_desktop(self) -> bool:
        return self.app_mode == "desktop"

    @property
    def is_server(self) -> bool:
        return self.app_mode == "server"

    @property
    def management_api_enabled(self) -> bool:
        return self.enable_idle_shutdown or self.enable_remote_quit


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def load_deployment_settings() -> DeploymentSettings:
    app_mode = str(os.environ.get("APP_MODE") or "desktop").strip().lower()
    if app_mode not in {"desktop", "server"}:
        app_mode = "desktop"

    bind_host = str(
        os.environ.get("BIND_HOST")
        or ("127.0.0.1" if app_mode == "server" else "127.0.0.1")
    ).strip()

    default_port = 8000 if app_mode == "server" else 5000
    try:
        port = int(os.environ.get("PORT") or default_port)
    except ValueError:
        port = default_port
    if port < 1 or port > 65535:
        port = default_port

    public_base_url = str(os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    parsed = urlparse(public_base_url) if public_base_url else None
    public_scheme = parsed.scheme if parsed and parsed.scheme else "https"
    public_host = parsed.netloc if parsed and parsed.netloc else ""

    enable_browser_autostart = _env_bool(
        "ENABLE_BROWSER_AUTOSTART",
        default=app_mode == "desktop",
    )
    enable_idle_shutdown = _env_bool(
        "ENABLE_IDLE_SHUTDOWN",
        default=app_mode == "desktop",
    )
    enable_remote_quit = _env_bool(
        "ENABLE_REMOTE_QUIT",
        default=app_mode == "desktop",
    )

    if app_mode == "server":
        enable_browser_autostart = False
        enable_idle_shutdown = False
        enable_remote_quit = False

    return DeploymentSettings(
        app_mode=app_mode,
        bind_host=bind_host,
        port=port,
        public_base_url=public_base_url,
        public_scheme=public_scheme,
        public_host=public_host,
        enable_browser_autostart=enable_browser_autostart,
        enable_idle_shutdown=enable_idle_shutdown,
        enable_remote_quit=enable_remote_quit,
    )


def ensure_runtime_dirs() -> None:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def configure_logging() -> logging.Logger:
    ensure_runtime_dirs()
    logger = logging.getLogger(APP_ID)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_runtime_db_files() -> tuple[Path | None, Path | None]:
    external_db = EXTERNAL_DATA_DIR / "corpus.sqlite"
    external_hash = EXTERNAL_DATA_DIR / "corpus.sqlite.sha256"
    if external_db.exists():
        return external_db, external_hash

    bundled_db = BUNDLED_DATA_DIR / "corpus.sqlite"
    bundled_hash = BUNDLED_DATA_DIR / "corpus.sqlite.sha256"
    if bundled_db.exists():
        return bundled_db, bundled_hash
    return None, None


def read_expected_hash(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    raw = read_text(path)
    return raw.split()[0].strip().lower() if raw else ""


def read_data_version() -> str:
    for candidate in (
        EXTERNAL_DATA_DIR / "release.json",
        BUNDLED_DATA_DIR / "release.json",
    ):
        payload = read_json(candidate)
        version = str(payload.get("data_version") or "").strip()
        if version:
            return version
    return "unknown"


def collect_runtime_status() -> RuntimeStatus:
    problems: list[str] = []
    manifest_ok = MANIFEST_PATH.exists()
    volumes_ok = VOLUMES_PATH.exists()
    if not manifest_ok:
        problems.append("缺少 config/manifest.yaml。")
    if not volumes_ok:
        problems.append("缺少 config/volumes.yaml。")

    db_path, db_hash_path = resolve_runtime_db_files()
    db_status = "missing"
    db_expected_hash = ""
    db_actual_hash = ""
    can_search = False

    if db_path is None:
        problems.append("未找到 data/corpus.sqlite。")
    else:
        db_expected_hash = read_expected_hash(db_hash_path)
        if not db_expected_hash:
            problems.append("未找到 data/corpus.sqlite.sha256，无法验证资料完整性。")
            db_status = "missing_hash"
        else:
            try:
                db_actual_hash = compute_sha256(db_path)
            except Exception:
                problems.append("读取 corpus.sqlite 失败。")
                db_status = "unreadable"
            else:
                if hmac.compare_digest(db_actual_hash, db_expected_hash):
                    can_search = manifest_ok and volumes_ok
                    db_status = "ok"
                else:
                    problems.append("corpus.sqlite 校验失败，资料可能已被替换或损坏。")
                    db_status = "mismatch"

    full_resources_ready = can_search and PDF_ROOT.exists()
    if can_search and not PDF_ROOT.exists():
        problems.append("未找到 pdfs/ 资料目录，当前只能使用纯检索能力。")

    return RuntimeStatus(
        can_search=can_search,
        full_resources_ready=full_resources_ready,
        db_path=db_path,
        db_hash_path=db_hash_path,
        db_status=db_status,
        db_expected_hash=db_expected_hash,
        db_actual_hash=db_actual_hash,
        pdf_root=PDF_ROOT,
        manifest_ok=manifest_ok,
        volumes_ok=volumes_ok,
        problems=tuple(problems),
        data_version=read_data_version(),
    )


def load_allowed_source_files() -> set[str]:
    if not MANIFEST_PATH.exists():
        return set()
    payload = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")) or {}
    allowed: set[str] = set()
    for items in payload.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            source_file = str(item.get("file") or "").strip()
            if source_file:
                allowed.add(str(Path(source_file).as_posix()))
    return allowed


def machine_fingerprint() -> str:
    parts = [
        socket.gethostname(),
        hex(uuid.getnode()),
        platform.machine(),
        platform.system(),
    ]
    return "|".join(parts)


def build_activation_code(fingerprint: str | None = None) -> str:
    fingerprint = fingerprint or machine_fingerprint()
    digest = hmac.new(
        ACTIVATION_SECRET.encode("utf-8"),
        fingerprint.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().upper()[:20]
    return "-".join(digest[i : i + 5] for i in range(0, 20, 5))


def load_activation_status() -> ActivationStatus:
    fingerprint = machine_fingerprint()
    expected_code = build_activation_code(fingerprint)
    payload = read_json(ACTIVATION_FILE)
    saved_code = str(payload.get("code") or "").strip().upper()
    saved_fingerprint = str(payload.get("fingerprint") or "").strip()

    if not saved_code:
        return ActivationStatus(
            valid=False,
            fingerprint=fingerprint,
            expected_code=expected_code,
            message="未激活，当前只能使用纯检索能力。",
        )

    if saved_fingerprint and saved_fingerprint != fingerprint:
        return ActivationStatus(
            valid=False,
            fingerprint=fingerprint,
            expected_code=expected_code,
            saved_code=saved_code,
            message="当前激活码不属于这台机器。",
        )

    if hmac.compare_digest(saved_code, expected_code):
        return ActivationStatus(
            valid=True,
            fingerprint=fingerprint,
            expected_code=expected_code,
            saved_code=saved_code,
            message="已激活完整资料版。",
        )

    return ActivationStatus(
        valid=False,
        fingerprint=fingerprint,
        expected_code=expected_code,
        saved_code=saved_code,
        message="激活码无效，请重新输入。",
    )


def save_activation_code(code: str) -> ActivationStatus:
    ensure_runtime_dirs()
    fingerprint = machine_fingerprint()
    normalized = code.strip().upper()
    expected = build_activation_code(fingerprint)
    if not hmac.compare_digest(normalized, expected):
        return ActivationStatus(
            valid=False,
            fingerprint=fingerprint,
            expected_code=expected,
            saved_code=normalized,
            message="激活码无效，请重新输入。",
        )
    payload = {"fingerprint": fingerprint, "code": normalized}
    ACTIVATION_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return load_activation_status()
