"""One HTTP MCP/REST integration scenario, plus independent schema assertions."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUTPUT = Path(os.environ["AGENT_API_TEST_OUTPUT"])
OUTPUT.mkdir(parents=True, exist_ok=True)


def request(url, payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        response = urlopen(req, timeout=35)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}


def main():
    log = (OUTPUT / "fixture-server.log").open("w", encoding="utf-8")
    fixture = subprocess.Popen([os.environ["AGENT_API_BACKEND_PYTHON"], str(ROOT / "tests/agent_api_fixture_server.py"), str(OUTPUT)],
                               cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=log)
    server = None
    try:
        for _ in range(120):
            if (OUTPUT / "fixture.json").exists(): break
            if fixture.poll() is not None: raise RuntimeError("fixture startup failed")
            time.sleep(0.25)
        config = json.loads((OUTPUT / "fixture.json").read_text("utf-8"))
        rest = f"http://127.0.0.1:{config['port']}/api/v1"
        os.environ["MARX_AGENT_API_UPSTREAM"] = rest
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        import uvicorn
        import agent_api_mcp
        from jsonschema import Draft202012Validator
        from agent_api_contract import OUTPUTS
        server = uvicorn.Server(uvicorn.Config(agent_api_mcp.app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started: break
            time.sleep(0.05)
        assert server.started
        url = f"http://127.0.0.1:{port}/mcp"
        headers = {"Authorization": "Bearer " + config["key"], "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
        status, _ = request(url, {"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
        assert status == 401
        def rpc(ident, method, params=None):
            status, body = request(url, {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}, headers)
            assert status == 200, (status, body)
            assert "result" in body, body
            return body["result"]
        initialized = rpc(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "contract-fixture", "version": "1"}})
        assert initialized["protocolVersion"] == "2025-11-25"
        tools = rpc(2, "tools/list")["tools"]
        assert len(tools) == 4 and all(t["annotations"]["readOnlyHint"] for t in tools)
        initial = request(rest + "/limits", headers=headers)[1]["rate"]["remaining"]
        sources = rpc(3, "tools/call", {"name": "list_sources", "arguments": {}})
        Draft202012Validator(OUTPUTS["list_sources"]).validate(sources["structuredContent"])
        assert sources["structuredContent"]["sources"]
        found = rpc(4, "tools/call", {"name": "search_corpus", "arguments": {"q": "生产关系", "limit": 1}})
        body = found["structuredContent"]
        Draft202012Validator(OUTPUTS["search_corpus"]).validate(body)
        evidence_id = body["results"][0]["evidence_id"]
        passage = rpc(5, "tools/call", {"name": "get_passage", "arguments": {"evidence_id": evidence_id}})
        Draft202012Validator(OUTPUTS["get_passage"]).validate(passage["structuredContent"])
        assert "生产关系" in passage["structuredContent"]["result"]["text"]
        assert request(rest + "/search", {"q": "生产关系", "limit": 1}, headers)[0] == 200
        remaining = request(rest + "/limits", headers=headers)[1]["rate"]["remaining"]
        assert initial - remaining == 4, (initial, remaining)
        invalid = rpc(6, "tools/call", {"name": "search_corpus", "arguments": {"q": "生产关系", "limit": 100}})
        assert invalid["isError"] and invalid["structuredContent"]["error"]["code"] == "invalid_arguments"
        from agent_api_store import Store
        store = Store(OUTPUT / "state")
        with store.admission(999):
            busy = rpc(7, "tools/call", {"name": "search_corpus", "arguments": {"q": "生产关系"}})
            assert busy["isError"] and busy["structuredContent"]["error"]["retry_after_seconds"] == 2
            assert request(rest + "/sources", headers=headers)[0] == 429
        store.revoke(17, store.keys(17)[0]["id"])
        assert request(url, {"jsonrpc": "2.0", "id": 8, "method": "tools/list"}, headers)[0] == 401
        (OUTPUT / "protocol-summary.json").write_text(json.dumps({"status": "passed", "transport": "HTTP legacy 2025-11-25",
            "tools": [t["name"] for t in tools], "mixed_data_calls": 4, "real_clients": "not tested"}, indent=2), "utf-8")
        print("PASS: real REST + MCP HTTP, membership/key auth, schemas, evidence, shared quota/locks, revocation")
    finally:
        if server:
            server.should_exit = True
        fixture.terminate()
        fixture.wait(timeout=10)
        log.close()
        # This fixture credential is synthetic, never a user token.
        (OUTPUT / "fixture.json").unlink(missing_ok=True)


if __name__ == "__main__": main()
