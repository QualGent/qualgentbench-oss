"""The CreateBench v2 creation arm: pins resolved up front, the surface injected at run
time, SHAs recorded (QUA-2852).

Every repository here is a throwaway git repo made in tmp_path with PUBLIC stand-in
text; the real private checkouts are only touched by the opt-in live smoke at the end
(`QGB_PRIVATE_QUALGENT_MCP` / `QGB_PRIVATE_DEVLOOP`, read at import because the suite
strips `QGB_*` before each test).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from qualgentbench.cli import main
from qualgentbench.config import REPO_ROOT, BenchConfig, CreateArm, RepoPin
from qualgentbench.create import arm as armmod
from qualgentbench.create.arm import (
    FAKE_API_KEY,
    ArmError,
    creation_prompt,
    materialize_qualgent_mcp,
    parse_pin,
    parse_template,
    probe_arm,
    resolve_arm,
    resolve_repo,
)
from qualgentbench.create.fake_api import AUTHORED_CASE_FILE

PRIVATE_QUALGENT_MCP = os.environ.get("QGB_PRIVATE_QUALGENT_MCP")
PRIVATE_DEVLOOP = os.environ.get("QGB_PRIVATE_DEVLOOP")

TEMPLATE_PATH = "subagent-templates/qualgent-test-creator.md"
STAND_IN_TEMPLATE = textwrap.dedent("""\
    ---
    name: qualgent-test-creator
    description: stand-in template for tests
    model: inherit
    tools:
      qualgent:
        - list_categories
        - list_test_cases
        - create_test_case
      devloop:
        - mobile_observe_screen
    ---
    Stand-in creator instructions. Explore, draft, then create the case.
    """)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                          text=True).stdout.strip()


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def repos(tmp_path):
    qg = _repo(tmp_path / "QualGent-MCP", {"pyproject.toml": "[project]\nname='x'\n",
                                           "src/pkg/__init__.py": ""})
    dl = _repo(tmp_path / "DevLoop-MCP", {TEMPLATE_PATH: STAND_IN_TEMPLATE})
    _git(dl, "tag", "v1")
    return qg, dl


def _spec(qg: Path, dl: Path, **kw) -> CreateArm:
    return CreateArm(qualgent_mcp=RepoPin(path=str(qg), ref=kw.pop("qg_ref", "main")),
                     devloop=RepoPin(path=str(dl), ref=kw.pop("dl_ref", "main")), **kw)


# ── pins ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spec,expected", [
    ("../QualGent-MCP@main", RepoPin(path="../QualGent-MCP", ref="main")),
    ("/abs/DevLoop-MCP@8fb4ce7", RepoPin(path="/abs/DevLoop-MCP", ref="8fb4ce7")),
    ("https://github.com/org/repo.git@feature/x",
     RepoPin(git_url="https://github.com/org/repo.git", ref="feature/x")),
    ("git@github.com:org/repo.git@v1.2", RepoPin(git_url="git@github.com:org/repo.git",
                                                 ref="v1.2")),
])
def test_parse_pin(spec, expected):
    assert parse_pin(spec) == expected


@pytest.mark.parametrize("spec", ["../QualGent-MCP", "git@github.com:org/repo.git",
                                  "@main", "path@"])
def test_parse_pin_requires_a_ref(spec):
    with pytest.raises(ArmError):
        parse_pin(spec)


def test_a_pin_names_exactly_one_source():
    with pytest.raises(ValueError):
        RepoPin(ref="main")
    with pytest.raises(ValueError):
        RepoPin(path="a", git_url="https://x/y.git", ref="main")


def test_config_carries_a_create_arm():
    cfg = BenchConfig.model_validate({
        "agent": "codex-cli", "model": "gpt-6-astra", "scope": {"apps": ["medtimer"]},
        "create_arm": {"name": "baseline",
                       "qualgent_mcp": {"path": "../QualGent-MCP", "ref": "8fb4ce7"},
                       "devloop": {"git_url": "https://github.com/o/DevLoop-MCP.git",
                                   "ref": "main"},
                       "qualgent_tools": ["create_test_case"]}})
    assert cfg.create_arm.name == "baseline"
    assert cfg.create_arm.template == TEMPLATE_PATH
    assert BenchConfig.model_validate({"agent": "a", "model": "m",
                                       "scope": {"apps": ["x"]}}).create_arm is None


# ── resolution ────────────────────────────────────────────────────────────────

def test_resolve_records_full_shas(repos, tmp_path):
    qg, dl = repos
    arm = resolve_arm(_spec(qg, dl, dl_ref="v1"), cache_root=tmp_path / "cache")
    assert arm.qualgent_mcp.sha == _git(qg, "rev-parse", "HEAD")
    assert arm.devloop.sha == _git(dl, "rev-parse", "HEAD")
    m = arm.manifest()
    assert m["qualgent_mcp"] == {"source": str(qg.resolve()), "ref": "main",
                                 "sha": arm.qualgent_mcp.sha}
    assert m["devloop"]["sha"] == arm.devloop.sha and m["devloop"]["ref"] == "v1"
    assert len(m["devloop"]["template_sha256"]) == 64
    assert m["surface_note_sha256"] == armmod.surface_note_sha256()
    written = json.loads(arm.write_manifest(tmp_path / "ep").read_text())
    assert written == m
    # Hashes and names only: the template body is never in the manifest.
    assert "Stand-in creator instructions" not in json.dumps(written)


def test_a_short_sha_resolves_to_the_full_one(repos, tmp_path):
    qg, dl = repos
    full = _git(qg, "rev-parse", "HEAD")
    arm = resolve_arm(_spec(qg, dl, qg_ref=full[:7]), cache_root=tmp_path)
    assert arm.qualgent_mcp.sha == full


@pytest.mark.parametrize("which", ["qg_ref", "dl_ref"])
def test_an_invalid_ref_fails_fast(repos, tmp_path, which):
    qg, dl = repos
    with pytest.raises(ArmError, match="does not resolve to a commit"):
        resolve_arm(_spec(qg, dl, **{which: "no-such-branch"}), cache_root=tmp_path)


def test_a_path_that_is_not_a_checkout_fails(tmp_path, repos):
    _, dl = repos
    (tmp_path / "plain").mkdir()
    with pytest.raises(ArmError, match="not a git checkout"):
        resolve_arm(_spec(tmp_path / "plain", dl), cache_root=tmp_path)
    with pytest.raises(ArmError, match="not a directory"):
        resolve_arm(_spec(tmp_path / "missing", dl), cache_root=tmp_path)


def test_a_missing_template_fails(repos, tmp_path):
    qg, dl = repos
    with pytest.raises(ArmError, match="does not exist at"):
        resolve_arm(_spec(qg, dl, template="subagent-templates/nope.md"), cache_root=tmp_path)


@pytest.mark.parametrize("raw,match", [
    ("no frontmatter at all\n", "no YAML frontmatter"),
    ("---\nname: x\ntools:\n  devloop: [a]\n---\nbody\n", "no `tools.qualgent` list"),
    ("---\nname: x\ntools:\n  qualgent: [a]\n---\n\n", "body is empty"),
])
def test_a_malformed_template_fails(raw, match):
    with pytest.raises(ArmError, match=match):
        parse_template(raw, path="t.md")


def test_only_committed_content_is_read(repos, tmp_path):
    qg, dl = repos
    (dl / TEMPLATE_PATH).write_text(STAND_IN_TEMPLATE.replace("Stand-in", "UNCOMMITTED"))
    arm = resolve_arm(_spec(qg, dl), cache_root=tmp_path)
    assert "UNCOMMITTED" not in arm.template.body
    assert "Stand-in creator instructions" in arm.template.developer_instructions


def test_a_git_url_pin_is_mirrored_into_the_cache(repos, tmp_path):
    _, dl = repos
    cache = tmp_path / "cache"
    pin = RepoPin(git_url=f"file://{dl}", ref="main")
    first = resolve_repo(pin, role="devloop", cache_root=cache)
    assert first.sha == _git(dl, "rev-parse", "HEAD")
    assert cache in first.git_dir.parents and (first.git_dir / "HEAD").exists()
    (dl / "later.txt").write_text("x")
    _git(dl, "add", "-A")
    _git(dl, "commit", "-q", "-m", "later")
    second = resolve_repo(pin, role="devloop", cache_root=cache)      # fetches the new head
    assert second.sha == _git(dl, "rev-parse", "HEAD") != first.sha
    assert second.git_dir == first.git_dir
    with pytest.raises(ArmError, match="does not resolve"):
        resolve_repo(RepoPin(git_url=f"file://{dl}", ref="nope"), role="devloop",
                     cache_root=cache)


def test_a_relative_path_is_taken_from_the_config_dir(repos, tmp_path):
    qg, dl = repos
    spec = CreateArm(qualgent_mcp=RepoPin(path=qg.name, ref="main"),
                     devloop=RepoPin(path=dl.name, ref="main"))
    arm = resolve_arm(spec, base_dir=qg.parent, cache_root=tmp_path)
    assert arm.qualgent_mcp.source == str(qg.resolve())


# ── the injected surface ──────────────────────────────────────────────────────

def test_tool_policy_template_narrows_to_the_templates_list(repos, tmp_path):
    qg, dl = repos
    arm = resolve_arm(_spec(qg, dl), cache_root=tmp_path)
    entry = arm.qualgent_server_entry("http://127.0.0.1:9", "/x/qualgent-mcp")
    assert entry == {"command": "/x/qualgent-mcp", "args": [],
                     "env": {"QUALGENT_API_URL": "http://127.0.0.1:9",
                             "QUALGENT_API_KEY": FAKE_API_KEY},
                     "enabled_tools": ["list_categories", "list_test_cases",
                                       "create_test_case"]}
    assert FAKE_API_KEY.startswith("qg_")          # QualGent-MCP refuses any other key
    assert arm.manifest()["tools_policy"] == "template"


def test_tool_policy_all_and_list(repos, tmp_path):
    qg, dl = repos
    arm = resolve_arm(_spec(qg, dl, qualgent_tools="all"), cache_root=tmp_path)
    assert "enabled_tools" not in arm.qualgent_server_entry("u", "c")
    assert arm.manifest()["qualgent_tools"] == "all"
    arm = resolve_arm(_spec(qg, dl, qualgent_tools=["create_test_case", "create_test_case"]),
                      cache_root=tmp_path)
    assert arm.qualgent_server_entry("u", "c")["enabled_tools"] == ["create_test_case"]
    assert arm.manifest()["tools_policy"] == "list"


def test_private_surface_goes_to_the_episode_dir_only(repos, tmp_path):
    qg, dl = repos
    arm = resolve_arm(_spec(qg, dl), cache_root=tmp_path)
    path = arm.write_private_surface(tmp_path / "ep")
    assert path == (tmp_path / "ep" / "private" / "developer_instructions.md").resolve()
    assert path.read_text() == "Stand-in creator instructions. Explore, draft, then create " \
                               "the case.\n"
    with pytest.raises(ArmError, match="inside the public repository"):
        arm.write_private_surface(REPO_ROOT / "runs" / "ep")


def test_creation_prompt_is_the_public_note_plus_the_brief():
    prompt = creation_prompt(app_name="MedTimer", bundle_id="com.example.med",
                             serial="emulator-5558", brief="  Feature: adding a thing.  \n")
    assert 'device="emulator-5558"' in prompt
    assert "already reserved" in prompt and "approval is granted" in prompt
    assert prompt.rstrip().endswith("Feature: adding a thing.")


# ── materialising QualGent-MCP ────────────────────────────────────────────────

def test_materialize_exports_the_committed_tree_only(repos, tmp_path):
    qg, dl = repos
    (qg / ".env").write_text("QUALGENT_API_URL=https://real.example\n")    # untracked
    (qg / "src/pkg/__init__.py").write_text("DIRTY = True\n")              # uncommitted
    arm = resolve_arm(_spec(qg, dl), cache_root=tmp_path)
    cache = tmp_path / "cache"
    exe = materialize_qualgent_mcp(arm.qualgent_mcp, cache_root=cache, install=False)
    dest = cache / "qualgent-mcp" / arm.qualgent_mcp.sha
    assert exe == dest / ".venv" / "bin" / "qualgent-mcp"
    assert (dest / "pyproject.toml").exists()
    assert not (dest / ".env").exists()
    assert (dest / "src/pkg/__init__.py").read_text() == ""
    assert (dest / ".qgb-ready").read_text().strip() == arm.qualgent_mcp.sha
    # Idempotent: a second call does not re-export.
    (dest / "marker").write_text("kept")
    materialize_qualgent_mcp(arm.qualgent_mcp, cache_root=cache, install=False)
    assert (dest / "marker").exists()


def test_the_cache_may_not_live_in_the_repo(repos, tmp_path):
    qg, dl = repos
    arm = resolve_arm(_spec(qg, dl), cache_root=tmp_path)
    with pytest.raises(ArmError, match="inside the public repository"):
        materialize_qualgent_mcp(arm.qualgent_mcp, cache_root=REPO_ROOT / ".cache",
                                 install=False)


# ── the smoke, against a stand-in QualGent-MCP ────────────────────────────────

STAND_IN_SERVER = textwrap.dedent('''\
    """Public stand-in for QualGent-MCP: the same routes, none of its text."""
    import os
    import httpx
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("stand-in")
    http = httpx.Client(base_url=os.environ["QUALGENT_API_URL"],
                        headers={"x-api-key": os.environ["QUALGENT_API_KEY"]})

    def j(r):
        r.raise_for_status()
        return r.json()

    @mcp.resource("qualgent://test-case-guide")
    def guide() -> str:
        return "stand-in guide"

    @mcp.tool()
    def list_categories() -> list:
        return j(http.get("/v1/categories/list")) or []

    @mcp.tool()
    def list_credentials() -> str:
        return str(j(http.get("/v1/credentials/list")))

    @mcp.tool()
    def list_apps() -> list:
        return j(http.get("/v1/apps/list")) or []

    @mcp.tool()
    def list_test_cases() -> list:
        return j(http.get("/v1/test-cases/list")) or []

    @mcp.tool()
    def check_credits(required_credits: int = 1) -> dict:
        return j(http.post("/v1/credits/validate", json={"required_credits": required_credits}))

    @mcp.tool()
    def create_test_case(name: str, steps: list, expected_result: str,
                         priority: str = "Medium") -> dict:
        return j(http.post("/v1/test-cases", json={
            "name": name, "steps": steps, "expected_result": expected_result,
            "priority": priority, "change_source": "mcp"}))

    @mcp.tool()
    def get_test_case(test_case_id: str) -> dict:
        return j(http.get(f"/v1/test-cases/{test_case_id}"))

    @mcp.tool()
    def update_test_case(test_case_id: str, expected_result: str | None = None) -> dict:
        return j(http.patch(f"/v1/test-cases/{test_case_id}",
                            json={"change_source": "mcp", "expected_result": expected_result}))

    @mcp.tool()
    def run_tests() -> dict:
        return j(http.post("/v1/test-cases/run", json={"jobs": []}))

    mcp.run(transport="stdio")
    ''')


@pytest.fixture
def stand_in_server(tmp_path):
    script = tmp_path / "stand_in_qualgent_mcp.py"
    script.write_text(STAND_IN_SERVER)
    (tmp_path / "bin").mkdir()        # not tmp_path/qualgent-mcp: macOS folds case
    launcher = tmp_path / "bin" / "qualgent-mcp"
    launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {script}\n")
    launcher.chmod(0o755)
    return launcher


async def test_probe_arm_creates_a_case_and_lands_the_artifact(repos, tmp_path,
                                                               stand_in_server):
    qg, dl = repos
    arm = resolve_arm(_spec(qg, dl), cache_root=tmp_path)
    ep = tmp_path / "runs" / "smoke"
    report = await probe_arm(arm, ep, command=stand_in_server)
    artifact = json.loads((ep / AUTHORED_CASE_FILE).read_text())
    assert report["created_id"] == artifact["test_case_id"]
    assert artifact["request"]["name"] == armmod.SMOKE_CASE["name"]
    assert artifact["request"]["change_source"] == "mcp"
    assert artifact["version_number"] == 2                   # create + the one update
    assert report["serialized_steps"].startswith("1. [setup] Open the app")
    assert report["unknown_routes"] == []
    assert [c["tool"] for c in report["calls"]] == [
        "list_categories", "list_credentials", "list_apps", "list_test_cases",
        "check_credits", "create_test_case", "get_test_case", "update_test_case"]
    manifest = json.loads((ep / "arm.json").read_text())
    assert manifest["qualgent_mcp"]["sha"] == arm.qualgent_mcp.sha
    assert len(manifest["guide_sha256"]) == 64


async def test_probe_arm_fails_when_an_offered_tool_is_missing(repos, tmp_path,
                                                               stand_in_server):
    qg, dl = repos
    arm = resolve_arm(_spec(qg, dl, qualgent_tools=["create_test_case", "upload_test_file"]),
                      cache_root=tmp_path)
    with pytest.raises(ArmError, match="does not serve upload_test_file"):
        await probe_arm(arm, tmp_path / "runs" / "smoke", command=stand_in_server)


async def test_probe_arm_refuses_an_episode_dir_in_the_repo(repos, tmp_path,
                                                            stand_in_server):
    qg, dl = repos
    arm = resolve_arm(_spec(qg, dl), cache_root=tmp_path)
    with pytest.raises(ArmError, match="inside the public repository"):
        await probe_arm(arm, REPO_ROOT / "runs" / "smoke", command=stand_in_server)


# ── CLI ────────────────────────────────────────────────────────────────────────

def test_cli_resolve_prints_the_manifest(repos):
    qg, dl = repos
    res = CliRunner().invoke(main, ["create-arm", "resolve", "--qualgent-mcp", f"{qg}@main",
                                    "--devloop", f"{dl}@v1", "--json"])
    assert res.exit_code == 0, res.output
    m = json.loads(res.output)
    assert m["devloop"]["sha"] == _git(dl, "rev-parse", "HEAD")
    assert m["qualgent_tools"] == ["list_categories", "list_test_cases", "create_test_case"]


def test_cli_resolve_fails_on_an_invalid_ref(repos):
    qg, dl = repos
    res = CliRunner().invoke(main, ["create-arm", "resolve", "--qualgent-mcp", f"{qg}@nope",
                                    "--devloop", f"{dl}@main"])
    assert res.exit_code == 1 and "does not resolve to a commit" in res.output


def test_cli_resolve_reads_the_config_block(repos, tmp_path):
    qg, dl = repos
    cfg = tmp_path / "bench.config.yaml"
    cfg.write_text(textwrap.dedent(f"""\
        agent: codex-cli
        model: gpt-6-astra
        scope: {{apps: [medtimer]}}
        create_arm:
          name: from-config
          qualgent_mcp: {{path: {qg.name}, ref: main}}
          devloop: {{path: {dl.name}, ref: main}}
          qualgent_tools: all
        """))
    res = CliRunner().invoke(main, ["create-arm", "resolve", "--config", str(cfg), "--json"])
    assert res.exit_code == 0, res.output
    m = json.loads(res.output)
    assert m["name"] == "from-config" and m["qualgent_tools"] == "all"


def test_cli_needs_both_pins(repos):
    qg, _ = repos
    res = CliRunner().invoke(main, ["create-arm", "resolve", "--qualgent-mcp", f"{qg}@main"])
    assert res.exit_code != 0 and "--devloop" in res.output


# ── opt-in: the real private surface ──────────────────────────────────────────

@pytest.mark.skipif(not (PRIVATE_QUALGENT_MCP and PRIVATE_DEVLOOP),
                    reason="set QGB_PRIVATE_QUALGENT_MCP and QGB_PRIVATE_DEVLOOP to local "
                           "checkouts for the live smoke (installs QualGent-MCP with uv)")
async def test_live_smoke_against_the_private_checkouts(tmp_path):
    spec = CreateArm(qualgent_mcp=RepoPin(path=PRIVATE_QUALGENT_MCP, ref="HEAD"),
                     devloop=RepoPin(path=PRIVATE_DEVLOOP, ref="HEAD"))
    arm = resolve_arm(spec)
    exe = materialize_qualgent_mcp(arm.qualgent_mcp)
    report = await probe_arm(arm, tmp_path / "smoke", command=exe)
    assert report["unknown_routes"] == [] and report["version_number"] == 2
    assert set(arm.qualgent_tools) <= set(report["offered_tools"])
