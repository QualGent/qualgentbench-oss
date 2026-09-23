"""The run config file: one agent, one model, a
scope, and the devices to use. Shape only — whether the values are *runnable*
(agent installed, tier ready, APKs present...) is preflight.py's job, so the
allowed-value lists live in the harness once, never in a launcher."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .credit import DEFAULT_STOP_AT_SEVEN_DAY_PCT

# ── Where episodes land (QUA-2778) ────────────────────────────────────────────
#
# An episode's agent runs with cwd = <runs_dir>/<task>/<run>/workspace, and both
# coding agents read instruction files off that cwd's ANCESTORS as start-up context:
# claude-code walks every ancestor up to `/` (CLAUDE.md, CLAUDE.local.md,
# .claude/CLAUDE.md, .claude/rules/*.md — probed with `claude -p /context`,
# 2026-09-23), and codex-cli reads AGENTS.md / AGENTS.override.md from the git root
# down to its cwd. With runs under the repo, this repo's CLAUDE.md — which names
# journey defect ids and their mechanisms — arrived as system context, where the
# contamination scanner (tool inputs and results only) cannot see it. So host runs
# default OUTSIDE the tree, and `run` refuses a runs dir that is inside it or that
# has an instruction file anywhere on its ancestor chain.

#: The harness's own tree (the repo root in a checkout, /app in the image).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: How the default is spelled in help text and messages.
DEFAULT_RUNS_DIR_DISPLAY = "~/.qualgentbench/runs"

#: Escape hatch, set by `run --allow-runs-in-repo`; read by the per-episode check in
#: episode_runner, so the flag does not have to be threaded through the lanes.
ALLOW_RUNS_IN_REPO_ENV = "QGB_ALLOW_RUNS_IN_REPO"

#: Per directory: the files (and one rules dir) an agent loads as instructions.
INSTRUCTION_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENTS.override.md",
                     ".claude/CLAUDE.md")
INSTRUCTION_DIRS = (".claude/rules",)


def default_runs_dir() -> Path:
    """Where host runs land when neither `--runs-dir` nor the config names one."""
    return Path.home() / ".qualgentbench" / "runs"


def resolve_runs_dir(value: str | Path | None) -> Path:
    """`--runs-dir` / `runs_dir:` as a path; None or empty means the default."""
    return Path(value).expanduser() if value else default_runs_dir()


def allow_runs_in_repo() -> bool:
    return os.environ.get(ALLOW_RUNS_IN_REPO_ENV, "").strip().lower() not in (
        "", "0", "false", "no")


def _nonempty(path: Path) -> bool:
    # An empty CLAUDE.md loads nothing (claude-code lists no memory file for it),
    # and ~/.claude/CLAUDE.md is often an empty placeholder.
    try:
        return path.is_file() and bool(path.read_text(errors="replace").strip())
    except OSError:
        return True   # unreadable is not provably empty


def instruction_files_on_chain(path: str | Path) -> list[Path]:
    """Every non-empty agent instruction file in `path` or any of its ancestors —
    what a coding agent started with cwd=`path` could load as context. `path` need
    not exist yet: its ancestors are what matter."""
    p = Path(path).expanduser().resolve()
    found: list[Path] = []
    for d in (p, *p.parents):
        found += [d / n for n in INSTRUCTION_FILES if _nonempty(d / n)]
        for sub in INSTRUCTION_DIRS:
            rules = d / sub
            if rules.is_dir():
                found += sorted(f for f in rules.rglob("*.md") if _nonempty(f))
    return found


def runs_dir_problems(runs_dir: str | Path) -> list[str]:
    """Why an agent whose workspace is under `runs_dir` would inherit context it
    must not see. Empty = safe."""
    p = Path(runs_dir).expanduser().resolve()
    problems: list[str] = []
    if p == REPO_ROOT or REPO_ROOT in p.parents:
        problems.append(f"{p} is inside the harness tree {REPO_ROOT}")
    problems += [f"{f} would be read by the agent as instructions"
                 for f in instruction_files_on_chain(p)]
    return problems


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tiers: list[str] = Field(default_factory=list)
    apps: list[str] = Field(default_factory=list)
    mode: Literal["guided", "hunt", "journey", "all"] = "hunt"
    trials: int = Field(1, ge=1)

    @model_validator(mode="after")
    def _something_selected(self) -> "Scope":
        if not self.tiers and not self.apps:
            raise ValueError("scope needs `tiers` and/or `apps` — nothing is selected")
        return self


class Devices(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # AVD names the launcher boots on the host (scripts/launch.py).
    avds: list[str] = Field(default_factory=list)
    # Already-running adb serials (what `run --devices` receives).
    serials: list[str] = Field(default_factory=list)
    max_lanes: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def _no_duplicates(self) -> "Devices":
        for name, values in (("avds", self.avds), ("serials", self.serials)):
            dupes = sorted({v for v in values if values.count(v) > 1})
            if dupes:
                raise ValueError(f"devices.{name} lists {', '.join(dupes)} more than once")
        return self

    def lane_count(self) -> int:
        n = len(self.serials) or len(self.avds)
        return min(n, self.max_lanes) if self.max_lanes else n


class Checkpoint(BaseModel):
    """When a sweep should stop itself so it can be finished later.

    Only the weekly windows carry a stop *threshold*: they are the budget the sweep
    is spending, and past the configured percentage the run exits 75 with a checkpoint
    somebody else can pick up. A five-hour block always stops the run too, but it is
    not configurable — it is a wait, and `wait_for_five_hour_reset` tells the LAUNCHER
    whether to sit it out and resume, or to hand back and stop. The harness itself
    stops either way; nothing in a single `run` invocation sleeps for hours.

    `stop_at_seven_day_pct` is ONE knob over every weekly window a plan reports — the
    generic `seven_day` and any model-scoped cap beside it — applied to whichever of
    them reads highest. Per-model keys were considered and rejected: the sweep cannot
    buy another episode once any cap it needs is spent, so a second number would only
    give the operator a way to set a ceiling the run cannot honour.

    Both keys are inert unless the agent reports usage windows (claude-code on
    subscription auth today). See credit.py.
    """
    model_config = ConfigDict(extra="forbid")
    # Percentage, 1-100 — NOT the 0-1 fraction the provider reports. The default (100)
    # means "only when the window is spent", i.e. off unless asked for. Taken from
    # credit.py so the config, the CLI flag and the guard cannot disagree about it.
    # A ceiling on the HIGHEST weekly window, not on `seven_day` alone.
    #
    # 0 is REFUSED rather than accepted, because it is the one value whose plain
    # reading is the opposite of its behaviour: it reads as "off" and means "stop at
    # 0% used", i.e. stop a completely healthy run before its first episode. There is
    # no way to spell "off" here that 100 does not already spell.
    stop_at_seven_day_pct: int = Field(DEFAULT_STOP_AT_SEVEN_DAY_PCT, ge=0, le=100)
    wait_for_five_hour_reset: bool = True

    @model_validator(mode="after")
    def _zero_is_not_a_way_to_say_off(self) -> "Checkpoint":
        # Refused here rather than by the field bound so the message can say why: the
        # bound would print "greater than or equal to 1", which does not tell a user
        # who typed 0 meaning "off" that they asked for the opposite.
        if self.stop_at_seven_day_pct == 0:
            raise ValueError(
                "0 does not mean 'off' — it means 'stop once the seven-day window is "
                "0% used', which stops a healthy sweep before its first episode. Use "
                "100 (the default) to stop only when the window is spent, or a real "
                "budget like 90")
        return self


class BenchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Read by the launcher only; the harness inside the image ignores it.
    image: str | None = None
    agent: str
    model: str = Field(min_length=1)
    scope: Scope
    devices: Devices = Field(default_factory=Devices)
    mcp_server: str | None = None
    env_file: str | None = None
    # None = `default_runs_dir()`, outside the repo (QUA-2778). A relative path is
    # taken from the current directory, as `--runs-dir` is.
    runs_dir: str | None = None
    # The held-out split's data root (same layout as src/qualgentbench/data/), relative
    # to the config file. Sets QGB_HELDOUT_DIR for the run unless the environment
    # already has one — the env var is the single source every loader reads.
    heldout_dir: str | None = None
    checkpoint: Checkpoint = Field(default_factory=Checkpoint)


class ConfigError(Exception):
    """One message per problem, already phrased for the user."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("\n".join(problems))


def load_config(path: Path) -> BenchConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except OSError as exc:
        raise ConfigError([f"cannot read {path}: {exc}"]) from exc
    except yaml.YAMLError as exc:
        raise ConfigError([f"{path} is not valid YAML: {exc}"]) from exc
    if not isinstance(raw, dict):
        raise ConfigError([f"{path} must be a mapping at the top level"])
    try:
        return BenchConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError([_describe(e) for e in exc.errors()]) from exc


def _describe(err: dict) -> str:
    loc = ".".join(str(p) for p in err.get("loc", ())) or "(top level)"
    msg = err.get("msg", "invalid")
    if err.get("type") == "extra_forbidden":
        return f"{loc}: unknown key"
    if err.get("type") == "missing":
        return f"{loc}: required"
    return f"{loc}: {msg}"
