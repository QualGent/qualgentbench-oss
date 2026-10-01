"""CreateBench v2 stage A: the creation episode (`run --mode create`, QUA-2856).

One episode = one case's neutral BRIEF (`brief:` in `data/test-cases/<app>.yaml`,
QUA-2853) + one trial. The app is staged exactly as that case's CLEAN journey episode
(journey build, no defect on, the case's fixture, the pinned clock), the author
explores it through the metered device server, and it submits a test case through the
REAL QualGent-MCP server at the arm's pinned ref (`create/arm.py`), backed by this
episode's own fake QualGent API (`create/fake_api.py`). Nothing is graded here: the
artifact is `<episode>/authored_case.json`, which QUA-2857 grades.

What an episode is given (see `create/brief.py`): the creator template as developer
instructions (PRIVATE, read at run time, written only under the runs dir) and, as the
prompt, the harness note + the brief + one request sentence.

Two MCP servers per episode:

* `device` — the operator's standalone DevLoop-MCP behind the MCP meter, exactly as in
  journey mode (step budget, isolation record, server stamp, contamination roots).
* `qualgent` — QualGent-MCP over stdio, spawned by the agent's MCP client through the
  harness's stdio relay (`mcp_meter.run_stdio_relay`), which meters every call into
  `creation_calls.json` (`interactions.CreationLedger`, classified by
  `interactions.QUALGENT_TOOL_RULES`). Those calls are NOT device interactions: they
  never reach `interactions.json` and the budget hook never charges them. Isolation is
  by construction — a fresh server process and a fresh, EMPTY fake per episode — and
  recorded (`provenance.create.qualgent`). The only writes the fake accepts are the
  authored case's create and update; every other write 404s and is counted.

The verdict (`create_verdict`) turns the capture into validity flags, and `passed`
consults them (the QUA-2609 lesson: an episode must never read `passed: true` while the
board calls it unquotable):

* `env_failure`  — staging failed, the agent crashed without a case, or QualGent-MCP
  never started (the relay saw no `initialize`). Excluded like every env failure.
* `no_case`      — no case was created. Its reason is an OUTCOME, never a crash:
  `truncated` (the budget ran out first), `create_refused` (every create was rejected
  by the validator), `asked_instead` (single-turn `codex exec`: the author asked a
  question instead of submitting, spike P3) or `never_submitted`.
* `dead`         — a case exists, but it is not grounded in what the author OBSERVED:
  no screen read ever came back with content, or the case quotes UI strings and not
  one of them was in any device result (the QUA-2609 lesson: grounding on the observed
  surface, not raw call counts). Quotes are read off the author's steps and expected
  result (`create.lint.quoted`) and matched folded, like journey report grounding.
* `off_app`      — the device ended in another app than the one under test.
* `truncated`    — recorded; it invalidates only through `no_case` (the hook denies
  every call past the cap, `create_test_case` included, so a case that exists was
  created within budget).
* `contaminated` — the shared scan, plus `qualgent_api_bypass` (the API reached around
  QualGent-MCP).

`valid_case` = a case exists and none of env_failure / dead / off_app / contaminated
holds; `passed` is exactly `valid_case`. `run --mode create` exits non-zero when no
episode produced one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import corpus, journey, pricing
from ..interactions import QG_OFF_SURFACE, QG_UNKNOWN, read_creation_ledger
from ..mcp_meter import STDIO_LEDGER_FILE
from ..result import RunResult, VerifierResult
from ..task import BenchmarkTask
from ..transcript import TranscriptParser
from .arm import QUALGENT_SERVER_NAME, ResolvedArm, creation_prompt
from .brief import CREATE_BRIEF_VERSION, case_brief, render_brief, text_sha256
from .fake_api import API_DIR, AUTHORED_CASE_FILE, REQUEST_LOG, FakeApp, FakeQualGentAPI

logger = logging.getLogger(__name__)

MODE = "create"
TASK_TYPE = "create_case"
VERSION = "create"
#: One fixed creation-mode step budget (device interactions only). Never a corpus
#: case's journey budget: that budget sized a RUNNER on a known route, and an author
#: has to discover the route first. The spike's author spent 16 of 150.
CREATE_STEP_BUDGET = 150

# Validity flags, in the order a failure reason names them.
ENV_FAILURE = "env_failure"
NO_CASE = "no_case"
DEAD = "dead"
OFF_APP = "off_app"
TRUNCATED = "truncated"
CONTAMINATED = "contaminated"
#: Flags that make a created case unusable (`valid_case` false).
INVALIDATING = (ENV_FAILURE, NO_CASE, DEAD, OFF_APP, CONTAMINATED)

# Outcomes (one per episode).
CASE_CREATED = "case_created"
NO_CASE_CREATED = "no_case_created"

# Why no case exists.
NC_TRUNCATED = "truncated"
NC_REFUSED = "create_refused"
NC_ASKED = "asked_instead"
NC_NEVER = "never_submitted"

# The author's last message reads as a question or a request for approval: single-turn
# `codex exec` ends there, with no case (spike P3).
_ASKED_RE = re.compile(
    r"\?\s*$|\b(?:would you like|shall i|should i|do you want|let me know|please "
    r"(?:confirm|approve|review)|awaiting (?:your )?(?:approval|confirmation)|"
    r"once you (?:approve|confirm)|if you approve|your approval)\b",
    re.IGNORECASE)


# ── tasks ──────────────────────────────────────────────────────────────────────

def task_id(case_id: str) -> str:
    return f"{case_id}~{VERSION}"


def case_of(task_id_: str) -> str:
    return task_id_.rsplit("~", 1)[0] if task_id_.endswith(f"~{VERSION}") else task_id_


def create_tasks(suite: dict[str, Any]) -> list[BenchmarkTask]:
    """One creation task per journey case that carries a usable `brief:` block. The
    spec holds what STAGING needs (the case's clean journey staging: fixture, shared
    storage, no defect on) and the brief; nothing from the case's route, steps,
    oracle or bugs is copied, so nothing the author must not see can leak through it."""
    app = suite["app"]
    app_id = str(app.get("id", ""))
    doc = journey.load_cases(app_id) or {}
    tasks: list[BenchmarkTask] = []
    for case in doc.get("test_cases", []):
        brief = case_brief(case)
        if not case.get("id") or brief is None:
            continue
        cid = str(case["id"])
        spec = {
            "mode": MODE,
            "app_id": app_id,
            "case_id": cid,
            "version": VERSION,
            "brief": brief,
            "step_budget": CREATE_STEP_BUDGET,
            "active_bugs": [],
            "device_setup": suite.get("device_setup"),
            "shared_storage": suite.get("shared_storage"),
            "heldout": corpus.is_heldout(app_id),
        }
        tasks.append(BenchmarkTask(
            id=task_id(cid),
            name=brief["title"],
            instruction="",                    # composed per episode (CreationEpisode)
            app_file_id="",
            app_name=str(app.get("name") or app_id),
            platform=str(app.get("platform") or "android"),
            bundle_id=str(app.get("package") or ""),
            bug_spec=spec,
        ))
    return tasks


def known_brief_ids(apps: Iterable[dict[str, Any]]) -> dict[str, str]:
    """{case id: app id} of every case with a creation brief — what `--case` selects
    from in create mode."""
    out: dict[str, str] = {}
    for suite in apps:
        for t in create_tasks(suite):
            out.setdefault(str(t.bug_spec["case_id"]), str(t.bug_spec["app_id"]))
    return out


# ── the per-run surface and the per-episode wiring ────────────────────────────

class CreationSurface:
    """One run's creation surface: the resolved arm and the installed QualGent-MCP
    executable. `open` wires one episode (see `EpisodeOptions.creation`)."""

    def __init__(self, arm: ResolvedArm, qualgent_command: str | Path, *,
                 python: str | None = None, spec: dict[str, Any] | None = None) -> None:
        self.arm = arm
        self.qualgent_command = str(qualgent_command)
        self.python = python or sys.executable
        # The arm as plan.json keeps it for a resume: both pins at the SHAs they
        # resolved to (a branch that moves between sittings cannot change the arm).
        self.spec = spec if spec is not None else pinned_spec(arm)

    def stamp(self) -> dict[str, Any]:
        """What plan.json's environment carries (and a resume compares): the arm
        manifest, the creation brief version and the hash of its public text."""
        return {"arm": self.arm.manifest(), "create_brief_version": CREATE_BRIEF_VERSION,
                "create_text_sha256": text_sha256()}

    def open(self, run_dir: Path, task: BenchmarkTask, device_serial: str) -> CreationEpisode:
        return CreationEpisode(self, Path(run_dir), task, device_serial)


class CreationEpisode:
    """The creation half of one episode: the fake API (started here), the `qualgent`
    server entry, the private surface and the prompt. `close` is idempotent."""

    def __init__(self, surface: CreationSurface, run_dir: Path, task: BenchmarkTask,
                 device_serial: str) -> None:
        self.surface = surface
        self.arm = surface.arm
        self.run_dir = run_dir
        self.task = task
        self.device_serial = device_serial
        self.ledger_path = run_dir / STDIO_LEDGER_FILE
        self.summary: dict[str, Any] | None = None
        # Private text goes ONLY under the episode dir (refused inside the repo).
        self.arm.write_private_surface(run_dir)
        self.arm.write_manifest(run_dir)
        self.fake = FakeQualGentAPI(run_dir, app=FakeApp(task.app_name)).start()
        self.api_url = self.fake.url

    @property
    def developer_instructions(self) -> str:
        return self.arm.template.developer_instructions

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        """`qualgent`: QualGent-MCP at the arm's SHA, spawned through the stdio relay
        that meters its calls, pointed at this episode's fake API."""
        entry = self.arm.qualgent_server_entry(self.api_url, self.surface.python, args=[
            "-m", "qualgentbench.mcp_meter", "stdio", "--ledger", str(self.ledger_path),
            "--", self.surface.qualgent_command])
        # QGB_DISALLOWED_TOOLS withholds DEVICE tools; the author's QualGent surface is
        # the arm's tool policy alone (spike P5).
        entry["apply_disallowed_tools"] = False
        return {QUALGENT_SERVER_NAME: entry}

    def instruction(self) -> str:
        spec = self.task.bug_spec or {}
        return creation_prompt(app_name=self.task.app_name, bundle_id=self.task.bundle_id,
                               serial=self.device_serial,
                               brief=render_brief(spec.get("brief") or {}))

    def close(self) -> dict[str, Any]:
        if self.summary is None:
            try:
                self.summary = self.fake.stop()
            except Exception as exc:  # noqa: BLE001 - never fail an episode on teardown
                logger.warning("fake QualGent API did not stop cleanly: %s", exc)
                self.summary = self.fake.summary()
        return self.summary

    def record(self, spec: dict[str, Any]) -> None:
        """Everything the verdict reads, into the task spec (after `close`)."""
        summary = self.close()
        spec["creation"] = {
            "authored_case": _read_json(self.run_dir / AUTHORED_CASE_FILE),
            "api_summary": summary,
            "requests": read_requests(self.run_dir),
            "ledger": read_creation_ledger(self.ledger_path),
            "qualgent_api": self.api_url,
        }

    def provenance(self) -> dict[str, Any]:
        ledger = read_creation_ledger(self.ledger_path) or {}
        summary = self.summary or self.fake.summary()
        return {
            **self.surface.stamp(),
            "qualgent": {
                # A fresh server process per episode (the agent's client spawns it) and
                # a fresh, EMPTY fake API per episode: nothing another episode wrote is
                # reachable through the second server.
                "isolation": "per_episode",
                "transport": "stdio",
                "fake_api_empty_at_start": True,
                "sessions": ledger.get("sessions"),
                "requests": summary.get("requests"),
                "unknown_routes": summary.get("unknown_routes") or [],
            },
        }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_requests(run_dir: Path) -> list[dict[str, Any]]:
    """The fake API's request log, bodies dropped (the captured case is its own file)."""
    out: list[dict[str, Any]] = []
    try:
        lines = (Path(run_dir) / API_DIR / REQUEST_LOG).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        out.append({k: e.get(k) for k in ("seq", "method", "path", "status", "route",
                                          "unknown")})
    return out


def pinned_spec(arm: ResolvedArm) -> dict[str, Any]:
    """The `config.CreateArm` of `arm` with every ref replaced by its resolved SHA and
    every path made absolute — what a resume re-resolves."""
    def pin(repo) -> dict[str, str]:
        key = "git_url" if ("://" in repo.source or "@" in repo.source.split("/")[0]) else "path"
        return {key: repo.source, "ref": repo.sha}
    return {"name": arm.name, "qualgent_mcp": pin(arm.qualgent_mcp),
            "devloop": pin(arm.devloop), "template": arm.template.path,
            "qualgent_tools": (list(arm.qualgent_tools) if arm.tools_policy == "list"
                               else arm.tools_policy)}


async def prepare_surface(spec: Any, *, base_dir: Path | None, runs_dir: Path,
                          console: Any = None) -> CreationSurface:
    """Resolve the arm, install QualGent-MCP at its SHA and smoke it against a fake API
    (no device) — every way the arm can be unrunnable fails here, before a device or a
    model is paid for. The smoke episode lands in `<runs>/_create_smoke/`."""
    from .arm import materialize_qualgent_mcp, probe_arm, resolve_arm

    arm = resolve_arm(spec, base_dir=base_dir)
    exe = await asyncio.to_thread(materialize_qualgent_mcp, arm.qualgent_mcp)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    smoke_dir = Path(runs_dir).expanduser() / "_create_smoke" / f"{stamp}_{arm.name}"
    report = await probe_arm(arm, smoke_dir, command=exe)
    if console is not None:
        console.print(f"[dim]create arm {arm.name}: QualGent-MCP "
                      f"{arm.qualgent_mcp.sha[:12]} · DevLoop {arm.devloop.sha[:12]} · "
                      f"template {arm.template.sha256[:12]} · guide "
                      f"{(arm.guide_sha256 or '?')[:12]} · tools {arm.tools_policy} "
                      f"({len(report['offered_tools'])}) · smoke ok[/]")
    return CreationSurface(arm, exe)


# ── the verdict ────────────────────────────────────────────────────────────────

def _authored_quotes(case: dict[str, Any]) -> list[str]:
    from .lint import quoted

    texts = [str(s.get("description", "")) if isinstance(s, dict) else str(s)
             for s in (case.get("steps") or [])]
    texts.append(str(case.get("expected_result") or ""))
    out: list[str] = []
    for text in texts:
        for q in quoted(text):
            if q not in out:
                out.append(q)
    return out


def grounding(case: dict[str, Any] | None, transcript: str) -> dict[str, Any]:
    """How much of the authored case is grounded in what the device SHOWED the author:
    its quoted UI strings against every device result (folded, refused replies and
    argument echoes dropped — journey report grounding's own reader), and whether any
    screen read came back with content at all."""
    screen_reads = [t for t in journey._observation_texts(transcript, "mcp", screen_only=True,
                                                          fold=False)
                    if journey._witness_capable(t, "mcp")]
    results = journey._device_texts(transcript, "mcp", results_only=True)
    quotes = _authored_quotes(case or {})
    grounded, ungrounded = [], []
    for q in quotes:
        needle = journey._evidence(q)
        if needle and any(needle in t for t in results):
            grounded.append(q)
        elif needle:
            ungrounded.append(q)
    return {"observed_screen": bool(screen_reads), "screen_reads": len(screen_reads),
            "quotes": len(grounded) + len(ungrounded), "grounded": len(grounded),
            "ungrounded": ungrounded[:10]}


def _last_agent_text(transcript: str) -> str:
    """The author's final message (codex `agent_message`, claude `text`)."""
    last = ""
    for line in transcript.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") if isinstance(ev, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            last = str(item.get("text") or "")
        elif isinstance(ev, dict) and ev.get("type") == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    last = str(b["text"])
    return last.strip()


def create_verdict(transcript: str, model: str, task: BenchmarkTask) -> VerifierResult:
    from ..bugs import _count_tool_calls
    from ..contamination import scan as contamination_scan

    spec = task.bug_spec or {}
    creation = spec.get("creation") or {}
    parser = TranscriptParser(transcript)
    contamination = contamination_scan(parser, spec.get("workspace"),
                                       devloop_roots=spec.get("devloop_roots"),
                                       nonce=spec.get("flag_nonce"),
                                       adbd_at_end=spec.get("adbd_at_end"),
                                       qualgent_api=creation.get("qualgent_api") or "")

    artifact = creation.get("authored_case")
    case = (artifact or {}).get("case") if isinstance(artifact, dict) else None
    requests = creation.get("requests") or []
    ledger = creation.get("ledger")
    creates = [r for r in requests if r.get("route") == "create_test_case"]
    updates = [r for r in requests if r.get("route") == "update_test_case"]
    unknown = [f"{r.get('method')} {r.get('path')}" for r in requests if r.get("unknown")]

    # The author's QualGent calls as the transcript shows them (the ledger is the
    # harness's own count; both are recorded, the ledger is authoritative).
    qg_events = [e for e in parser.events() if e.server == QUALGENT_SERVER_NAME]
    device_events = [e for e in parser.events() if e.is_device_evidence]
    device_ok = [e for e in device_events if e.success]
    off_surface_calls = (int((ledger or {}).get(f"calls_{QG_OFF_SURFACE}") or 0)
                         + int((ledger or {}).get(f"calls_{QG_UNKNOWN}") or 0))
    ground = grounding(case, transcript)

    truncated = bool(spec.get("truncated"))
    exit_code = int(spec.get("exit_code") or 0)
    agent_ran = not spec.get("staging_failed")
    last_text = _last_agent_text(transcript)

    flags: list[str] = []
    reasons: list[str] = []
    if spec.get("staging_failed"):
        flags.append(ENV_FAILURE)
        reasons.append(f"staging failed: {spec['staging_failed']}")
    elif ledger is None or not int(ledger.get("sessions") or 0):
        # The agent's MCP client never initialised the second server: the relay or
        # QualGent-MCP did not start. Nothing the author did could have produced a case.
        flags.append(ENV_FAILURE)
        reasons.append("QualGent-MCP never started (no initialize reached the relay)")
    elif exit_code and case is None and not truncated:
        flags.append(ENV_FAILURE)
        reasons.append(f"the agent exited {exit_code} without creating a case")

    no_case_reason = None
    if case is None:
        flags.append(NO_CASE)
        if truncated:
            no_case_reason = NC_TRUNCATED
            reasons.append(f"step budget ({spec.get('step_budget')}) exhausted before a "
                           "case was created")
        elif creates:
            no_case_reason = NC_REFUSED
            reasons.append(f"create_test_case was rejected {len(creates)} time(s) "
                           "(422 from the product's validator) and never accepted")
        elif last_text and _ASKED_RE.search(last_text[-400:]):
            no_case_reason = NC_ASKED
            reasons.append("the author ended by asking instead of submitting (codex exec "
                           "is single-turn, so nobody answers)")
        else:
            no_case_reason = NC_NEVER
            reasons.append("create_test_case was never called")
    else:
        if not ground["observed_screen"]:
            flags.append(DEAD)
            reasons.append("dead: the author never read the screen, so the case is not "
                           "grounded in the app")
        elif ground["quotes"] and not ground["grounded"]:
            flags.append(DEAD)
            reasons.append(f"dead: none of the case's {ground['quotes']} quoted UI "
                           f"string(s) was ever in the device's answers "
                           f"({', '.join(ground['ungrounded'][:5])})")
    if spec.get("off_app"):
        flags.append(OFF_APP)
        reasons.append(f"the device ended in {spec.get('ended_in_package')}, not the app "
                       "under test")
    if truncated:
        flags.append(TRUNCATED)
    if contamination.contaminated:
        flags.append(CONTAMINATED)
        reasons.append("contaminated: " + ", ".join(contamination.reasons))

    valid = case is not None and not any(f in flags for f in INVALIDATING)
    outcome = CASE_CREATED if case is not None else NO_CASE_CREATED
    steps = spec.get("hook_steps")
    if steps is None:
        steps = spec.get("metered_total")
    budget = spec.get("step_budget")
    usage = pricing.usage_metrics(model, parser.token_usage())
    authored_steps = (case or {}).get("steps") or []
    metrics: dict[str, Any] = {
        "mode": MODE,
        "case_id": spec.get("case_id"),
        "app_id": spec.get("app_id"),
        "heldout": bool(spec.get("heldout")),
        "create_brief_version": CREATE_BRIEF_VERSION,
        # the artifact
        "outcome": outcome,
        "no_case_reason": no_case_reason,
        "valid_case": valid,
        "validity_flags": flags,
        "authored_case": AUTHORED_CASE_FILE if case is not None else None,
        "authored_case_id": (artifact or {}).get("test_case_id") if case is not None else None,
        "authored_name": (case or {}).get("name"),
        "authored_steps": len(authored_steps),
        "authored_version": (artifact or {}).get("version_number") if case is not None else None,
        "cases_created": int((artifact or {}).get("cases_created") or 0),
        "creates": {"accepted": sum(1 for r in creates if r.get("status") == 201),
                    "refused": sum(1 for r in creates if r.get("status") != 201)},
        "updates": {"accepted": sum(1 for r in updates if r.get("status") == 200),
                    "refused": sum(1 for r in updates if r.get("status") != 200)},
        "grounding": ground,
        # the second server
        "qualgent_calls": ledger,
        "qualgent_tool_events": len(qg_events),
        "qualgent_requests": len(requests),
        "off_surface_calls": off_surface_calls,
        "unknown_routes": unknown,
        "final_message_tail": last_text[-300:],
        # the device side (journey-compatible names, for the footer and the lanes)
        "steps": steps,
        "hook_steps": spec.get("hook_steps"),
        "step_budget": budget,
        "budget_used": (round(steps / budget, 4) if budget and steps else None),
        "device_actions": spec.get("metered_total"),
        "device_tool_calls": len(device_ok),
        "observations": ground["screen_reads"],
        "total_tool_calls": _count_tool_calls(transcript),
        "truncated": truncated,
        "timed_out": bool(spec.get("timed_out")),
        "device_serial": spec.get("device_serial"),
        "off_app": bool(spec.get("off_app")),
        "ended_in_package": spec.get("ended_in_package"),
        "env_failure": ENV_FAILURE in flags,
        # Tried the device and never got one answer: the device side was down.
        "infra_failure": bool(agent_ran and device_events and not device_ok and case is None),
        "staging_failed": spec.get("staging_failed") or "",
        "app_crashes": int(spec.get("app_crash_count") or 0),
        "reward": 1.0 if valid else 0.0,
        "reported_status": "CASE" if case is not None else "NONE",
        **contamination.as_metrics(),
        **usage,
    }
    return VerifierResult(
        passed=valid,
        score=1.0 if valid else 0.0,
        weighted_score=1.0 if valid else 0.0,
        criteria={"case_created": case is not None, "grounded": DEAD not in flags,
                  "on_app": OFF_APP not in flags, "clean": CONTAMINATED not in flags},
        failure_reason="; ".join(reasons) or None,
        metrics=metrics,
    )


# ── the board ──────────────────────────────────────────────────────────────────

def arm_label(result: RunResult) -> str:
    arm = ((result.provenance or {}).get("create") or {}).get("arm") or {}
    sha = ((arm.get("qualgent_mcp") or {}).get("sha") or "")[:7]
    return f"{arm.get('name') or '?'}@{sha or '?'}"


def summary(results: Iterable[RunResult]) -> list[dict[str, Any]]:
    """The creation board as data: one row per (agent, model, arm). Excluded episodes
    (env failure, infra failure, contamination, rate limit) are counted, never averaged
    in; every other episode counts toward `valid / episodes`."""
    from ..failures import is_excluded
    from ..leaderboard import clean_model_name

    groups: dict[tuple, list[RunResult]] = {}
    excluded: dict[tuple, int] = {}
    for r in results:
        if r.task_type != TASK_TYPE:
            continue
        key = (r.agent, clean_model_name(r.model), arm_label(r))
        if is_excluded(r.metrics or {}):
            excluded[key] = excluded.get(key, 0) + 1
            groups.setdefault(key, [])
            continue
        groups.setdefault(key, []).append(r)
    rows = []
    for key, rs in sorted(groups.items()):
        m = [r.metrics or {} for r in rs]
        priced = [x["cost_usd"] for x in m if isinstance(x.get("cost_usd"), (int, float))]
        walls = sorted(r.wall_time_sec for r in rs if r.wall_time_sec)
        flags: dict[str, int] = {}
        for x in m:
            for f in x.get("validity_flags") or []:
                flags[f] = flags.get(f, 0) + 1
        reasons: dict[str, int] = {}
        for x in m:
            if x.get("no_case_reason"):
                reasons[x["no_case_reason"]] = reasons.get(x["no_case_reason"], 0) + 1
        briefs = sorted({str(x.get("case_id")) for x in m})
        rows.append({
            "agent": key[0], "model": key[1], "arm": key[2],
            "episodes": len(rs), "excluded": excluded.get(key, 0),
            "briefs": len(briefs),
            "valid": sum(1 for x in m if x.get("valid_case")),
            "created": sum(1 for x in m if x.get("outcome") == CASE_CREATED),
            "flags": flags, "no_case_reasons": reasons,
            "cost_per_episode": (round(sum(priced) / len(priced), 4) if priced else None),
            "cost_unpriced": len(m) - len(priced),
            "minutes_per_episode": (round(walls[len(walls) // 2] / 60, 2) if walls else None),
            "brief_versions": sorted({x.get("create_brief_version") for x in m
                                      if x.get("create_brief_version") is not None}),
        })
    return rows


def valid_count(results: Iterable[RunResult]) -> int:
    return sum(1 for r in results
               if r.task_type == TASK_TYPE and (r.metrics or {}).get("valid_case"))
