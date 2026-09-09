"""The run config file: one agent, one model, a
scope, and the devices to use. Shape only — whether the values are *runnable*
(agent installed, tier ready, APKs present...) is preflight.py's job, so the
allowed-value lists live in the harness once, never in a launcher."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .credit import DEFAULT_STOP_AT_SEVEN_DAY_PCT


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

    Only the seven-day window is a stop *threshold*: it is the budget the sweep is
    spending, and past the configured percentage the run exits 75 with a checkpoint
    somebody else can pick up. A five-hour block always stops the run too, but it is
    not configurable — it is a wait, and `wait_for_five_hour_reset` tells the LAUNCHER
    whether to sit it out and resume, or to hand back and stop. The harness itself
    stops either way; nothing in a single `run` invocation sleeps for hours.

    Both keys are inert unless the agent reports usage windows (claude-code on
    subscription auth today). See credit.py.
    """
    model_config = ConfigDict(extra="forbid")
    # Percentage, 0-100 — NOT the 0-1 fraction the provider reports. The default (100)
    # means "only when the window is spent", i.e. off unless asked for. Taken from
    # credit.py so the config, the CLI flag and the guard cannot disagree about it.
    stop_at_seven_day_pct: int = Field(DEFAULT_STOP_AT_SEVEN_DAY_PCT, ge=0, le=100)
    wait_for_five_hour_reset: bool = True


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
    runs_dir: str = "runs"
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
