"""QUA-2851 spike: ONE CreateBench v2 creation episode, by monkeypatching the journey
episode runner. Throwaway — QUA-2856 builds `--mode create` properly.

What it reuses unchanged: journey staging of the CLEAN build (install, pm clear,
device_setup fixture, empty flags file, pinned clock, launch), the ADB + MCP meters,
frame capture, the codex adapter (per-episode CODEX_HOME), the transcript.

What it patches (in-process only):
  * the instruction -> a creation prompt: feature brief + device note + approval note
  * the MCP config -> the bench's metered `device` server PLUS a `qualgent` stdio
    server (QualGent-MCP from a PRIVATE checkout, QUALGENT_API_URL -> the fake API)
  * codex config.toml -> `developer_instructions` = the qualgent-test-creator template
    body, read at RUNTIME from the private DevLoop-MCP checkout (the Codex rendering the
    desktop app installs puts the body in `developer_instructions` the same way). The
    text never enters this repository; it lands only under the gitignored run dir.
  * the step budget -> --budget (default 150)

    VIRTUAL_ENV= uv run python scratch/qua-2851/create_episode.py \
        --template <DevLoop-MCP>/subagent-templates/qualgent-test-creator.md \
        --qualgent-mcp <QualGent-MCP>/.venv/bin/qualgent-mcp --api http://127.0.0.1:18731 \
        --mcp-server http://127.0.0.1:51871 --device emulator-5554 \
        --brief scratch/qua-2851/briefs/medtimer-add-medicine.md --runs-dir scratch/qua-2851/out/create
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from qualgentbench import bugs as bugmod
from qualgentbench import episode_runner as er
from qualgentbench import journey
from qualgentbench.adapters import codex_cli
from qualgentbench.schemas import Condition

# The route whose CLEAN version is staged. Only its staging is used (fixture, flags
# off); its steps are never shown to the creator.
STAGING_TASK = "medtimer-add-medicine-back-to-list~clean"


def template_body(path: Path) -> str:
    text = path.read_text()
    if text.startswith("---"):
        _, _, rest = text.partition("\n---")
        text = rest.partition("\n")[2]
    return text.strip()


def creation_prompt(app_name: str, bundle_id: str, serial: str, brief: str) -> str:
    # Public-safe: our own words. The private template reaches the agent only through
    # developer_instructions.
    return f"""You are working as the qualgent-test-creator agent described in your developer instructions.

## Device & app
The Android app `{app_name}` (`{bundle_id}`) is already installed and running on Android device `{serial}`.
Device tools come from the `device` MCP server; every device tool takes the device as its first argument - always pass device="{serial}".
There are no device-lock tools in this session: the device is already reserved for you, so skip any acquire or release step.
QualGent test-management tools (list_test_cases, list_categories, create_test_case, ...) come from the `qualgent` MCP server.

## No source code
There is no app source code in this session. Skip any step that reads the codebase and learn the app only from the running device.

## Feature brief
{brief.strip()}

## Approval
The user approved this request in advance: approval is granted. Nobody will answer questions during this session, so do not wait for a reply. When your draft is ready, submit it with create_test_case, then finish with a short summary that includes the created test case id.
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=Path, required=True)
    ap.add_argument("--qualgent-mcp", required=True)
    ap.add_argument("--api", required=True)
    ap.add_argument("--mcp-server", required=True)
    ap.add_argument("--device", required=True)
    ap.add_argument("--brief", type=Path, required=True)
    ap.add_argument("--runs-dir", type=Path, required=True)
    ap.add_argument("--apk", type=Path,
                    default=Path.home() / ".cache/qualgentbench/apps/journey/medtimer/medtimer-buggy.apk")
    ap.add_argument("--model", default="gpt-6-astra")
    ap.add_argument("--budget", type=int, default=150)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    body = template_body(a.template)
    brief = a.brief.read_text()

    suite = next(s for s in bugmod.load_apps() if s["app"]["id"] == "medtimer")
    task = next(t for t in journey.journey_tasks(suite) if t.id == STAGING_TASK)
    task.bug_spec["step_budget"] = a.budget

    orig_cfg = er._generate_mcp_config
    er._generate_mcp_config = lambda url: {"mcpServers": {
        **orig_cfg(url)["mcpServers"],
        "qualgent": {"command": a.qualgent_mcp, "args": [],
                     "env": {"QUALGENT_API_URL": a.api, "QUALGENT_API_KEY": "qg_spike_fake"}},
    }}
    er._ablation_instruction = lambda t, serial, tooling: creation_prompt(
        t.app_name, t.bundle_id, serial, brief)

    orig_toml = codex_cli.CodexCliAdapter._config_toml

    def _toml(self, ctx):
        s = orig_toml(self, ctx)
        head, _, rest = s.partition("\n\n")
        return head + "\ndeveloper_instructions = " + json.dumps(body) + "\n\n" + rest

    codex_cli.CodexCliAdapter._config_toml = _toml

    async def go():
        session = er.DeviceSession(a.mcp_server)
        task.bundle_id = await er.prepare_app(session, a.device, a.apk, task)
        opts = er.EpisodeOptions(
            agent="codex-cli", model=a.model, condition=Condition.no_routines, trial=1,
            mcp_server=a.mcp_server, runs_dir=a.runs_dir.resolve(), task_type="create_spike",
            device_serial=a.device, tooling="mcp", apk_path=a.apk, force_model=a.model,
            app_id="medtimer", condition_label="create")
        t0 = time.monotonic()
        res = await er.run_episode(task, opts)
        wall = time.monotonic() - t0
        print(json.dumps({"wall_sec_total": round(wall, 1),
                          "result": getattr(res, "__dict__", str(res))}, default=str, indent=2)[:4000])

    asyncio.run(go())


if __name__ == "__main__":
    main()
