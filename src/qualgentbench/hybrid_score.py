"""Hybrid scoring: recall from the verified key, false positives from replay —
each source covers the other's blind spot. `trust` is published beside the
score, never folded in."""

from __future__ import annotations

from dataclasses import dataclass, field

SCORING = "hybrid-v1"

_SPEED_WEIGHT = 0.035     # mirrors bugs.py; keep in step with it
_FP_PENALTY = 0.25

# Credit for a defect identified correctly but not demonstrated. Not zero — it
# is a real find; not full — a report nobody can reproduce is half a report.
_UNDEMONSTRATED_CREDIT = 0.5


@dataclass
class Hybrid:
    scoring: str = SCORING
    app_id: str = ""
    condition: str = ""
    seeded: int = 0
    controls: int = 0
    found: int = 0             # from the verified key
    false_positives: int = 0   # from replay, weak reproductions excluded
    weak_repro: int = 0
    undetermined: int = 0
    unverified: int = 0        # claimed a real defect but produced no usable proof
    errors: list = field(default_factory=list)
    steps: int = 0
    budget: int = 0
    trust: float = 0.0
    replay_proven: int = 0     # confirmations that replay established on its own
    notes: list = field(default_factory=list)

    @property
    def credited(self) -> float:
        """Findings weighted by whether the agent could demonstrate them."""
        return self.found + _UNDEMONSTRATED_CREDIT * self.unverified

    @property
    def recall(self) -> float:
        # Clamped: guards against `bugs_found` and the seeded set drifting apart.
        return min(1.0, self.credited / self.seeded) if self.seeded else 0.0

    @property
    def precision(self) -> float:
        d = self.credited + self.false_positives
        return self.credited / d if d else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def fp_rate(self) -> float:
        """False alarms per working control — dividing by every area would
        flatter an app with few controls."""
        return self.false_positives / self.controls if self.controls else 0.0

    @property
    def speed(self) -> float:
        used = min(self.steps / self.budget, 1.0) if self.budget else 1.0
        return 1.0 - _SPEED_WEIGHT * used

    @property
    def overall(self) -> float:
        return max(0.0, self.recall * self.speed - _FP_PENALTY * self.false_positives)

    def as_dict(self) -> dict:
        return {"scoring": self.scoring, "app_id": self.app_id,
                "condition": self.condition, "recall": round(self.recall, 4),
                "precision": round(self.precision, 4), "f1": round(self.f1, 4),
                "false_positives": self.false_positives,
                "fp_rate": round(self.fp_rate, 4),
                "overall": round(self.overall, 4), "trust": round(self.trust, 4),
                "weak_repro": self.weak_repro, "undetermined": self.undetermined,
                "unverified": self.unverified, "credited": round(self.credited, 2),
                "errors": self.errors,
                "replay_proven": self.replay_proven, "steps": self.steps,
                "seeded": self.seeded, "controls": self.controls, "notes": self.notes}


def combine(features: list, key_metrics: dict, replay: "object | None",
            unverified: int = 0, unverified_detail: list | None = None) -> Hybrid:
    """Key-based detection + replay-based false positives. `replay` None = not
    replayed: false positives fall back to the key and trust is 0.0, so an
    unreplayed episode is visibly unverified rather than silently trusted."""
    h = Hybrid(app_id=key_metrics.get("app_id", ""),
               condition=key_metrics.get("condition", "") or "",
               steps=key_metrics.get("steps") or 0,
               budget=key_metrics.get("step_budget") or 0)
    h.seeded = sum(1 for f in features if str(f.get("state")) == "broken")
    h.controls = sum(1 for f in features if str(f.get("state")) == "ok")
    h.found = key_metrics.get("bugs_found") or 0

    if replay is None:
        h.false_positives = key_metrics.get("false_alarms_count") or 0
        h.trust = 0.0
        h.notes.append("not replayed — false positives from the key, trust unverified")
        return h

    h.false_positives = replay.false_positives
    h.weak_repro = replay.weak_repro
    h.undetermined = replay.undetermined
    h.trust = replay.trust
    h.replay_proven = replay.confirmed - replay.by_fallback
    h.notes.extend(replay.notes)

    # Claimed but undemonstrated defects earn half credit. Still reported as an
    # error, because the fix is a reproduction that runs.
    h.unverified = unverified or 0
    for area, why in (unverified_detail or []):
        h.errors.append(f"{h.app_id}/{area}: claimed a defect but its reproduction "
                        f"could not be replayed — {why}")
    h.found = max(0, h.found - h.unverified)
    return h
