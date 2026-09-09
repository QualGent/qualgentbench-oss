"""The run config file: one agent, one model, a
scope, and the devices to use. Shape only — whether the values are *runnable*
(agent installed, tier ready, APKs present...) is preflight.py's job, so the
allowed-value lists live in the harness once, never in a launcher."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


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
