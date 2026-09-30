"""QUA-2851 spike (offline): does the codex adapter carry TWO MCP servers?

Renders the adapter's own config.toml from an mcp_config.json holding the bench's
`device` (http, DevLoop) server plus a `qualgent` stdio server (QualGent-MCP -> fake
API), then asks codex itself (`codex mcp list --json`) what it sees. No model call.

    uv run python scratch/qua-2851/multi_server_check.py <out_dir> <devloop_url> <qualgent-mcp exe> <fake api>
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from qualgentbench.adapters.base import RunContext
from qualgentbench.adapters.codex_cli import CodexCliAdapter
from qualgentbench.schemas import Condition

out, devloop, qg_exe, api = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
run_dir = out / "multi_server"
(run_dir / "workspace").mkdir(parents=True, exist_ok=True)
cfg = {"mcpServers": {
    "device": {"type": "http", "url": f"{devloop.rstrip('/')}/mcp"},
    "qualgent": {"command": qg_exe, "args": [],
                 "env": {"QUALGENT_API_URL": api, "QUALGENT_API_KEY": "qg_spike_fake"}},
}}
(run_dir / "mcp_config.json").write_text(json.dumps(cfg, indent=2))
ctx = RunContext(task=None, agent="codex-cli", model="gpt-6-astra",
                 condition=list(Condition)[0], trial=1, run_dir=run_dir, mcp_server=devloop,
                 mcp_config_path=run_dir / "mcp_config.json",
                 workspace_dir=run_dir / "workspace", inject_mcp=False,
                 disabled_tools=["mobile_workspace_info"], tool_call_cap=None)
ad = CodexCliAdapter()
home = ad._codex_home(ctx)
home.mkdir(parents=True, exist_ok=True)
toml = ad._config_toml(ctx)
(home / "config.toml").write_text(toml)
print(toml)
env = {**os.environ, **ad.env(ctx)}
p = subprocess.run(["codex", "mcp", "list", "--json"], env=env, capture_output=True, text=True)
print("codex mcp list rc", p.returncode)
print(p.stdout[:3000], p.stderr[:1000])
