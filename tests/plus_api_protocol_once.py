"""Single mixed-transport scenario; run in the existing MCP Python runtime."""
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUTPUT = Path(os.environ["PLUS_API_OUTPUT"])


def request(url, payload=None, key=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-11-25"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = Request(url, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
    try:
        response = build_opener(ProxyHandler({})).open(req, timeout=30)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read())


def wait_for(fn, timeout=5):
    end = time.monotonic() + timeout
    while not fn():
        if time.monotonic() >= end:
            raise AssertionError("Fixture did not reach the expected state")
        time.sleep(.02)


def main():
    config_path = OUTPUT / "protocol-fixture.json"
    fixture_env = dict(os.environ, MARX_AGENT_API_LIGHT_CONCURRENCY="1",
                       PYTHONPATH=os.environ["PLUS_API_BACKEND_PATH"])
    log = (OUTPUT / "protocol-fixture.log").open("w", encoding="utf-8")
    fixture = subprocess.Popen([os.environ["PYTHON"], "-B", "-u", str(ROOT / "tests/plus_api_fixture_server.py"), str(config_path)],
        cwd=ROOT, env=fixture_env, stdout=log, stderr=log)
    server, thread, config = None, None, None
    try:
        wait_for(lambda: config_path.exists() or fixture.poll() is not None, 60)
        assert fixture.poll() is None, "Backend fixture failed; inspect protocol-fixture.log"
        config = json.loads(config_path.read_text("utf-8"))
        base = config["base"]
        os.environ["MARX_AGENT_API_UPSTREAM"] = base + "/api/v1"
        import uvicorn
        import agent_api_mcp
        from agent_api_store import Store
        from agent_api_contract import OUTPUTS
        from jsonschema import Draft202012Validator
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(agent_api_mcp.app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        wait_for(lambda: server.started)
        mcp = f"http://127.0.0.1:{port}/mcp"
        keys, users = config["keys"], config["users"]
        store = Store(config["state"])
        def rpc(name, tool, ident, key=None):
            status, body = request(mcp, {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                "params": {"name": tool, "arguments": {}}}, key or keys[name])
            assert status == 200, (status, body)
            result = body["result"]
            assert not result["isError"], result
            Draft202012Validator(OUTPUTS[tool]).validate(result["structuredContent"])
            return result["structuredContent"]
        status, listing = request(mcp, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, keys["pro"])
        assert status == 200
        limits_tool = next(t for t in listing["result"]["tools"] if t["name"] == "get_api_limits")
        assert limits_tool["outputSchema"]["oneOf"][0]["properties"]["scheduling"]["properties"]["priority"]["enum"] == ["normal", "high", "highest"]
        for i, (name, priority) in enumerate([("plus", "normal"), ("pro", "high"), ("max", "highest")], 10):
            rest_limits = request(base + "/api/v1/limits", key=keys[name])[1]
            mcp_limits = rpc(name, "get_api_limits", i)
            assert rest_limits == mcp_limits
            assert mcp_limits["scheduling"]["priority"] == priority
        assert request(base + "/fixture/pause/" + str(users["plus"]), {})[0] == 200
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            running = pool.submit(request, base + "/api/v1/sources", None, keys["plus"])
            try:
                wait_for(lambda: request(base + "/fixture/status")[1]["entered"])
                pending = []
                for count, (name, transport, priority) in enumerate([
                    ("plus2", "rest", 0), ("pro", "mcp", 1), ("max", "rest", 2), ("max2", "mcp", 2),
                ], 1):
                    future = (pool.submit(rpc, name, "list_sources", 20 + count) if transport == "mcp"
                              else pool.submit(request, base + "/api/v1/sources", None, keys[name]))
                    pending.append((transport, future))
                    def queued():
                        with store.connect() as db:
                            rows = list(db.execute("SELECT user_id,priority FROM api_waiters"))
                        return len(rows) == count and any(r["user_id"] == users[name] and r["priority"] == priority for r in rows)
                    wait_for(queued)
                assert not running.done() and not any(f.done() for _, f in pending)
                assert request(base + "/fixture/status")[1]["executed"] == [users["plus"]]
            finally:
                request(base + "/fixture/release", {})
            assert running.result(timeout=10)[0] == 200
            for transport, future in pending:
                result = future.result(timeout=10)
                if transport == "rest":
                    assert result[0] == 200
                    Draft202012Validator(OUTPUTS["list_sources"]).validate(result[1])
            expected = [users[n] for n in ("plus", "max", "max2", "pro", "plus2")]
            assert request(base + "/fixture/status")[1]["executed"] == expected
        rpc("plus2", "list_sources", 30, config["second_key"])
        assert request(base + "/api/v1/limits", key=keys["plus2"])[1]["rate"]["remaining"] == 28
        assert rpc("plus2", "get_api_limits", 31, config["second_key"])["rate"]["remaining"] == 28
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM api_waiters").fetchone()[0] == 0
        (OUTPUT / "protocol-summary.json").write_text(json.dumps({
            "status": "passed", "scenario": "running Plus, then mixed REST/MCP Max FIFO > Pro > Plus",
            "shared_key_quota": True, "tiers": ["normal", "high", "highest"],
            "scope": "Actual loopback HTTP routes and MCP adapter; no public endpoint or desktop client acceptance",
        }, indent=2), encoding="utf-8")
        print("PASS: mixed REST/MCP three-tier ordering, FIFO, running nonpreemption, live schemas and shared Key quota")
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(5)
        if config and fixture.poll() is None:
            request(config["base"] + "/fixture/shutdown", {})
        try:
            fixture.wait(timeout=10)
        except subprocess.TimeoutExpired:
            fixture.terminate()
            fixture.wait(timeout=5)
        log.close()
        config_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
