"""Bounded, read-only public TLS/HTTP probes. Never logs response bodies or cookies."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import socket
import ssl
import time
from datetime import datetime, timezone


def addresses(host):
    return sorted({row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})


def probe(host, address, *, port=443, path="/", timeout=8):
    """Connect to a specific address while preserving hostname verification and SNI."""
    result = {"at": datetime.now(timezone.utc).isoformat(), "host": host,
              "address": address, "port": port, "path": path.split("?", 1)[0]}
    start = time.monotonic()
    sock = None
    try:
        sock = socket.create_connection((address, port), timeout)
        if port == 443:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            cert = sock.getpeercert()
            result.update(tls_verified=True, tls_version=sock.version(),
                          certificate_sha256=hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest(),
                          subject=cert.get("subject"), issuer=cert.get("issuer"),
                          names=cert.get("subjectAltName"), expires=cert.get("notAfter"))
        connection = http.client.HTTPConnection(host, timeout=timeout)
        connection.sock = sock
        connection.request("HEAD", path, headers={"Host": host, "User-Agent": "MarxAccessProbe/1.0", "Connection": "close"})
        response = connection.getresponse()
        result.update(status=response.status, server=response.getheader("Server"),
                      ray=response.getheader("CF-Ray"))
        # Monitor only fixed non-sensitive routes. Strip query/fragment from redirects.
        location = response.getheader("Location")
        if location:
            result["location"] = location.split("?", 1)[0].split("#", 1)[0]
        connection.close()
    except ssl.SSLCertVerificationError as exc:
        result.update(tls_verified=False, error="certificate_verification_failed", verify_code=exc.verify_code)
        # Collect only the public leaf fingerprint on a new TLS connection.
        # No HTTP request or credentials are ever sent over this connection.
        try:
            untrusted = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            untrusted.check_hostname = False
            untrusted.verify_mode = ssl.CERT_NONE
            with socket.create_connection((address, port), timeout) as diagnostic_sock:
                with untrusted.wrap_socket(diagnostic_sock, server_hostname=host) as diagnostic_tls:
                    der = diagnostic_tls.getpeercert(binary_form=True)
                    if der:
                        result["untrusted_certificate_sha256"] = hashlib.sha256(der).hexdigest()
        except (OSError, ssl.SSLError):
            pass
    except (OSError, http.client.HTTPException) as exc:
        result.update(error=type(exc).__name__)
    finally:
        if sock:
            sock.close()
        result["seconds"] = round(time.monotonic() - start, 3)
    return result


def make_logger(directory):
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Only this tool's fixed filenames; never follow symlinks or remove directories.
    for suffix in [""] + [f".{n}" for n in range(1, 8)]:
        item = root / ("access-probes.jsonl" + suffix)
        if item.is_symlink():
            raise ValueError("probe logs must not be symlinks")
        if item.is_file() and time.time() - item.stat().st_mtime > 7 * 86400:
            item.unlink()
    handler = RotatingFileHandler(root / "access-probes.jsonl", maxBytes=6*1024*1024,
                                  backupCount=7, encoding="utf-8")
    logger = logging.getLogger("access-probes")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for old in list(logger.handlers):
        old.close()
        logger.removeHandler(old)
    logger.addHandler(handler)
    return logger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--duration", type=int, default=0, help="seconds; 0 = one round")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--label", default="local")
    args = parser.parse_args()
    if args.duration < 0 or args.duration > 7*86400 or args.interval < 60:
        parser.error("duration must be 0..604800; interval >= 60")
    hosts = args.host or ["mazhuzuojiansuo.com", "www.mazhuzuojiansuo.com"]
    if any(not h or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in h) for h in hosts):
        parser.error("host must be a DNS hostname")
    logger = make_logger(args.output)
    deadline = time.monotonic() + args.duration
    while True:
        started = time.monotonic()
        count = failures = 0
        for host in hosts:
            try:
                targets = addresses(host)
            except OSError:
                targets = []
                logger.info(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "host": host,
                                        "label": args.label, "error": "dns_resolution_failed"}))
                failures += 1
            for address in targets:
                for port in (443, 80):
                    row = probe(host, address, port=port)
                    row["label"] = args.label
                    logger.info(json.dumps(row, ensure_ascii=False))
                    count += 1
                    failures += int("error" in row or row.get("status", 0) >= 500)
        print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "probes": count, "failures": failures}), flush=True)
        if not args.duration or time.monotonic() >= deadline:
            return
        time.sleep(max(0, min(args.interval - (time.monotonic() - started), deadline - time.monotonic())))


if __name__ == "__main__":
    main()
