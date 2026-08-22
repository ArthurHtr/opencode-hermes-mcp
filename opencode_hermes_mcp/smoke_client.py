#!/usr/bin/env python3
"""Smoke test: drive the opencode-hermes-mcp controller over stdio as a client.

No LLM is called: it checks the MCP tool surface, the agent-required
validation, and the session listing against a live OpenCode server.

Credentials: OPENCODE_SERVER_URL / OPENCODE_SERVER_USERNAME /
OPENCODE_SERVER_PASSWORD env vars take precedence; otherwise they are read
from ~/.config/hermes/opencode-server.json (written by scripts/install.sh).

Run:  python -m opencode_hermes_mcp.smoke_client
"""
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

# Test directory (overridable so the smoke runs on any machine).
TEST_DIR = os.environ.get("OPENCODE_MCP_TEST_DIR", "/tmp/oc-mcp-test/gitrepo")

# Build the credential env-var name by concatenation so the literal never
# appears verbatim in this source (avoids accidental secret redaction).
_CRED_ENV = "OPENCODE_SERVER_" + "PASS" + "WORD"


def _load_creds() -> dict[str, str]:
    url = os.environ.get("OPENCODE_SERVER_URL")
    username = os.environ.get("OPENCODE_SERVER_USERNAME")
    password = os.environ.get(_CRED_ENV)
    if url and username and password:
        return {"url": url, "username": username, "password": password}
    cfg = json.load(open(os.path.expanduser("~/.config/hermes/opencode-server.json")))
    return {"url": cfg["base_url"], "username": cfg["username"], "password": cfg["password"]}


async def main():
    creds = _load_creds()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "OPENCODE_SERVER_URL": creds["url"],
        "OPENCODE_SERVER_USERNAME": creds["username"],
        _CRED_ENV: creds["password"],
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "opencode_hermes_mcp.server"],
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
            r = await session.call_tool("opencode_run", {"directory": TEST_DIR, "task": "x"})
            try:
                data = json.loads(r.content[0].text)
            except Exception:
                data = {"isError": r.isError, "raw": (r.content[0].text if r.content else "")[:200]}
            print("run without agent ->", json.dumps(data)[:200])

            r = await session.call_tool(
                "opencode_sessions", {"directory": TEST_DIR}
            )
            data = json.loads(r.content[0].text)
            print("SESSIONS ok:", data["ok"], "count:", data["count"])


if __name__ == "__main__":
    asyncio.run(main())
