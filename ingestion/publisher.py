from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

from .candidate import build_candidate
from .scheduler import Scheduler, YieldRequired
from .store import Store, sha256


def command(*args, timeout=30):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".incoming")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def atomic_copy(source, target):
    target = Path(target)
    if target.is_symlink():
        target=target.resolve(strict=True)
    stat = target.stat() if target.exists() else Path(source).stat()
    temporary = target.with_name(target.name + ".ingestion-next")
    shutil.copy2(source, temporary)
    os.chown(temporary, stat.st_uid, stat.st_gid)
    os.chmod(temporary, stat.st_mode)
    os.replace(temporary, target)


def sync_ingestion_runtime(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination:
        return False
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return True


def memory_required_kib(worker_memory_bytes):
    """Budget a cold corpus process from the existing worker plus 1 GiB slack.

    Candidate has an independent hard cap; live services are never stopped to
    satisfy this check. Runtime health/admission still governs the actual trial.
    """
    return max(2 * 1024**2, (int(worker_memory_bytes) + 1023) // 1024 + 1024**2)


def stage_pdf_links(packages, store, app_root, pdf_root=Path('/home/data/pdfs'), checkpoint=lambda:None):
    """Register immutable data-disk files; visibility still requires publication."""
    pdf_root=Path(pdf_root).resolve()
    destination=Path(app_root)/'pdfs/自动入库'
    for package in packages:
        checkpoint()
        digest=package['source_sha256']
        if not re.fullmatch(r'[0-9a-f]{64}',digest):
            raise ValueError('PDF 哈希格式无效')
        if package['source_file']!='pdfs/自动入库/'+digest+'.pdf':
            raise ValueError('PDF 发布路径不符合清单')
        book=store.get_book(package['book_id'])
        source=Path(book['path']).resolve()
        expected=(pdf_root/'自动入库'/(digest+'.pdf')).resolve()
        if book['sha']!=digest or source!=expected or pdf_root not in source.parents or not source.is_file():
            raise ValueError('PDF 来源与已登记数据盘文件不符')
        destination.mkdir(parents=True,exist_ok=True)
        target=destination/(digest+'.pdf')
        if os.path.lexists(target):
            if target.is_symlink() and target.resolve()!=source:
                raise ValueError('PDF 链接已指向其他文件')
            # A regular existing file is checked by build_candidate's SHA-256.
            continue
        target.symlink_to(source)


@contextlib.contextmanager
def publication_lock(path):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Publisher:
    def __init__(self, store, scheduler, app_root=Path("/opt/marx-search")):
        self.store, self.scheduler, self.app_root = store, scheduler, Path(app_root)
        # Root-owned release trees cannot be replaced by uploaded data or the web user.
        self.root = Path("/home/data/marx-ingestion-releases")
        self.root.mkdir(mode=0o755, exist_ok=True)
        self.journal = self.root / "publication.json"
        self.python = str(self.app_root / ".venv/bin/python")
        self.unit = "marx-ingestion-candidate.service"
        self.port = 8002
        self.next_tick = 0.0

    def checkpoint(self):
        if time.monotonic() >= self.next_tick:
            self.scheduler.tick()
            self.next_tick = time.monotonic() + 2
        self.scheduler.check_quiet()

    def admit_unit(self):
        self.checkpoint()
        with self.scheduler.admit():
            pass

    def health(self, port):
        started = time.monotonic()
        for endpoint in ["/api/runtime", "/", "/v2/read", "/ai", "/pricing"]:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{endpoint}", timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError("网站健康检查失败")
                if endpoint == "/api/runtime":
                    runtime=json.load(response)
                    if not runtime.get('ok'):
                        raise RuntimeError("网站语料健康检查失败")
                    worker=runtime.get('ingestion_worker',{})
                    if worker.get('enabled') and not worker.get('alive'):
                        raise RuntimeError('校注执行器停止运行，发布检查未通过')
        return time.monotonic() - started

    def switch(self, source, target, backup):
        command(self.python, str(self.app_root / "scripts/switch_caddy_upstream.py"), str(source), str(target),
                "--backup-dir", str(backup), timeout=20)

    def prepare_runtime(self, directory, record):
        import yaml
        self.checkpoint()
        for file in self.app_root.glob("*.py"):
            shutil.copy2(file, directory / file.name)
        for name in ["scripts", "templates", "static", "vendor", "static_library", "stream_library", "pdfs"]:
            source = self.app_root / name
            if source.exists() and not os.path.lexists(directory / name):
                (directory / name).symlink_to(source, target_is_directory=True)
        shutil.copytree(Path(__file__).parent, directory / "ingestion", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
        for file in (self.app_root / "data").iterdir():
            if file.name not in {"corpus.sqlite", "corpus.sqlite.sha256", "ingestion-generations.json"} and not os.path.lexists(directory / "data" / file.name):
                (directory / "data" / file.name).symlink_to(file, target_is_directory=file.is_dir())
        for name in ["logs", "cache"]:
            (directory / name).mkdir(exist_ok=True)
            os.chown(directory / name, 33, 33)
        ancestry_path = self.app_root / "data/ingestion-generations.json"
        ancestry = json.loads(ancestry_path.read_text()) if ancestry_path.exists() else {}
        if ancestry and ancestry.get("current") != record["base_sha256"]:
            # A text repair invalidates transitive append-only compatibility.
            ancestry = {}
        ancestors = ancestry.get("ancestors", {})
        old_books = yaml.safe_load((self.app_root / "config/books.yaml").read_text(encoding="utf-8"))
        ancestors[record["base_sha256"]] = {"books": [b["key"] for b in old_books["books"]]}
        if record.get('protected_revision_verified'):
            # Corrected text is not an append-only alias of any older generation.
            ancestors = {}
        atomic_json(directory / "data/ingestion-generations.json",
                    {"schema": 1, "current": record["candidate_sha256"], "ancestors": ancestors})
        # Preflight writes only one readiness file into this root-owned directory.
        ready = directory / "runtime-verified.json"
        ready.write_text("{}")
        os.chown(ready, 33, 33)
        (directory / 'worker-admission.lock').touch()
        for file in (directory / "config").iterdir():
            if file.is_file():
                file.chmod(0o640)
                os.chown(file, 0, 33)
        for parent, dirs, files in os.walk(directory, followlinks=False):
            parent_path = Path(parent)
            parts = parent_path.relative_to(directory).parts
            writable = bool(parts and parts[0] in {'logs','cache'})
            os.chown(parent_path, 33 if writable else 0, 33)
            parent_path.chmod(0o750)
            for name in files:
                file = parent_path / name
                if not file.is_symlink() and file != ready:
                    os.chown(file, 33 if writable else 0, 33)
                    file.chmod(0o640)

    def start_candidate(self, directory):
        self.admit_unit()
        if _unit_exists(self.unit):
            self.drain(self.port)
            command("systemctl", "stop", self.unit)
            subprocess.run(["systemctl", "reset-failed", self.unit], capture_output=True)
        command("systemd-run", "--unit=" + self.unit.removesuffix(".service"), "--property=Type=exec",
                "--property=User=www-data", "--property=Group=www-data", "--property=WorkingDirectory=" + str(directory),
                "--property=EnvironmentFile=/etc/marx-search.env", "--property=MemoryMax=2304M",
                "--property=CPUWeight=10", "--property=IOWeight=10", "--property=Nice=5",
                "--setenv=MARX_SKIP_SEARCH_WARM=1", "--setenv=PYTHONPATH=" + str(directory),
                self.python, "-m", "ingestion.runtime", "--preflight", "--with-worker", "--port", str(self.port),
                "--activation-file", str(directory / "worker-active"))
        start = time.monotonic()
        readiness_timeout = getattr(self, 'candidate_readiness_timeout', 55)
        while time.monotonic() - start < readiness_timeout:
            self.checkpoint()
            status=command('systemctl','show',self.unit,'-p','ActiveState','--value')
            if status=='failed':
                raise RuntimeError('候选进程启动或接口验收失败，保留现网；详见候选服务日志')
            try:
                self.health(self.port)
                if json.loads((directory / "runtime-verified.json").read_text()).get("ok"):
                    return
            except Exception:
                pass
            time.sleep(2)
        raise YieldRequired(f"候选实例未在 {readiness_timeout} 秒内就绪，保留现网")

    def publish(self, packages, prepared=None):
        from catalog_release import assert_legacy_catalog_write_allowed
        assert_legacy_catalog_write_allowed()
        self.checkpoint()
        if not (self.app_root / 'scripts/switch_caddy_upstream.py').is_file():
            raise RuntimeError('发布入口切换工具未安装，保留现网')
        available = next(int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines() if l.startswith("MemAvailable:"))
        worker_memory = self.worker_memory_sample()
        if not worker_memory.isdigit():
            raise YieldRequired('无法测量校注执行器内存，保留现网')
        required = memory_required_kib(worker_memory)
        if available < required:
            raise YieldRequired(f"发布等待 {required // 1024} MiB 可用内存（按现有执行器实测预留）；校注服务继续运行")
        if shutil.disk_usage("/home/data").free < 12 * 1024**3:
            raise YieldRequired("发布和回滚空间不足，保留现网")
        baseline = self.health(8000)
        caddy = Path("/etc/caddy/Caddyfile").read_text()
        if not re.search(r"reverse_proxy\s+127\.0\.0\.1:8000\b", caddy):
            raise YieldRequired("现网入口正在由其他版本管理，暂不发布")
        stage_pdf_links(packages,self.store,self.app_root,checkpoint=self.admit_unit)
        directory = Path(prepared) if prepared else self.root / (str(int(time.time())) + "-" + packages[0]["book_id"][:8])
        if prepared:
            from .candidate import validate_prepared
            if directory.resolve().parent != self.root.resolve():
                raise ValueError('候选必须在独立发布目录内')
            record = validate_prepared(directory, self.app_root, packages, self.checkpoint)
        else:
            record = build_candidate(packages, self.app_root, directory, self.checkpoint, unit_checkpoint=self.admit_unit)
        self.prepare_runtime(directory, record)
        backup = directory / "rollback"
        backup.mkdir(exist_ok=True)
        shutil.copy2(self.app_root / "data/corpus.sqlite", backup / "corpus.sqlite")
        shutil.copy2(self.app_root / "data/corpus.sqlite.sha256", backup / "corpus.sqlite.sha256")
        for name in record["base_config"]:
            shutil.copy2(self.app_root / "config" / name, backup / name)
        ancestry = self.app_root / "data/ingestion-generations.json"
        if ancestry.exists():
            shutil.copy2(ancestry, backup / ancestry.name)
        original_dropin = Path("/etc/systemd/system/marx-search.service.d/60-ingestion.conf")
        if original_dropin.exists():
            shutil.copy2(original_dropin, backup / original_dropin.name)
        state = {"phase": "preparing", "directory": str(directory), "record": record,
                 "live_written": False, "traffic_candidate": False, "started": time.time()}
        atomic_json(self.journal, state)
        try:
            self.start_candidate(directory)
            self.checkpoint()
            self.verify_baseline(record)
            # Assign the exact UTC window only after the isolated candidate has
            # passed its route acceptance, immediately before public cutover.
            # app._runtime_public_window observes this atomic config rewrite
            # without restarting the already-verified candidate.
            from .candidate import stamp_public_windows
            record = stamp_public_windows(directory, packages, record)
            state["record"] = record
            state["phase"] = "switching"
            atomic_json(self.journal, state)
            # Only the final switch activates consumption. During cancellable
            # preflight the candidate must never claim a user's running stage.
            (directory / "worker-was-active").touch()
            (directory / "worker-active").touch()
            self.switch(8000, self.port, backup)
            state["traffic_candidate"] = True
            state["phase"] = "observing"
            atomic_json(self.journal, state)
            # Candidate already runs a version-aware citation worker. No 30-minute
            # user queue freeze: observation never disables user task consumption.
            failures = 0
            observed_path=directory/'observation-completed.json'
            try:
                observed=json.loads(observed_path.read_text())
            except (OSError,ValueError):
                observed={}
            same_observed=(observed.get('candidate_sha256')==record['candidate_sha256']
                and observed.get('candidate_config')==record['candidate_config']
                and observed.get('packages_sha256')==record['packages_sha256']
                and observed.get('seconds',0)>=1800)
            until = time.monotonic() + (0 if same_observed else 1800)
            while time.monotonic() < until:
                try:
                    elapsed = self.health(self.port)
                    if elapsed > max(10, baseline * 2):
                        raise RuntimeError("候选延迟明显高于基线")
                    failures = 0
                except Exception:
                    failures += 1
                    if failures >= 3:
                        raise RuntimeError("候选连续三次健康检查失败")
                time.sleep(5)
            if not same_observed:
                atomic_json(observed_path,{'candidate_sha256':record['candidate_sha256'],
                    'candidate_config':record['candidate_config'],'packages_sha256':record['packages_sha256'],
                    'seconds':1800,'completed_at':time.time()})
            # Both application generations may exist during drain; job leases and
            # generation aliases preserve jobs created before and after the switch.
            self.verify_baseline(record)
            state["phase"] = "committing"
            state["live_written"] = True
            atomic_json(self.journal, state)
            for name in record["base_config"]:
                atomic_copy(directory / "config" / name, self.app_root / "config" / name)
            for name in ["corpus.sqlite", "corpus.sqlite.sha256", "ingestion-generations.json"]:
                atomic_copy(directory / "data" / name, self.app_root / "data" / name)
            # The production publisher normally imports this module from app_root,
            # so source and destination are the same directory.  Python 3.10's
            # copytree raises SameFileError for every entry in that case, after the
            # data files have already been staged.  Only copy when a publisher is
            # deliberately executed from a distinct release tree.
            sync_ingestion_runtime(Path(__file__).parent, self.app_root / "ingestion")
            dropin = Path("/etc/systemd/system/marx-search.service.d")
            dropin.mkdir(parents=True, exist_ok=True)
            old_dropin = dropin / "60-ingestion.conf"
            old_dropin.write_text("[Service]\nExecStart=\nExecStart=" + self.python + " -m ingestion.runtime --with-worker --port 8000\n")
            command("systemctl", "daemon-reload")
            self.drain(8000)
            self.restart_main()
            for _ in range(getattr(self, 'main_readiness_attempts', 25)):
                try:
                    self.health(8000)
                    break
                except Exception:
                    time.sleep(2)
            else:
                raise RuntimeError("主站新版本启动失败")
            self.switch(self.port, 8000, backup)
            state["traffic_candidate"] = False
            state["phase"] = "published"
            atomic_json(self.journal, state)
            # Keep candidate until all its user stages finish. It remains a
            # generation-compatible consumer for old review/export continuations.
            with self.store.connect() as c:
                c.executemany("UPDATE books SET status='published',error='' WHERE id=?", [(p["book_id"],) for p in packages])
            self.store.event("published", candidate_sha256=record["candidate_sha256"], ids=[p["book_id"] for p in packages])
            self.retire_candidate(state)
        except BaseException:
            if state["phase"] != "published":
                self.rollback(state)
            raise

    def worker_memory_sample(self):
        return command('systemctl', 'show', 'marx-search-citation-worker.service', '-p', 'MemoryCurrent', '--value')

    def verify_baseline(self, record):
        if sha256(self.app_root / 'data/corpus.sqlite') != record['base_sha256']:
            raise YieldRequired('候选期间生产语料有更新，重新合并')
        if any(sha256(self.app_root / 'config' / name) != digest
               for name, digest in record['base_config'].items()):
            raise YieldRequired('候选期间书目配置有更新，重新合并')

    def restart_main(self):
        command("systemctl", "restart", "marx-search")

    def drain(self, port):
        until = time.monotonic() + 120
        while time.monotonic() < until:
            rows = command("ss", "-Htn", "state", "established", f"( sport = :{port} )")
            if not rows.strip():
                return
            time.sleep(2)
        raise YieldRequired("旧连接仍在使用，暂缓回收")

    def rollback(self, state):
        directory = Path(state["directory"])
        backup = directory / "rollback"
        if state.get("live_written"):
            for name in state["record"]["base_config"]:
                atomic_copy(backup / name, self.app_root / "config" / name)
            for name in ["corpus.sqlite", "corpus.sqlite.sha256"]:
                atomic_copy(backup / name, self.app_root / "data" / name)
            ancestry = self.app_root / "data/ingestion-generations.json"
            if (backup / ancestry.name).exists():
                atomic_copy(backup / ancestry.name, ancestry)
            else:
                ancestry.unlink(missing_ok=True)
            dropin = Path("/etc/systemd/system/marx-search.service.d/60-ingestion.conf")
            if (backup / "60-ingestion.conf").exists():
                atomic_copy(backup / "60-ingestion.conf", dropin)
            else:
                dropin.unlink(missing_ok=True)
            command("systemctl", "daemon-reload")
            command("systemctl", "restart", "marx-search")
        if re.search(r"reverse_proxy\s+127\.0\.0\.1:8002\b", Path("/etc/caddy/Caddyfile").read_text()):
            for attempt in range(25):
                try:
                    self.health(8000)
                    break
                except Exception:
                    if attempt == 24:
                        raise
                    time.sleep(2)
            self.switch(self.port, 8000, backup)
        state["phase"] = "rolled_back"
        state['traffic_candidate'] = False
        atomic_json(self.journal, state)
        # Retain candidate data and its generation worker for any new-generation
        # jobs accepted before rollback; never rewrite their recorded corpus hash.
        self.store.event("rolled_back", directory=str(directory))

    def retire_candidate(self, state):
        """Release an unused preflight; retain executors needed after rollback."""
        if not _unit_exists(self.unit):
            return
        directory = Path(state["directory"])
        if (directory / "worker-was-active").exists():
            self.drain(self.port)
            from .activation import deactivate
            deactivate(directory / "worker-active")
            # Even after moving HTTP traffic away, an executor may still be
            # processing a claimed stage. Human review survives publication but
            # needs the rolled-back generation to remain available.
            try:
                now = datetime.now(timezone.utc).isoformat(timespec='seconds')
                with sqlite3.connect(self.scheduler.citation_db.as_uri() + "?mode=ro", uri=True) as c:
                    running = c.execute("SELECT COUNT(*) FROM citation_assistant_jobs WHERE "
                                        "lease_owner<>'' AND lease_expires_at>? AND "
                                        "status IN ('extracting','queued','matching','exporting')", (now,)).fetchone()[0]
                    pending = c.execute("SELECT COUNT(*) FROM citation_assistant_jobs WHERE corpus_sha256=? "
                                        "AND status IN ('extracting','awaiting_sections','queued','matching','review_ready','exporting') "
                                        "AND expires_at>?", (state["record"]["candidate_sha256"],now)).fetchone()[0]
                export_db = self.scheduler.citation_db.with_name('search_exports.sqlite3')
                if export_db.exists():
                    with sqlite3.connect(export_db.as_uri() + '?mode=ro', uri=True) as c:
                        running += c.execute("SELECT COUNT(*) FROM search_export_jobs WHERE lease_expires_at>? "
                                             "AND status IN ('counting','collecting','rendering','packaging')", (now,)).fetchone()[0]
                        pending += c.execute("SELECT COUNT(*) FROM search_export_jobs WHERE corpus_version=? AND expires_at>? "
                                             "AND status IN ('queued','counting','collecting','rendering','packaging')",
                                             (state['record']['candidate_sha256'], now)).fetchone()[0]
                if running or (pending and state["phase"] == "rolled_back"):
                    raise YieldRequired("旧版本仍有用户任务，保留执行器并暂缓下一次发布")
            except BaseException:
                (directory / "worker-active").touch()
                raise
        self.drain(self.port)
        command("systemctl", "stop", self.unit)


def _unit_exists(unit):
    return subprocess.run(["systemctl", "cat", unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/home/data/marx-ingestion"))
    args = parser.parse_args()
    store = Store(args.root)
    scheduler = Scheduler(store, Path("/var/www/.marx_search_full/citation_assistant.sqlite3"), health_url="http://127.0.0.1:8000/api/runtime")
    publisher = Publisher(store, scheduler)
    while True:
        try:
            with publication_lock(Path("/home/data/marx-search-data/corpus-publish.lock")):
                if publisher.journal.exists():
                    state = json.loads(publisher.journal.read_text())
                    if state["phase"] not in {"published", "rolled_back"}:
                        publisher.rollback(state)
                    publisher.retire_candidate(state)
                with store.connect() as c:
                    rows = list(c.execute("SELECT b.id FROM books b JOIN batches a ON a.id=b.batch WHERE b.status='ready' AND b.paused=0 AND a.pilot=0"))
                if rows:
                    packages = [json.loads((store.root / "packages" / r[0] / "book.json").read_text(encoding="utf-8")) for r in rows]
                    publisher.publish(packages)
                    store.set_state("publication", "合格书目已发布")
        except Exception as exc:
            store.set_state("publication", str(exc)[:300])
        time.sleep(15)


if __name__ == "__main__":
    main()
