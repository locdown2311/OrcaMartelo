"""stdio transport for the OrcaMartelo MCP server.

MCP clients that launch a command (Claude Desktop, for one) run this script with
the Python bundled with OrcaMartelo. Each JSON-RPC line read from stdin goes to
the LAN bridge's /mcp endpoint and its answer is written to stdout. While the
bridge is not running, the messages are answered here without printer data, so
the client keeps the server and each tool call says how to bring it up.
"""

import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orcamartelo_mcp  # noqa: E402

BRIDGE_URL = os.environ.get("ORCAMARTELO_MCP_URL", "http://127.0.0.1:18988/mcp")


def forward(line):
    """Send one message to the bridge. Returns the answer text ("" for none), or None when unreachable."""
    request = urllib.request.Request(BRIDGE_URL, data=line.encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read().decode("utf-8").strip()
    except urllib.error.HTTPError as e:
        # The bridge answers JSON-RPC errors with a body; anything else means it is not an MCP bridge.
        body = e.read().decode("utf-8", errors="replace").strip()
        return body if body.startswith(("{", "[")) else None
    except (urllib.error.URLError, OSError):
        return None


def main():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for raw in stdin:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        answer = forward(line)
        if answer is None:
            answer = orcamartelo_mcp.handle_text(line, None) or ""
        if answer:
            stdout.write(answer.encode("utf-8") + b"\n")
            stdout.flush()


if __name__ == "__main__":
    main()
