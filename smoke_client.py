#!/usr/bin/env python3
"""Smoke test: drive the opencode-hermes-mcp controller over stdio as a client."""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "opencode_run",
    "opencode_answer",
    "opencode_permission",
    "opencode_abort",
    "opencode_inspect",
    "opencode_sessions",
}


async def main():
    cfg = json.load(open(os.path.expanduser("~/.config/hermes/opencode-server.json")))
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "OPENCODE_SERVER_URL": cfg["base_url"],
        "OPENCODE_SERVER_USERNAME": cfg["username"],
        "OPENCODE_SERVER_PASSWORD": cfg["password"],
    }
    params = StdioServerParameters(
        command=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python"),
        args=[os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            print("TOOLS:", sorted(names))
            assert names == EXPECTED_TOOLS, f"tool surface mismatch: {names ^ EXPECTED_TOOLS}"
            print("tool surface OK (exactly the 6 expected tools)")

            # agent required?
            r = await session.call_tool("opencode_run", {"directory": "/tmp/oc-mcp-test/gitrepo", "task": "x"})
            try:
                data = json.loads(r.content[0].text)
            except Exception:
                data = {"isError": r.isError, "raw": (r.content[0].text if r.content else "")[:200]}
            print("run without agent ->", json.dumps(data)[:200])

            r = await session.call_tool(
                "opencode_sessions", {"directory": "/home/arthur/gitlab/erdos-moser-equation"}
            )
            data = json.loads(r.content[0].text)
            print("SESSIONS ok:", data["ok"], "count:", data["count"])


asyncio.run(main())
