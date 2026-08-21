"""Pydantic models for task.yaml — the single source of truth for task configuration."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class TaskType(str, Enum):
    bug_reproduction = "bug_reproduction"
    flow_completion = "flow_completion"
    routine_repair = "routine_repair"
    regression_test = "regression_test"
    smoke_test = "smoke_test"
    exploratory = "exploratory"


class Platform(str, Enum):
    android = "android"
    ios = "ios"


class Difficulty(str, Enum):
    easy = "easy"
    medium = "medium"
    hard = "hard"


class Condition(str, Enum):
    no_routines = "no-routines"


# ── Oracle spec models ─────────────────────────────────────────────────────────

class ScreenSpec(BaseModel):
    """A UI screen the agent must visit to prove real device interaction."""
    id: str
    description: str = ""
    screen_keywords: list[str] = Field(default_factory=list)
    required: bool = True


class InteractionSpec(BaseModel):
    """A specific tool call the agent must perform to trigger or confirm the bug."""
    id: str
    description: str = ""
    tool_patterns: list[str] = Field(default_factory=list)
    target_keywords: list[str] = Field(default_factory=list)
    required: bool = True


class BugSpec(BaseModel):
    """Defines the specific bug symptom — used to tighten the fallback and correct_bug check."""
    symptom_keywords: list[str] = Field(default_factory=list)
    trigger_actions: list[str] = Field(default_factory=list)


class ReportSection(BaseModel):
    """A required section in the QA report — checked by keyword presence."""
    id: str
    keywords: list[str] = Field(default_factory=list)


class ReportSpec(BaseModel):
    """Requirements for the agent's QA report artifact."""
    min_chars: int = 300
    required_sections: list[ReportSection] = Field(default_factory=list)


class OracleSpec(BaseModel):
    """Task oracle — the ground truth spec for the verifier. Living in
    task.yaml means standard tasks need no Python, and criteria come from the
    oracle rather than generic heuristics."""
    navigation: list[ScreenSpec] = Field(default_factory=list)
    expected_interactions: list[InteractionSpec] = Field(default_factory=list)
    bug: BugSpec = Field(default_factory=BugSpec)
    report: ReportSpec = Field(default_factory=ReportSpec)
    weights: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def weights_valid(self) -> "OracleSpec":
        if self.weights:
            total = sum(self.weights.values())
            if abs(total - 1.0) > 0.01:
                raise ValueError(f"oracle.weights must sum to 1.0, got {total:.3f}")
        return self

    def effective_weights(self, keys: list[str]) -> dict[str, float]:
        """Per-criterion weights: declared values as-is, remaining weight
        shared equally among undeclared keys so the total sums to 1.0."""
        if not self.weights:
            n = len(keys)
            return {k: 1.0 / n for k in keys} if n else {}

        declared = {k: v for k, v in self.weights.items() if k in keys}
        unweighted = [k for k in keys if k not in declared]
        if unweighted:
            remaining = max(0.0, 1.0 - sum(declared.values()))
            share = remaining / len(unweighted)
            for k in unweighted:
                declared[k] = share
        return declared


# ── Task config models ─────────────────────────────────────────────────────────

class AppConfig(BaseModel):
    id: str          # references apps/manifest.yaml
    version: str     # version tag in manifest
    bundle_id: str
    platform: Platform


class SeedConfig(BaseModel):
    credentials: str = "seed/credentials.yaml"
    routines: str = "seed/routines/"


class ConditionsConfig(BaseModel):
    no_routines: bool = True


class AgentConfig(BaseModel):
    timeout_sec: int = 900
    max_tool_calls: int = 150


class VerifierConfig(BaseModel):
    command: str = "python verifier/verify.py"
    timeout_sec: int = 120


class TaskConfig(BaseModel):
    version: str = "0.1"
    id: str
    type: TaskType
    title: str
    difficulty: Difficulty = Difficulty.medium
    app: AppConfig
    seed: SeedConfig = Field(default_factory=SeedConfig)
    conditions: ConditionsConfig = Field(default_factory=ConditionsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    oracle: OracleSpec | None = None

    # Resolved at load time — not in YAML
    path: Path = Field(default=Path("."), exclude=True)

    model_config = {"arbitrary_types_allowed": True}

    def instruction_path(self) -> Path:
        return self.path / "instruction.md"

    def instruction(self) -> str:
        return self.instruction_path().read_text()

    def seed_credentials_path(self) -> Path:
        return self.path / self.seed.credentials

    def seed_routines_path(self) -> Path:
        return self.path / self.seed.routines

    def verifier_path(self) -> Path:
        return self.path / "verifier" / "verify.py"
