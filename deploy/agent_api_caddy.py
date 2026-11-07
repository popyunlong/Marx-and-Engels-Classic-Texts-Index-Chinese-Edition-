"""Patch only the managed MCP route inside an existing release transaction."""
import re
import sys
from pathlib import Path

BEGIN = "\t# BEGIN marx-agent-api-v1\n"
END = "\t# END marx-agent-api-v1\n"


def patch(source, port=None):
    if BEGIN in source:
        start = source.index(BEGIN)
        end = source.index(END, start) + len(END)
        source = source[:start] + source[end:]
    if port is None:
        return source
    if not 1024 <= int(port) <= 65535:
        raise ValueError("invalid sidecar port")
    matches = list(re.finditer(r"(?m)^mazhuzuojiansuo\.com\s*\{[^\S\n]*\n", source))
    if len(matches) != 1:
        raise ValueError("actual site block requires review")
    pos = matches[0].end()
    block = BEGIN + f"\t@agent_api_mcp path /mcp\n\treverse_proxy @agent_api_mcp 127.0.0.1:{int(port)}\n" + END
    return source[:pos] + block + source[pos:]


if __name__ == "__main__":
    target = Path(sys.argv[1])
    target.write_text(patch(target.read_text(), int(sys.argv[2]) if len(sys.argv) > 2 else None))
