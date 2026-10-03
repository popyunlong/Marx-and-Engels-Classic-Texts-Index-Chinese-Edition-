"""Capture a production catalogue while protecting live reader traffic.

The server performs only low-priority, bounded reads under a nonblocking shared
release lock. Files are transferred uncompressed at no more than 2 MiB/s;
compression and verification happen locally. Any incomplete capture is removed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import tarfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from catalog_health import HealthWindow, validate_sample, identity

# Bundle the reviewed policy in memory; never install a helper on production.
_POLICY = (ROOT / "catalog_health.py").read_bytes()
REMOTE_SOURCE = (
    "import sys, types\n"
    "_policy = types.ModuleType('catalog_health')\n"
    "sys.modules['catalog_health'] = _policy\n"
    "exec(compile(" + repr(_POLICY) + ", 'catalog_health.py', 'exec'), _policy.__dict__)\n"
).encode('utf-8') + (ROOT / 'scripts/catalog_snapshot_remote.py').read_bytes().replace(
    b'from __future__ import annotations', b'')
BATCH_FILES = 100
BATCH_BYTES = 32 * 1024 * 1024
SAMPLE_SECONDS = 30
BASELINE_SECONDS = 300
LOCK_RETRIES = 10


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def batches(files):
    current, size = {}, 0
    for name, record in sorted(files.items()):
        amount = record["size"]
        if amount > BATCH_BYTES:
            raise ValueError("single source exceeds safe batch budget: " + name)
        if current and (len(current) == BATCH_FILES or size + amount > BATCH_BYTES):
            yield current
            current, size = {}, 0
        current[name] = record
        size += amount
    if current:
        yield current


class SnapshotMonitor:
    def __init__(self, remote, *, baseline_seconds=BASELINE_SECONDS,
                 sample_seconds=SAMPLE_SECONDS):
        self.remote = remote
        self.baseline_seconds = baseline_seconds
        self.sample_seconds = sample_seconds
        self.stopped = threading.Event()
        self.alarm = threading.Event()
        self.reason = ""
        self.thread = None
        self.policy = None
        self.observations = []

    def measure_baseline(self):
        first = self.remote.json("metrics")
        validate_sample(first)
        samples = [first]
        for _ in range(max(0, self.baseline_seconds // self.sample_seconds)):
            if self.stopped.wait(self.sample_seconds):
                raise RuntimeError("snapshot baseline interrupted")
            sample = self.remote.json("metrics")
            validate_sample(sample, identity(first))
            samples.append(sample)
        self.policy = HealthWindow(samples)
        self.observations.extend(samples)
        return {"routes": self.policy.baseline,
                "iowait_fraction": self.policy.baseline_iowait,
                "windows": len(samples), "sample_source": "server_loopback",
                "policy_version": 2}

    def _fail(self, reason):
        self.reason = reason
        self.alarm.set()
        self.remote.terminate_active()

    def _run(self):
        while not self.stopped.wait(self.sample_seconds):
            try:
                self._observe()
            except Exception as exc:  # noqa: BLE001 - monitoring failure must abort capture
                self._fail("production health metrics unavailable: " + str(exc))
                return

    def _observe(self):
        sample = self.remote.json("metrics")
        self.observations.append(sample)
        self.policy.observe(sample)

    def start(self):
        self.thread = threading.Thread(target=self._run,
                                       name="catalog-snapshot-monitor", daemon=True)
        self.thread.start()

    def check(self):
        if self.alarm.is_set():
            raise RuntimeError(self.reason)

    def finish(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=self.sample_seconds + 50)
            if self.thread.is_alive():
                raise RuntimeError("health observation did not finish")
        self.check()
        self._observe()


class Remote:
    def __init__(self, host, key):
        self.host, self.key = host, key
        self.active = None
        self.active_lock = threading.Lock()

    def command(self, request):
        encoded = base64.b64encode(canonical(request)).decode("ascii")
        remote = ("flock -s -n /run/lock/marx-search-release.lock "
                  "timeout 30s ionice -c3 nice -n19 "
                  "/opt/marx-search/.venv/bin/python -B - " + encoded)
        return ["ssh"] + (["-i", str(self.key)] if self.key else []) + ["-o", "BatchMode=yes",
                "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=yes", self.host, remote]

    def terminate_active(self):
        with self.active_lock:
            if self.active and self.active.poll() is None:
                self.active.terminate()

    def json(self, action):
        for attempt in range(LOCK_RETRIES):
            result = subprocess.run(self.command({"action": action}), input=REMOTE_SOURCE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=45)
            if result.returncode == 0:
                return json.loads(result.stdout)
            if result.returncode == 1 and not result.stderr.strip() and attempt < LOCK_RETRIES - 1:
                time.sleep(SAMPLE_SECONDS)
                continue
            raise RuntimeError("remote " + action + " failed: " +
                               result.stderr.decode(errors="replace")[-1000:])
        raise RuntimeError("release lock remained occupied")

    def files(self, records, destination, monitor):
        request = {"action": "files", "files": records}
        for attempt in range(LOCK_RETRIES):
            monitor.check()
            proc = subprocess.Popen(self.command(request), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with self.active_lock:
                self.active = proc
            try:
                proc.stdin.write(REMOTE_SOURCE)
                proc.stdin.close()
                seen = set()
                try:
                    with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
                        for member in archive:
                            if (not member.isfile() or member.name not in records
                                    or member.name in seen):
                                raise ValueError("unexpected snapshot archive member")
                            if member.size != records[member.name]["size"]:
                                raise ValueError("snapshot file size changed")
                            target = destination / member.name
                            if not target.resolve().is_relative_to(destination.resolve()):
                                raise ValueError("snapshot path escapes local output")
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with archive.extractfile(member) as source, target.open("xb") as out:
                                shutil.copyfileobj(source, out)
                            seen.add(member.name)
                except tarfile.ReadError:
                    pass  # A busy nonblocking lock emits no tar stream.
                # Streaming tar readers stop at the end marker and may leave
                # record padding in stdout. Drain it before waiting on stderr;
                # otherwise SSH can wait for its pipe while the client waits
                # for SSH to exit.
                proc.stdout.read()
                stderr = proc.stderr.read().decode(errors="replace")
                code = proc.wait(timeout=40)
                monitor.check()
                if code == 0 and seen == set(records):
                    return
                if code == 1 and not stderr.strip() and not seen and attempt < LOCK_RETRIES - 1:
                    time.sleep(SAMPLE_SECONDS)
                    continue
                raise RuntimeError("remote file batch failed: " + stderr[-1000:])
            finally:
                with self.active_lock:
                    if self.active is proc:
                        self.active = None
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
        raise RuntimeError("release lock remained occupied")


def capture(output, remote, *, baseline_seconds=BASELINE_SECONDS,
            sample_seconds=SAMPLE_SECONDS):
    output = Path(output).resolve()
    if output.exists() or output.parent == output:
        raise ValueError("snapshot output must be a new directory")
    monitor = SnapshotMonitor(remote, baseline_seconds=baseline_seconds,
                              sample_seconds=sample_seconds)
    baseline = monitor.measure_baseline()
    output.mkdir(parents=True)
    try:
        initial = remote.json("metadata")
        snapshot = output / "snapshot"
        snapshot.mkdir()
        (snapshot / "toc_entries.json").write_bytes(
            canonical(initial["database"]["toc_entries"]))
        (snapshot / "sources.json").write_bytes(
            canonical(initial["database"]["sources"]))
        (snapshot / "baseline.json").write_bytes(canonical(initial["runtime"]))
        monitor.start()
        for batch in batches(initial["files"]):
            remote.files(batch, snapshot, monitor)
        monitor.finish()
        final = remote.json("verify")
        for key in ("runtime", "app", "files", "ledger_bytes"):
            if final[key] != initial[key]:
                raise RuntimeError("production " + key + " changed during snapshot")
        for key in ("sha256", "toc_count", "source_count"):
            if final["database"][key] != initial["database"][key]:
                raise RuntimeError("production catalogue changed during snapshot")
        hashes = {}
        for name, record in initial["files"].items():
            path = snapshot / name
            if path.stat().st_size != record["size"]:
                raise RuntimeError("local snapshot size differs: " + name)
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        (output / "snapshot_manifest.json").write_bytes(canonical({
            "baseline": baseline, "runtime": initial["runtime"],
            "database": final["database"], "files": hashes,
            "ledger_bytes": initial["ledger_bytes"],
            "health_observations": monitor.observations}))
        # Production monitoring is complete before local compression begins.
        with tarfile.open(output / "snapshot.tar.gz", "w:gz") as archive:
            archive.add(snapshot, arcname="snapshot")
        return snapshot
    except BaseException:
        monitor.stopped.set()
        if monitor.thread:
            monitor.thread.join(timeout=5)
        shutil.rmtree(output)  # This invocation required and created a new path.
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", default="root@38.76.174.234")
    parser.add_argument("--key", type=Path,
                        default=Path.home() / ".ssh/id_marx_cloud_ed25519")
    parser.add_argument("--baseline-seconds", type=int, default=BASELINE_SECONDS,
                        help=argparse.SUPPRESS)
    parser.add_argument("--sample-seconds", type=int, default=SAMPLE_SECONDS,
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.baseline_seconds < 300 or not 10 <= args.sample_seconds <= 30:
        parser.error("production capture requires a >=300s baseline and 10-30s sampling")
    result = capture(args.output, Remote(args.host, args.key),
                     baseline_seconds=args.baseline_seconds,
                     sample_seconds=args.sample_seconds)
    print("Verified read-only production snapshot:", result)


if __name__ == "__main__":
    main()
