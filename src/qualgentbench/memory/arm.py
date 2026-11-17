"""The memory ARM of one episode: which memory the agent gets, and how (QUA-3488).

An arm is three values plus where the memory lives:

- `mode`    `none` | `static` | `dynamic`. `none` is today's episode, byte for byte: no
            render output, no file, no flag, no env, the default condition label. `static`
            and `dynamic` render and inject the same way; they differ only in where the
            memory root comes from (hand-written seed vs a curated stage snapshot), which
            the caller decides.
- `budget`  `small` | `large` | `index` (`render.BUDGETS`). `index` also places the full
            files in the workspace at `memory/` (`render.place_workspace`).
- `diff`    `none` | `diff` | `hunt`. Anything but `none` renders the app's
            `<app>.changes.md`; the snapshot carries the right changes text for the arm
            (`hunt` = diff plus Qualock's hunt findings, built upstream).
- `snapshot` the memory root directory (`org/<org>.md`, `app/<org>/<app>.md`, ...). Required
            for `dynamic`; for `static` it defaults to the packaged `data/memory/`.
- `org`     the org key; default `format.find_org(root, app_id)`.

The rendered block never goes into the brief (`journey.brief` is byte-identical across
arms). It reaches the agent through a system channel: codex-cli as `developer_instructions`
in its per-episode config.toml, claude-code through `--append-system-prompt-file` with its
own auto-memory turned off. A missing memory file fails the episode; it never falls back
to no memory.

After the agent exits, the harness records `workspace/memory_proposals.yaml` if the agent
wrote one. Its schema belongs to `memory.proposals` (the curator ticket); this module only
locates the file and fingerprints it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import format as fmt
from . import render as _render

MODE_NONE, MODE_STATIC, MODE_DYNAMIC = "none", "static", "dynamic"
MEMORY_MODES = (MODE_NONE, MODE_STATIC, MODE_DYNAMIC)
MEMORY_BUDGETS = (_render.SMALL, _render.LARGE, _render.INDEX)
DIFF_NONE, DIFF_DIFF, DIFF_HUNT = "none", "diff", "hunt"
MEMORY_DIFFS = (DIFF_NONE, DIFF_DIFF, DIFF_HUNT)

#: The agents that can take injected memory. `native` has no file tools and no system
#: channel the bench controls per arm, so a memory arm on it is refused.
MEMORY_AGENTS = ("claude-code", "codex-cli")

#: Where the agent may write proposals, relative to its workspace. The name is the
#: contract with `memory.proposals.FILENAME` (QUA-3486).
PROPOSALS_FILENAME = "memory_proposals.yaml"
#: The rendered system block, beside the workspace (never inside it): claude-code reads
#: it with `--append-system-prompt-file`; for codex-cli it is the provenance copy of the
#: `developer_instructions` text.
SYSTEM_FILENAME = "memory.md"

#: Memory conditions are internal-only (D13): `<tooling>+mem-<mode>-<budget>-<diff>`.
#: Path-safe on purpose: the condition is part of the episode directory name.
CONDITION_MARKER = "+mem-"

#: The packaged hand-written seed (QUA-3487 writes it), used by `static` without
#: `snapshot`.
DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "data" / "memory"


class MemoryArmError(ValueError):
    pass


def condition_label(tooling: str, mode: str, budget: str, diff: str) -> str:
    """The board condition of a memory episode, e.g. `mcp+mem-dynamic-small-diff`."""
    return f"{tooling}{CONDITION_MARKER}{mode}-{budget}-{diff}"


def is_memory_condition(condition: str | None) -> bool:
    """Is this board condition a memory arm (internal-only, D13)?"""
    return CONDITION_MARKER in str(condition or "")


def parse_condition(condition: str) -> dict[str, str] | None:
    """`mcp+mem-dynamic-small-diff` → {tooling, mode, budget, diff}; None for any other."""
    if not is_memory_condition(condition):
        return None
    tooling, _, rest = condition.partition(CONDITION_MARKER)
    parts = rest.split("-")
    if len(parts) != 3:
        return None
    return {"tooling": tooling, "mode": parts[0], "budget": parts[1], "diff": parts[2]}


@dataclass(frozen=True)
class MemoryArm:
    mode: str = MODE_NONE
    budget: str = _render.SMALL
    diff: str = DIFF_NONE
    snapshot: Path | None = None
    org: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in MEMORY_MODES:
            raise MemoryArmError(f"memory mode must be one of {MEMORY_MODES}, not {self.mode!r}")
        if self.budget not in MEMORY_BUDGETS:
            raise MemoryArmError(
                f"memory budget must be one of {MEMORY_BUDGETS}, not {self.budget!r}")
        if self.diff not in MEMORY_DIFFS:
            raise MemoryArmError(f"memory diff must be one of {MEMORY_DIFFS}, not {self.diff!r}")
        if self.mode == MODE_DYNAMIC and self.snapshot is None:
            raise MemoryArmError("memory mode `dynamic` needs a snapshot (the stage's "
                                 "memory root); it never falls back to the static seed")
        if self.snapshot is not None:
            object.__setattr__(self, "snapshot", Path(self.snapshot).expanduser())

    @property
    def enabled(self) -> bool:
        return self.mode != MODE_NONE

    @property
    def include_changes(self) -> bool:
        return self.diff != DIFF_NONE

    def root(self) -> Path:
        return self.snapshot if self.snapshot is not None else DEFAULT_ROOT

    def condition(self, tooling: str) -> str | None:
        """The condition label, or None for `none` (the episode keeps `mcp` / `raw`)."""
        return condition_label(tooling, self.mode, self.budget, self.diff) if self.enabled else None

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe, for plan.json and provenance; `from_dict` reads it back."""
        return {"mode": self.mode, "budget": self.budget, "diff": self.diff,
                "snapshot": str(self.snapshot) if self.snapshot is not None else None,
                "org": self.org}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, base: Path | None = None) -> MemoryArm:
        """Keys `mode`, `budget`, `diff`, `snapshot`, `org`; any other key is refused. A
        relative `snapshot` is resolved against `base` (the config file's directory)."""
        data = dict(data or {})
        unknown = sorted(set(data) - {"mode", "budget", "diff", "snapshot", "org"})
        if unknown:
            raise MemoryArmError(f"unknown memory key(s): {', '.join(unknown)}")
        snap = data.get("snapshot")
        if snap:
            snap = Path(str(snap)).expanduser()
            if base is not None and not snap.is_absolute():
                snap = base / snap
        return cls(mode=str(data.get("mode") or MODE_NONE),
                   budget=str(data.get("budget") or _render.SMALL),
                   diff=str(data.get("diff") or DIFF_NONE),
                   snapshot=snap or None, org=data.get("org") or None)

    @classmethod
    def load(cls, path: Path | str) -> MemoryArm:
        """A memory config file: JSON, or YAML when the text is not JSON. The lab writes
        it root-only, so the snapshot path never sits in the agent-visible argv."""
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            import yaml

            data = yaml.safe_load(text)
        if isinstance(data, dict) and isinstance(data.get("memory"), dict):
            data = data["memory"]
        if not isinstance(data, dict):
            raise MemoryArmError(f"{path}: expected a mapping of memory keys")
        return cls.from_dict(data, base=path.parent)

    def check_agent(self, agent: str) -> None:
        if self.enabled and agent not in MEMORY_AGENTS:
            raise MemoryArmError(f"memory arms run on {', '.join(MEMORY_AGENTS)} only, not "
                                 f"{agent!r}")

    def check_app(self, app_id: str) -> str:
        """The org key for `app_id`, after checking the files a render will read exist.
        Cheap; callers run it before any device is touched."""
        root = self.root()
        if not root.is_dir():
            raise FileNotFoundError(f"memory root missing: {root}")
        org = self.org or fmt.find_org(root, app_id)
        need = [fmt.org_path(root, org), fmt.app_path(root, org, app_id)]
        if self.include_changes:
            need.append(fmt.changes_path(root, org, app_id))
        for p in need:
            if not p.is_file():
                raise FileNotFoundError(f"memory file missing: {p}")
        return org


# What memory is for and how to propose entries. Plain guidance, no rigid rules (spec
# §3.4: a "record only failures" rule measured worse). Identical for every memory arm,
# so static and dynamic differ only in the memory itself.
GUIDE = """\
# Memory from earlier runs

QualGent keeps notes about this app and the company that makes it, from earlier test
runs and from people who know the app. They are in the <qualgent_memory> block below.
Use them the way you would use a colleague's notes: they can save you steps, such as
where a screen is or how to get past setup. They can also be incomplete or out of date,
because the app may have changed since they were written. The device shows the app as
it is now.

You may propose additions or corrections for later runs on this app: something you had
to work out that would have saved you time, a note that turned out wrong, an area that
was slow or unreliable. A reviewer reads the proposals after the run, so nothing you
propose changes memory during this run. Proposing is optional and is not part of the
task you are given.
"""

# The proposal file format. QUA-3486 owns the schema and exposes it as
# `memory.proposals.FORMAT_HELP`; until that module is on this branch, this is the same
# shape (spec §3.4: type, text, evidence, optional updates/contradicts).
# TODO(QUA-3486): drop the fallback once memory.proposals is merged into the epic.
_FALLBACK_FORMAT_HELP = f"""\
Memory proposals go in `{PROPOSALS_FILENAME}` in the workspace, as a YAML list. Append one
item per fact:

- type: nav            # one of: nav, setup, flaky, bug, note
  text: <one sentence>
  evidence: {{step: <step number>, screen: <screen name>}}
  updates: <entry id>      # optional, when it refines an existing memory entry
  contradicts: <entry id>  # optional, when an existing memory entry is wrong
"""


def format_help() -> str:
    try:
        from .proposals import FORMAT_HELP  # type: ignore[attr-defined]
    except ImportError:
        return _FALLBACK_FORMAT_HELP
    return FORMAT_HELP


def system_text(rendered_block: str) -> str:
    """The whole system block one memory episode receives."""
    return f"{GUIDE}\n{format_help()}\n{rendered_block}"


@dataclass
class PreparedMemory:
    """One episode's rendered memory, before (and after) it is written to disk."""
    arm: MemoryArm
    app_id: str
    org: str
    rendered: _render.Rendered
    text: str
    system_file: Path | None = None
    workspace_files: list[Path] = field(default_factory=list)

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def install(self, run_dir: Path, workspace_dir: Path) -> None:
        """Write the system file beside the workspace and, for `index`, the full files
        into `<workspace>/memory/`. Names never collide with an agent instruction file
        (`config.INSTRUCTION_FILES`), so the inherited-instructions guard stays strict."""
        self.system_file = Path(run_dir) / SYSTEM_FILENAME
        self.system_file.write_text(self.text, encoding="utf-8")
        if self.arm.budget == _render.INDEX:
            self.workspace_files = _render.place_workspace(
                self.arm.root(), self.org, self.app_id, workspace_dir,
                include_changes=self.arm.include_changes)

    def provenance(self) -> dict[str, Any]:
        return {**self.arm.as_dict(), "org_key": self.org, "app_key": self.app_id,
                "snapshot_hash": self.rendered.snapshot, "prompt_sha256": self.sha256,
                "included": list(self.rendered.included),
                "dropped": list(self.rendered.dropped),
                "changes_truncated": self.rendered.changes_truncated,
                "workspace_files": [p.name for p in self.workspace_files]}


def prepare(arm: MemoryArm | None, app_id: str) -> PreparedMemory | None:
    """Render `arm` for one app; None when the arm is off. Pure: reads the memory root,
    writes nothing. Raises FileNotFoundError when a file the arm needs is missing."""
    if arm is None or not arm.enabled:
        return None
    if not app_id:
        raise MemoryArmError("a memory arm needs the episode's app id")
    org = arm.check_app(app_id)
    rendered = _render.render(arm.root(), org, app_id, arm.budget,
                              include_changes=arm.include_changes)
    return PreparedMemory(arm=arm, app_id=app_id, org=org, rendered=rendered,
                          text=system_text(rendered.text))


def collect_proposals(workspace_dir: Path) -> dict[str, Any]:
    """Where the agent's proposals file is, as episode-result metrics. The file itself
    stays in the workspace, which every upload already carries."""
    path = Path(workspace_dir) / PROPOSALS_FILENAME
    try:
        data = path.read_bytes()
    except OSError:
        return {"memory_proposals_file": None, "memory_proposals_bytes": 0,
                "memory_proposals_sha256": None}
    return {"memory_proposals_file": f"workspace/{PROPOSALS_FILENAME}",
            "memory_proposals_bytes": len(data),
            "memory_proposals_sha256": "sha256:" + hashlib.sha256(data).hexdigest()}


def episode_metrics(prepared: PreparedMemory, condition: str,
                    workspace_dir: Path) -> dict[str, Any]:
    """The flat metrics a memory episode adds to result.json. `memory_mode` is the
    RENDER mode (`Rendered.metrics()`, the budget value); the arm's mode is
    `memory_arm_mode`."""
    out = dict(prepared.rendered.metrics())
    out.update({"memory_condition": condition,
                "memory_arm_mode": prepared.arm.mode,
                "memory_budget": prepared.arm.budget,
                "memory_diff": prepared.arm.diff,
                "memory_prompt_sha256": prepared.sha256,
                "memory_internal": True})
    out.update(collect_proposals(workspace_dir))
    return out
