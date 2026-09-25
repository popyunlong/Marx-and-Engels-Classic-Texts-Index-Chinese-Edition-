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
import statistics
import subprocess
import tarfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTE_SOURCE = (ROOT / "scripts/catalog_snapshot_remote.py").read_bytes()
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


def percentile95(values):
    values = sorted(values)
    return values[max(0, (95 * len(values) + 99) // 100 - 1)]


def observed_p95(sample):
    probes = [item["seconds"] for item in sample["probes"].values()]
    return max([sample["p95"] or 0, *probes])


def iowait(previous, current):
    total = current["cpu_total"] - previous["cpu_total"]
    return max(0, current["cpu_iowait"] - previous["cpu_iowait"]) / total if total > 0 else 0


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
        self.baseline_p95 = 0.0
        self.baseline_iowait = 0.0

    def measure_baseline(self):
        samples = []
        previous = self.remote.json("metrics")
        if previous["five_xx"]:
            raise RuntimeError("5xx already present before snapshot")
        windows = max(1, self.baseline_seconds // self.sample_seconds)
        if self.baseline_seconds <= 0:
            samples.append((observed_p95(previous), 0.0))
        for _ in range(windows if self.baseline_seconds > 0 else 0):
            time.sleep(self.sample_seconds)
            current = self.remote.json("metrics")
            if current["five_xx"]:
                raise RuntimeError("5xx present during snapshot baseline")
            samples.append((observed_p95(current), iowait(previous, current)))
            previous = current
        self.baseline_p95 = statistics.median(item[0] for item in samples)
        self.baseline_iowait = statistics.median(item[1] for item in samples)
        return {"p95_seconds": self.baseline_p95,
                "iowait_fraction": self.baseline_iowait,
                "windows": len(samples)}

    def _fail(self, reason):
        self.reason = reason
        self.alarm.set()
        self.remote.terminate_active()

    def _run(self):
        previous = None
        slow_windows = busy_windows = 0
        while not self.stopped.wait(self.sample_seconds):
            try:
                sample = self.remote.json("metrics")
            except Exception as exc:  # noqa: BLE001 - monitoring failure must abort capture
                self._fail("production health metrics unavailable: " + str(exc))
                return
            if sample["five_xx"]:
                self._fail("new production 5xx during snapshot")
                return
            p95 = observed_p95(sample)
            limit = max(self.baseline_p95 * 1.2, self.baseline_p95 + 0.1)
            slow_windows = slow_windows + 1 if p95 > limit else 0
            if previous is not None:
                wait = iowait(previous, sample)
                busy_windows = busy_windows + 1 if wait > self.baseline_iowait + 0.05 else 0
            previous = sample
            if slow_windows >= 2 or busy_windows >= 2:
                self._fail("production latency or disk wait exceeded snapshot limit")
                return

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
            self.thread.join(timeout=self.sample_seconds + 10)
        self.check()
        final = self.remote.json("metrics")
        if final["five_xx"]:
            raise RuntimeError("new production 5xx in final snapshot window")
        limit = max(self.baseline_p95 * 1.2, self.baseline_p95 + 0.1)
        if observed_p95(final) > limit:
            raise RuntimeError("production latency rose in final snapshot window")


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
        return ["ssh", "-i", str(self.key), "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=15", self.host, remote]

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
            "ledger_bytes": initial["ledger_bytes"]}))
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
    result = capture(args.output, Remote(args.host, args.key),
                     baseline_seconds=args.baseline_seconds,
                     sample_seconds=args.sample_seconds)
    print("Verified read-only production snapshot:", result)


if __name__ == "__main__":
    main()
