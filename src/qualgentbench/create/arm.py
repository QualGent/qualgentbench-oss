"""CreateBench v2: the creation ARM — which private authoring surface an episode gets,
resolved at run time and never committed (QUA-2852).

The surface a production author sees is private IP in two private repositories:

* **QualGent-MCP** — the server the author creates the case through: the
  `create_test_case` docstring, the `qualgent://test-case-guide` resource, the step
  validator. A creation episode runs this REAL server, from the arm's pinned ref,
  over stdio, with `QUALGENT_API_URL` pointed at the episode's fake API
  (`fake_api.py`), so every byte of it is production's.
* **DevLoop-MCP** — the `qualgent-test-creator` subagent template. It is delivered
  the way the desktop app installs it for Codex: the YAML frontmatter is dropped and
  the body becomes `developer_instructions`.

This repository is PUBLIC. So an arm is only a pair of pins
(`{qualgent_mcp: {path|git_url, ref}, devloop: {path|git_url, ref}}`, see
`config.CreateArm`) and everything behind them is read at run time:

1. `resolve_arm` resolves both refs to commit SHAs up front — an unknown ref, a
   missing template or a template without a QualGent tool list fails before any
   device time is spent.
2. `materialize_qualgent_mcp` exports the server's COMMITTED tree at that SHA (`git
   archive`, never a working tree and never the checkout's `.env`) into a cache dir
   outside the repo and installs it from its own lockfile.
3. `ResolvedArm.write_private_surface` writes the rendered template into the
   episode's private dir — under the runs dir, and refused inside this repo.
4. `ResolvedArm.write_manifest` records `arm.json`: resolved SHAs, the template's
   and the guide's sha256, the tool policy and the harness note's hash. Hashes and
   tool names only — never private text.

**Tool narrowing (spike P4).** The desktop's Codex rendering drops the template's
tool list, so a Codex author there sees every QualGent-MCP tool, `run_tests`,
`delete_apps`, `upload_app` and `save_bug` included. The default arm policy is
`template`: the server entry carries `enabled_tools` = the template's own QualGent
list, which is the surface the template was written for and what a host that honours
the list shows. `all` reproduces the desktop Codex surface exactly (anything off the
list 404s at the fake and is logged). An explicit list is an A/B arm.

**Harness note (spike P2/P3).** The template assumes a desktop session with device
lock tools and a user who answers questions. A benchmark episode has neither: the
device is reserved by the harness and `codex exec` is single-turn. `SURFACE_NOTE` is
our own public text that says so; `creation_prompt` puts it and the feature brief in
the user prompt. Its hash goes in the manifest so a change to it is an arm change.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import REPO_ROOT, CreateArm, RepoPin

logger = logging.getLogger(__name__)

#: QualGent-MCP refuses a key that does not start with `qg_` (and then silently
#: sends an empty one). The fake API accepts anything; this one is obviously fake.
FAKE_API_KEY = "qg_createbench_fake_key"
#: The MCP server name the author's QualGent tools are registered under.
QUALGENT_SERVER_NAME = "qualgent"
#: The resource the template points the author at; its hash is recorded per arm.
GUIDE_RESOURCE = "qualgent://test-case-guide"

ARM_MANIFEST_FILE = "arm.json"
PRIVATE_DIR = "private"
DEVELOPER_INSTRUCTIONS_FILE = "developer_instructions.md"
ARM_CACHE_ENV = "QGB_ARM_CACHE_DIR"
_READY_MARKER = ".qgb-ready"

# The desktop's template parser: a leading `---` line, YAML, a closing `---` line.
_FRONTMATTER_RE = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n([\s\S]*)$")
_SCP_URL_RE = re.compile(r"^[\w.-]+@[\w.-]+:")


class ArmError(Exception):
    """An arm that cannot run, phrased for the operator."""


# ── the harness's own (public) note ───────────────────────────────────────────

SURFACE_NOTE = """\
You are working as the qualgent-test-creator agent described in your developer instructions.

## Device and app
The Android app `{app_name}` (`{bundle_id}`) is already installed and running on Android device `{serial}`.
Device tools come from the `device` MCP server; every device tool takes the device as its first argument, so always pass device="{serial}".
There are no device-lock tools in this session: the device is already reserved for you, so skip any acquire, list or release step.
QualGent test-management tools (list_test_cases, list_categories, create_test_case, ...) come from the `qualgent` MCP server.

## No source code
There is no app source code in this session. Skip any step that reads the codebase and learn the app only from the running device.

## Approval
The user approved this request in advance: approval is granted. Nobody will answer questions during this session, so do not wait for a reply. When your draft is ready, submit it with create_test_case, then finish with a short summary that includes the created test case id.

## Feature brief
{brief}
"""


def creation_prompt(*, app_name: str, bundle_id: str, serial: str, brief: str) -> str:
    """The author's user prompt: the harness note plus the neutral feature brief. The
    private template reaches the agent only as developer instructions."""
    return SURFACE_NOTE.format(app_name=app_name, bundle_id=bundle_id, serial=serial,
                               brief=brief.strip())


def surface_note_sha256() -> str:
    return hashlib.sha256(SURFACE_NOTE.encode()).hexdigest()


# ── paths ──────────────────────────────────────────────────────────────────────

def default_cache_root() -> Path:
    override = os.environ.get(ARM_CACHE_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "qualgentbench" / "create-arms"


def assert_outside_repo(path: str | Path, what: str = "private creation-surface text") -> Path:
    """Refuse any destination inside this (public) repository's tree."""
    p = Path(path).expanduser().resolve()
    if p == REPO_ROOT or REPO_ROOT in p.parents:
        raise ArmError(f"refusing to write {what} to {p}: it is inside the public "
                       f"repository {REPO_ROOT}. Use a runs/cache dir outside it.")
    return p


# ── git ────────────────────────────────────────────────────────────────────────

def _git(git_dir: Path, *args: str, check: bool = True,
         input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(["git", "-C", str(git_dir), *args], capture_output=True,
                              check=check, input=input_bytes)
    except FileNotFoundError as exc:
        raise ArmError("git is not installed") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace").strip()
        raise ArmError(f"git {' '.join(args)} failed in {git_dir}: {detail}") from exc


def parse_pin(spec: str) -> RepoPin:
    """`SRC@REF` → a pin. SRC is a local checkout path or a clone URL
    (`https://…`, `ssh://…`, `git@host:org/repo.git`); REF is required."""
    src, sep, ref = spec.rpartition("@")
    if not sep or not src or not ref or ":" in ref:     # a git ref never holds ':'
        raise ArmError(f"{spec!r}: expected SRC@REF (a checkout path or git URL, then "
                       "'@' and a branch, tag or commit)")
    if "://" in src or _SCP_URL_RE.match(src):
        return RepoPin(git_url=src, ref=ref)
    return RepoPin(path=src, ref=ref)


@dataclass(frozen=True)
class ResolvedRepo:
    """A pin resolved to one commit, readable through `git_dir`."""
    role: str
    source: str
    ref: str
    sha: str
    git_dir: Path

    def read(self, relpath: str) -> bytes:
        """The committed bytes of `relpath` at `sha` (never the working tree)."""
        out = _git(self.git_dir, "show", f"{self.sha}:{relpath}", check=False)
        if out.returncode != 0:
            raise ArmError(f"{self.role}: {relpath} does not exist at {self.ref} "
                           f"({self.sha[:12]}) in {self.source}")
        return out.stdout

    def manifest(self) -> dict[str, str]:
        return {"source": self.source, "ref": self.ref, "sha": self.sha}


def _clone_cache_dir(url: str, cache_root: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", url.rstrip("/").rsplit("/", 1)[-1])
    digest = hashlib.sha1(url.encode()).hexdigest()[:12]
    return cache_root / "repos" / f"{slug}-{digest}"


def resolve_repo(pin: RepoPin, *, role: str, base_dir: Path | None = None,
                 cache_root: Path | None = None) -> ResolvedRepo:
    """Resolve `pin.ref` to a full commit SHA, or raise `ArmError` now.

    A `path` pin is read in place (read-only: `rev-parse`, `show`, `archive`). A
    `git_url` pin is fetched into a bare mirror under the cache root."""
    cache_root = cache_root or default_cache_root()
    if pin.path:
        git_dir = Path(pin.path).expanduser()
        if not git_dir.is_absolute() and base_dir is not None:
            git_dir = base_dir / git_dir
        git_dir = git_dir.resolve()
        if not git_dir.is_dir():
            raise ArmError(f"{role}: {git_dir} is not a directory")
        source = str(git_dir)
        if _git(git_dir, "rev-parse", "--git-dir", check=False).returncode != 0:
            raise ArmError(f"{role}: {git_dir} is not a git checkout")
    else:
        assert pin.git_url
        source = pin.git_url
        git_dir = _clone_cache_dir(pin.git_url, assert_outside_repo(cache_root, "the arm cache"))
        if not (git_dir / "HEAD").exists():
            git_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.run(["git", "clone", "--bare", "--quiet", pin.git_url, str(git_dir)],
                               capture_output=True, check=True)
            except subprocess.CalledProcessError as exc:
                raise ArmError(f"{role}: cannot clone {pin.git_url}: "
                               f"{exc.stderr.decode(errors='replace').strip()}") from exc
        else:
            _git(git_dir, "fetch", "--quiet", "--prune", pin.git_url,
                 "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
    out = _git(git_dir, "rev-parse", "--verify", "--quiet", f"{pin.ref}^{{commit}}",
               check=False)
    # A commit no branch head points at: ask the remote for it directly.
    if out.returncode != 0 and pin.git_url and _git(
            git_dir, "fetch", "--quiet", pin.git_url, pin.ref, check=False).returncode == 0:
        out = _git(git_dir, "rev-parse", "--verify", "--quiet",
                       f"{pin.ref}^{{commit}}", check=False)
    if out.returncode != 0:
        raise ArmError(f"{role}: ref {pin.ref!r} does not resolve to a commit in {source}")
    return ResolvedRepo(role=role, source=source, ref=pin.ref,
                        sha=out.stdout.decode().strip(), git_dir=git_dir)


# ── the creator template ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class CreatorTemplate:
    """The qualgent-test-creator template at the arm's DevLoop ref. `body` is private
    text: it only ever goes to the agent and to the episode's private dir."""
    path: str
    body: str = field(repr=False)
    sha256: str
    qualgent_tools: tuple[str, ...]

    @property
    def developer_instructions(self) -> str:
        """What the desktop's Codex rendering installs: the body, trailing space cut."""
        return self.body.rstrip()


def parse_template(raw: str, *, path: str) -> CreatorTemplate:
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ArmError(f"{path}: no YAML frontmatter — not a subagent template")
    try:
        front = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ArmError(f"{path}: frontmatter is not valid YAML: {exc}") from exc
    tools = (front.get("tools") or {}) if isinstance(front, dict) else {}
    qualgent = tools.get(QUALGENT_SERVER_NAME) if isinstance(tools, dict) else None
    if not isinstance(qualgent, list) or not all(isinstance(t, str) for t in qualgent):
        raise ArmError(f"{path}: frontmatter has no `tools.{QUALGENT_SERVER_NAME}` list")
    body = m.group(2)
    if not body.strip():
        raise ArmError(f"{path}: the template body is empty")
    return CreatorTemplate(path=path, body=body,
                           sha256=hashlib.sha256(raw.encode()).hexdigest(),
                           qualgent_tools=tuple(qualgent))


# ── the resolved arm ───────────────────────────────────────────────────────────

@dataclass
class ResolvedArm:
    name: str
    qualgent_mcp: ResolvedRepo
    devloop: ResolvedRepo
    template: CreatorTemplate
    tools_policy: str                         # "template" | "all" | "list"
    qualgent_tools: tuple[str, ...] | None    # None = every tool the server has
    guide_sha256: str | None = None           # filled by `probe_arm`

    def qualgent_server_entry(self, api_url: str, command: str | Path,
                              args: list[str] | None = None) -> dict[str, Any]:
        """The `qualgent` stdio entry for an episode's mcp_config.json (the codex
        adapter renders `command`/`args`/`env`/`enabled_tools` per server). `run
        --mode create` passes the harness's stdio relay as `command` and the server
        after it in `args` (`create.runner.CreationEpisode.mcp_servers`), so every
        QualGent-MCP call is metered on the creation ledger."""
        entry: dict[str, Any] = {
            "command": str(command), "args": list(args or []),
            "env": qualgent_server_env(api_url),
        }
        if self.qualgent_tools is not None:
            entry["enabled_tools"] = list(self.qualgent_tools)
        return entry

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "qualgent_mcp": self.qualgent_mcp.manifest(),
            "devloop": {**self.devloop.manifest(), "template": self.template.path,
                        "template_sha256": self.template.sha256},
            "qualgent_tools": (list(self.qualgent_tools)
                               if self.qualgent_tools is not None else "all"),
            "tools_policy": self.tools_policy,
            "guide_sha256": self.guide_sha256,
            "surface_note_sha256": surface_note_sha256(),
        }

    def write_manifest(self, episode_dir: str | Path) -> Path:
        path = Path(episode_dir) / ARM_MANIFEST_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.manifest(), indent=2) + "\n")
        return path

    def write_private_surface(self, episode_dir: str | Path) -> Path:
        """Write the rendered template into the episode's private dir and return its
        path. Refused anywhere inside this repository."""
        target = assert_outside_repo(Path(episode_dir) / PRIVATE_DIR)
        target.mkdir(parents=True, exist_ok=True)
        path = target / DEVELOPER_INSTRUCTIONS_FILE
        path.write_text(self.template.developer_instructions + "\n")
        return path


def qualgent_server_env(api_url: str) -> dict[str, str]:
    return {"QUALGENT_API_URL": api_url, "QUALGENT_API_KEY": FAKE_API_KEY}


def resolve_arm(spec: CreateArm, *, base_dir: Path | None = None,
                cache_root: Path | None = None) -> ResolvedArm:
    """Resolve both pins and read the template. Everything that can make the arm
    unrunnable, short of installing the server, fails here."""
    qg = resolve_repo(spec.qualgent_mcp, role="qualgent_mcp", base_dir=base_dir,
                      cache_root=cache_root)
    dl = resolve_repo(spec.devloop, role="devloop", base_dir=base_dir,
                      cache_root=cache_root)
    template = parse_template(dl.read(spec.template).decode("utf-8"), path=spec.template)
    if spec.qualgent_tools == "template":
        policy, tools = "template", template.qualgent_tools
    elif spec.qualgent_tools == "all":
        policy, tools = "all", None
    else:
        if not spec.qualgent_tools:
            raise ArmError("qualgent_tools: an explicit list must name at least one tool")
        policy, tools = "list", tuple(dict.fromkeys(spec.qualgent_tools))
    return ResolvedArm(name=spec.name, qualgent_mcp=qg, devloop=dl, template=template,
                       tools_policy=policy, qualgent_tools=tools)


# ── running QualGent-MCP from the pinned ref ──────────────────────────────────

def materialize_qualgent_mcp(repo: ResolvedRepo, *, cache_root: Path | None = None,
                             install: bool = True) -> Path:
    """Export QualGent-MCP's committed tree at `repo.sha` into the cache and install
    it from its own lockfile; return the `qualgent-mcp` executable. Idempotent per
    SHA. The export is `git archive`, so the private checkout is only read and its
    untracked files (a developer `.env` pointing at a real API) never come along."""
    cache_root = assert_outside_repo(cache_root or default_cache_root(), "the arm cache")
    dest = cache_root / "qualgent-mcp" / repo.sha
    exe = dest / ".venv" / "bin" / "qualgent-mcp"
    if (dest / _READY_MARKER).exists() and (exe.exists() or not install):
        return exe
    if dest.exists():
        shutil.rmtree(dest)                   # a half-built export of our own cache
    dest.mkdir(parents=True)
    archive = _git(repo.git_dir, "archive", "--format=tar", repo.sha).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    if install:
        uv = shutil.which("uv")
        if not uv:
            raise ArmError("uv is not installed; it installs QualGent-MCP from its lockfile")
        env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
        proc = subprocess.run([uv, "sync", "--frozen", "--no-dev", "--project", str(dest)],
                              capture_output=True, env=env, check=False)
        if proc.returncode != 0 or not exe.exists():
            raise ArmError(f"installing QualGent-MCP {repo.sha[:12]} failed: "
                           f"{proc.stderr.decode(errors='replace').strip()[-2000:]}")
    (dest / _READY_MARKER).write_text(repo.sha + "\n")
    return exe


#: A public, neutral case the smoke posts — our own words, no brief, no template.
SMOKE_CASE: dict[str, Any] = {
    "name": "Smoke case for the fake QualGent API",
    "steps": [
        {"description": "Open the app", "kind": "setup"},
        {"description": "Tap the \"Add\" button", "kind": "act"},
        {"description": "Verify the \"Saved\" message is shown", "kind": "verify"},
    ],
    "expected_result": "The saved message is shown.",
    "priority": "Low",
}


#: Read-only calls the smoke makes BEFORE creating, to prove every route the
#: creator's QualGent tools reach is one the fake serves (a route QualGent-MCP moved
#: would 404 and fail the smoke). `upload_test_file` needs a real file and is not
#: called; it would 404 at the fake, loudly.
SMOKE_READS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("list_categories", {}), ("list_credentials", {}), ("list_apps", {}),
    ("list_test_cases", {}), ("check_credits", {}),
)
#: The update the smoke makes AFTER creating: it must become version 2 of the case.
SMOKE_UPDATE = {"expected_result": "The saved message is shown on the same screen."}
SMOKE_STDERR = "qualgent-mcp.stderr.log"


def _unwrap(group: BaseExceptionGroup) -> BaseException:
    """The one ArmError (else the first leaf) inside a task group's exception group."""
    leaves: list[BaseException] = []

    def walk(exc: BaseException) -> None:
        if isinstance(exc, BaseExceptionGroup):
            for inner in exc.exceptions:
                walk(inner)
        else:
            leaves.append(exc)

    walk(group)
    return next((e for e in leaves if isinstance(e, ArmError)), leaves[0] if leaves else group)


def _tool_text(result: Any) -> str:
    return "".join(getattr(c, "text", "") or "" for c in result.content)


async def probe_arm(arm: ResolvedArm, episode_dir: str | Path, *, command: str | Path,
                    app_name: str = "SmokeApp") -> dict[str, Any]:
    """Offline smoke of an arm, no device: start the fake API, start QualGent-MCP at
    the pinned ref over stdio against it, check the offered tools exist and the guide
    resource answers, call every read the creator's tools make, create one case, read
    it back, update it once, and check that `authored_case.json` landed in
    `episode_dir` with the exact create body, the update and the serialized steps.
    Any route the fake does not serve fails the smoke. Records `arm.json`. Raises
    `ArmError`."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from .fake_api import API_DIR, AUTHORED_CASE_FILE, FakeApp, FakeQualGentAPI

    episode_dir = assert_outside_repo(episode_dir, "a creation episode")
    (episode_dir / API_DIR).mkdir(parents=True, exist_ok=True)
    calls: list[dict[str, Any]] = []

    async def call(session: Any, tool: str, args: dict[str, Any]) -> str:
        result = await session.call_tool(tool, args)
        text = _tool_text(result)
        calls.append({"tool": tool, "is_error": bool(result.isError)})
        if result.isError:
            raise ArmError(f"{tool} failed against the fake API: {text[:500]}")
        return text

    try:
        with FakeQualGentAPI(episode_dir, app=FakeApp(app_name)) as api, \
                (episode_dir / API_DIR / SMOKE_STDERR).open("w") as errlog:
            params = StdioServerParameters(command=str(command), args=[],
                                           env=qualgent_server_env(api.url),
                                           cwd=str(episode_dir))
            async with stdio_client(params, errlog=errlog) as (read, write), \
                    ClientSession(read, write) as session:
                init = await session.initialize()
                names = sorted(t.name for t in (await session.list_tools()).tools)
                missing = [t for t in (arm.qualgent_tools or ()) if t not in names]
                if missing:
                    raise ArmError(
                        f"QualGent-MCP {arm.qualgent_mcp.sha[:12]} does not serve "
                        f"{', '.join(missing)}, which the arm's tool list names")
                resources = {str(r.uri) for r in (await session.list_resources()).resources}
                if GUIDE_RESOURCE not in resources:
                    raise ArmError(f"QualGent-MCP {arm.qualgent_mcp.sha[:12]} has no "
                                   f"{GUIDE_RESOURCE} resource")
                guide = await session.read_resource(GUIDE_RESOURCE)
                guide_text = "".join(getattr(c, "text", "") or "" for c in guide.contents)
                if not guide_text.strip():
                    raise ArmError(f"{GUIDE_RESOURCE} is empty")
                arm.guide_sha256 = hashlib.sha256(guide_text.encode()).hexdigest()
                for tool, args in SMOKE_READS:
                    if tool in names:
                        await call(session, tool, args)
                text = await call(session, "create_test_case", dict(SMOKE_CASE))
                created = json.loads(text) if text.strip().startswith("{") else {}
                case_id = created.get("id")
                if not case_id:
                    raise ArmError(f"create_test_case answered without an id: {text[:300]}")
                await call(session, "get_test_case", {"test_case_id": case_id})
                if "update_test_case" in names:
                    await call(session, "update_test_case",
                               {"test_case_id": case_id, **SMOKE_UPDATE})
    except BaseExceptionGroup as group:      # anyio wraps what the session raised
        raise _unwrap(group) from None
    summary = json.loads((episode_dir / API_DIR / "summary.json").read_text())
    if summary["unknown_routes"]:
        raise ArmError("QualGent-MCP reached routes the fake API does not serve: "
                       + ", ".join(summary["unknown_routes"]))
    artifact_path = episode_dir / AUTHORED_CASE_FILE
    if not artifact_path.exists():
        raise ArmError(f"create_test_case returned but no {AUTHORED_CASE_FILE} landed")
    artifact = json.loads(artifact_path.read_text())
    if artifact.get("test_case_id") != case_id:
        raise ArmError("the id create_test_case returned is not the captured case's id")
    posted = {k: v for k, v in artifact.get("request", {}).items() if k != "change_source"}
    if posted.get("steps") != SMOKE_CASE["steps"] or posted.get("name") != SMOKE_CASE["name"]:
        raise ArmError(f"{AUTHORED_CASE_FILE} does not carry the body that was posted")
    if not artifact.get("serialized_steps"):
        raise ArmError(f"{AUTHORED_CASE_FILE} has no serialized_steps")
    arm.write_manifest(episode_dir)
    return {
        "episode_dir": str(episode_dir),
        "server": {"name": init.serverInfo.name, "version": init.serverInfo.version,
                   "tools": len(names)},
        "offered_tools": list(arm.qualgent_tools) if arm.qualgent_tools else names,
        "calls": calls,
        "created_id": case_id,
        "version_number": artifact.get("version_number"),
        "authored_case": str(artifact_path),
        "serialized_steps": artifact["serialized_steps"],
        "requests": summary["requests"],
        "unknown_routes": summary["unknown_routes"],
        "arm": arm.manifest(),
    }
