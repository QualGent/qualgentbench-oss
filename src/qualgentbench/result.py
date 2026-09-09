"""RunResult and VerifierResult models — written to result.json after each trial."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def relative_artifact_dir(runs_dir: Path | str | None,
                          artifact_dir: Path | str | None) -> str:
    """``artifact_dir`` as it is stored in result.json: relative to the runs dir.

    An absolute path is only true on the machine that produced it, so a runs tree
    copied anywhere else reads as a pile of dead paths. Relative survives the move.
    Falls back to the absolute path when the episode dir is not under ``runs_dir``
    (or ``runs_dir`` is unknown) — ``resolve_artifact_dir`` accepts both.
    """
    if not artifact_dir:
        return ""
    p = Path(artifact_dir)
    if runs_dir is None:
        return str(p)
    try:
        return str(p.resolve().relative_to(Path(runs_dir).resolve()))
    except (ValueError, OSError):
        return str(p)


def resolve_artifact_dir(runs_dir: Path | str,
                         result: "RunResult | str | Path | None") -> Path | None:
    """The episode dir on THIS machine, from a result written anywhere.

    Accepts a RunResult or a raw ``artifact_dir`` value. Relative is the current
    form and is joined onto ``runs_dir``; an absolute path is a legacy result.json
    (written before paths went relative) and is returned as-is. ``None`` when the
    result carries no artifact dir at all.
    """
    raw = result if isinstance(result, (str, Path)) else getattr(result, "artifact_dir", None)
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else Path(runs_dir) / p


class VerifierResult(BaseModel):
    passed: bool
    score: float = Field(ge=0.0, le=1.0)           # raw: fraction of criteria passed
    weighted_score: float = Field(default=0.0, ge=0.0, le=1.0)  # oracle-weighted leaderboard score
    criteria: dict[str, bool] = Field(default_factory=dict)
    failure_reason: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class RunResult(BaseModel):
    task_id: str
    task_version: str
    task_type: str
    agent: str
    model: str
    condition: str
    trial: int
    passed: bool
    score: float
    weighted_score: float = 0.0
    started_at: str
    ended_at: str
    wall_time_sec: float
    exit_code: int
    criteria: dict[str, bool] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    failure_reason: str | None = None
    # Episode dir RELATIVE to the runs dir (`<task_id>/<episode>`), so a runs tree
    # stays readable after it is copied to another machine. Results written before
    # that change hold an absolute path; read it through `resolve_artifact_dir`,
    # never `Path(...)` directly.
    artifact_dir: str = ""
    # One `run` invocation; lets `show --run` scope a board to a single sweep
    # instead of blending every sweep in runs/.
    run_id: str = ""
    # Where and how the episode ran (device, lane, attempt, image digest...).
    # A parallel board is only auditable with this beside every score.
    provenance: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        task_version: str,
        task_type: str,
        agent: str,
        model: str,
        condition: str,
        trial: int,
        started_at: datetime,
        ended_at: datetime,
        exit_code: int,
        verifier: VerifierResult,
        artifact_dir: Path,
        # When given, `artifact_dir` is stored relative to it (see
        # `relative_artifact_dir`). Omitted only by callers with no runs dir —
        # those keep the legacy absolute form.
        runs_dir: Path | str | None = None,
        run_id: str = "",
        provenance: dict[str, Any] | None = None,
    ) -> "RunResult":
        return cls(
            run_id=run_id,
            provenance=dict(provenance or {}),
            task_id=task_id,
            task_version=task_version,
            task_type=task_type,
            agent=agent,
            model=model,
            condition=condition,
            trial=trial,
            passed=verifier.passed,
            score=verifier.score,
            weighted_score=verifier.weighted_score,
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            wall_time_sec=(ended_at - started_at).total_seconds(),
            exit_code=exit_code,
            criteria=verifier.criteria,
            metrics=verifier.metrics,
            failure_reason=verifier.failure_reason,
            artifact_dir=relative_artifact_dir(runs_dir, artifact_dir),
        )

    def write(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2))

    def ctrf(self) -> dict[str, Any]:
        """CTRF-compatible output for tooling integration."""
        status = "passed" if self.passed else "failed"
        return {
            "results": {
                "tool": {"name": "qualgent-bench", "version": "0.1.0"},
                "summary": {
                    "tests": 1,
                    "passed": 1 if self.passed else 0,
                    "failed": 0 if self.passed else 1,
                    "skipped": 0,
                    "pending": 0,
                    "other": 0,
                    "start": self.started_at,
                    "stop": self.ended_at,
                },
                "tests": [
                    {
                        "name": self.task_id,
                        "status": status,
                        "duration": int(self.wall_time_sec * 1000),
                    }
                ],
            }
        }

    def write_ctrf(self, path: Path) -> None:
        path.write_text(json.dumps(self.ctrf(), indent=2))
