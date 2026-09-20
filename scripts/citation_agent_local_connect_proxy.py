from __future__ import annotations

"""Loopback-only CONNECT proxy for the local citation Agent test lane.

Production uses the separately managed tinyproxy/systemd egress boundary.  This
small standard-library proxy exists only so the Windows administrator preview can
exercise the same single-host HTTPS boundary without installing dependencies.
"""

import argparse
import ipaddress
import selectors
import socket
import socketserver
import sys
import time


MAX_HEADER_BYTES = 8192
DEFAULT_ALLOWED_HOST = "api.deepseek.com"


def _is_public_address(sockaddr: tuple) -> bool:
    try:
        address = ipaddress.ip_address(str(sockaddr[0]))
    except ValueError:
        return False
    return not any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _connect_public(host: str, port: int, timeout: float) -> socket.socket:
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        if not _is_public_address(sockaddr):
            continue
        upstream = socket.socket(family, socktype, proto)
        upstream.settimeout(timeout)
        try:
            upstream.connect(sockaddr)
            upstream.settimeout(None)
            return upstream
        except OSError as exc:
            last_error = exc
            upstream.close()
    raise last_error or OSError("no public address available")


class ConnectProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], allowed_host: str):
        self.allowed_host = allowed_host.strip().lower().rstrip(".")
        super().__init__(server_address, ConnectProxyHandler)


class ConnectProxyHandler(socketserver.BaseRequestHandler):
    server: ConnectProxyServer

    def _reply(self, status: str) -> None:
        self.request.sendall(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode("ascii"))

    def _read_header(self) -> bytes:
        payload = bytearray()
        self.request.settimeout(10)
        while b"\r\n\r\n" not in payload:
            chunk = self.request.recv(2048)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_HEADER_BYTES:
                raise ValueError("header too large")
        return bytes(payload)

    def _target(self, header: bytes) -> tuple[str, int]:
        first_line = header.split(b"\r\n", 1)[0].decode("ascii", "strict")
        parts = first_line.split()
        if len(parts) != 3 or parts[0].upper() != "CONNECT":
            raise ValueError("CONNECT required")
        authority = parts[1]
        if authority.count(":") != 1:
            raise ValueError("invalid authority")
        host, raw_port = authority.rsplit(":", 1)
        host = host.strip().lower().rstrip(".")
        port = int(raw_port)
        if host != self.server.allowed_host or port != 443:
            raise PermissionError("target denied")
        return host, port

    def _relay(self, upstream: socket.socket) -> None:
        self.request.setblocking(False)
        upstream.setblocking(False)
        selector = selectors.DefaultSelector()
        selector.register(self.request, selectors.EVENT_READ, upstream)
        selector.register(upstream, selectors.EVENT_READ, self.request)
        last_activity = time.monotonic()
        try:
            while time.monotonic() - last_activity < 120:
                events = selector.select(timeout=2)
                if not events:
                    continue
                for key, _mask in events:
                    source = key.fileobj
                    target = key.data
                    data = source.recv(65536)
                    if not data:
                        return
                    target.sendall(data)
                    last_activity = time.monotonic()
        finally:
            selector.close()

    def handle(self) -> None:
        upstream: socket.socket | None = None
        try:
            host, port = self._target(self._read_header())
            upstream = _connect_public(host, port, 10)
            self._reply("200 Connection Established")
            self._relay(upstream)
        except PermissionError:
            self._reply("403 Forbidden")
        except (OSError, UnicodeError, ValueError):
            try:
                self._reply("502 Bad Gateway")
            except OSError:
                pass
        finally:
            if upstream is not None:
                upstream.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local citation Agent HTTPS allowlist proxy")
    parser.add_argument("--host", default="127.0.0.2")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--allowed-host", default=DEFAULT_ALLOWED_HOST)
    args = parser.parse_args()
    if not ipaddress.ip_address(args.host).is_loopback:
        raise SystemExit("proxy must bind to a loopback address")
    with ConnectProxyServer((args.host, args.port), args.allowed_host) as server:
        print(f"citation Agent proxy ready on {args.host}:{args.port}", flush=True)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            sys.exit(0)


if __name__ == "__main__":
    main()
