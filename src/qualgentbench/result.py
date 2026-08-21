"""RunResult and VerifierResult models — written to result.json after each trial."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


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
    artifact_dir: str = ""

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
    ) -> "RunResult":
        return cls(
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
            artifact_dir=str(artifact_dir),
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
