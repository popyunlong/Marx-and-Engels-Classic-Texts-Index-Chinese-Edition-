"""Isolated MCP cancellation scenario; invoked by the once-only acceptance runner."""
import asyncio
import threading
from unittest.mock import patch

import agent_api_mcp as mcp

started = threading.Event()
release = threading.Event()
calls = []


def forward(method, path, auth, payload):
    calls.append((method, path, auth, payload))
    if path == '/sources':
        started.set()
        release.wait(3)
    else:
        release.set()
    return 200, {'ok': True}


async def run():
    token = mcp.TRACE.set('c' * 32)
    try:
        task = asyncio.create_task(mcp.forward('GET', '/sources', authorization='Bearer isolated'))
        while not started.is_set():
            await asyncio.sleep(.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('Cancellation must propagate to the MCP transport')
    finally:
        release.set()
        mcp.TRACE.reset(token)


if __name__ == '__main__':
    with patch.object(mcp, 'forward_sync', forward):
        asyncio.run(run())
    assert calls[-1] == ('POST', '/cancel', 'Bearer isolated', {'request_id': 'c' * 32})
    print('MCP cancellation preserves account identity and shared queue request id')
