"""QUA-2851 spike (offline): start QualGent-MCP over stdio against the fake API and call
every tool the creator template lists, so the fake's request log is the route inventory.
Prints tool names and each call's outcome; writes nothing but the fake's log.

    <QualGent-MCP venv>/bin/python probe_qgmcp.py <qualgent-mcp exe> http://127.0.0.1:18731
"""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CREATOR_QUALGENT_TOOLS = ["list_apps", "list_categories", "list_credentials",
                          "list_test_cases", "get_test_case", "create_test_case",
                          "upload_test_file", "check_credits"]


async def main(exe: str, api: str) -> None:
    params = StdioServerParameters(command=exe, args=[], env={
        "QUALGENT_API_URL": api, "QUALGENT_API_KEY": "qg_spike_fake", "PATH": "/usr/bin:/bin"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = (await s.list_tools()).tools
            names = sorted(t.name for t in tools)
            print("server:", init.serverInfo.name, init.serverInfo.version)
            print("tools:", len(names), names)
            res = await s.list_resources()
            print("resources:", [str(x.uri) for x in res.resources])
            missing = [t for t in CREATOR_QUALGENT_TOOLS if t not in names]
            print("creator tools missing from server:", missing)
            calls = [
                ("list_apps", {}), ("list_categories", {}), ("list_credentials", {}),
                ("list_test_cases", {}), ("check_credits", {}),
                ("create_test_case", {"name": "Probe case", "expected_result": "x",
                                      "steps": [{"description": "Open the app", "kind": "setup"}]}),
                ("list_test_cases", {}),
            ]
            created = None
            for name, args in calls:
                out = await s.call_tool(name, args)
                text = " ".join(getattr(c, "text", "") for c in out.content)
                print(f"{name}: isError={out.isError} -> {text[:200]}")
                if name == "create_test_case" and not out.isError:
                    created = json.loads(text).get("id")
            if created:
                out = await s.call_tool("get_test_case", {"test_case_id": created})
                print("get_test_case:", out.isError, " ".join(c.text for c in out.content)[:200])


asyncio.run(main(sys.argv[1], sys.argv[2]))
