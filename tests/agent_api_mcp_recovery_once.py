"""Run once in the existing MCP environment; no production/network requests."""
import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_api_mcp as mcp


class RecoveryTests(unittest.TestCase):
    def test_transport_timeout_does_not_advertise_retry(self):
        with patch.object(mcp, "build_opener") as build:
            build.return_value.open.side_effect = TimeoutError()
            status, body = mcp.forward_sync("POST", "/search", "Bearer marx_fixture", {"q": "生产关系"})
            self.assertEqual(status, 504)
            self.assertFalse(body["error"]["retryable"])
            self.assertEqual(build.return_value.open.call_count, 1)

    def test_ingress_limit_expires_and_ignores_loopback(self):
        limiter = mcp.IngressLimit()
        with patch.object(mcp.time, "monotonic", return_value=100):
            self.assertTrue(all(limiter.admit("1.1.1.1") == 0 for _ in range(600)))
            self.assertGreater(limiter.admit("1.1.1.1"), 0)
            self.assertEqual(limiter.admit("127.0.0.1"), 0)
        with patch.object(mcp.time, "monotonic", return_value=161):
            self.assertEqual(limiter.admit("1.1.1.1"), 0)

    def test_every_response_is_private_and_correlated(self):
        async def invoke(auth, body):
            events = []
            scope = {"type": "http", "path": "/mcp", "method": "POST", "client": ("127.0.0.1", 1),
                     "headers": [(b"authorization", auth)] if auth else []}
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}
            async def send(event):
                events.append(event)
            await mcp.app(scope, receive, send)
            return events[0], json.loads(b"".join(e.get("body", b"") for e in events[1:]))

        async def accepted(*args, **kwargs):
            return 200, {"ok": True}

        async def sdk(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": [(b"cache-control", b"public")]})
            await send({"type": "http.response.body", "body": b'{"result":{}}'})

        with patch.object(mcp, "forward", accepted), patch.object(mcp, "transport", sdk):
            cases = [(b"", b"{}", 401), (b"Bearer marx_fixture", b"x" * 16385, 413),
                     (b"Bearer marx_fixture", b"{}", 200)]
            ids = []
            for auth, body, expected in cases:
                start, _ = asyncio.run(invoke(auth, body))
                headers = dict(start["headers"])
                self.assertEqual(start["status"], expected)
                self.assertEqual(headers[b"cache-control"], b"private, no-store")
                self.assertRegex(headers[b"x-request-id"].decode(), r"^[0-9a-f]{32}$")
                ids.append(headers[b"x-request-id"])
            self.assertEqual(len(set(ids)), 3)
        self.assertEqual(mcp.TRACE.get(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
