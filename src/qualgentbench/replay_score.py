"""Score an episode from REPLAYED reproductions instead of an answer key.
Recall still counts against the seeded list, and UNREPLAYABLE costs the
benchmark information, never the agent points."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import replay as rp

SCORING = "replay-v1"

# Claim outcomes that mean "the agent asserted a defect its own steps do not show".
_FALSE = (rp.DOES_NOT_REPRODUCE, rp.NOT_A_DEFECT)


@dataclass
class ReplayScore:
    scoring: str = SCORING
    seeded_total: int = 0
    confirmed: int = 0                 # seeded defects proven by the agent's own repro
    by_fallback: int = 0               # counted via the key because replay could not decide
    missed: int = 0
    false_positives: int = 0
    weak_repro: int = 0                # right about the area, wrong about the evidence
    undetermined: int = 0              # no derived label to adjudicate against
    unreplayable: int = 0
    replayable_claims: int = 0
    replayable_possible: int = 0       # areas that COULD be replayed (have a check)
    counts: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.confirmed / self.seeded_total if self.seeded_total else 0.0

    @property
    def precision(self) -> float:
        # UNREPLAYABLE is not in this denominator on purpose.
        decided = self.confirmed + self.false_positives
        return self.confirmed / decided if decided else 1.0

    @property
    def trust(self) -> float:
        """How much of this episode's score was executed rather than believed."""
        if not self.replayable_possible:
            return 0.0
        return min(1.0, self.replayable_claims / self.replayable_possible)

    def as_dict(self) -> dict:
        return {"scoring": self.scoring, "recall": round(self.recall, 4),
                "precision": round(self.precision, 4), "trust": round(self.trust, 4),
                "seeded_total": self.seeded_total, "confirmed": self.confirmed,
                "by_fallback": self.by_fallback, "missed": self.missed,
                "false_positives": self.false_positives,
                "weak_repro": self.weak_repro, "undetermined": self.undetermined,
                "unreplayable": self.unreplayable,
                "replayable_claims": self.replayable_claims,
                "replayable_possible": self.replayable_possible,
                "counts": self.counts, "notes": self.notes}


def score(features: list, replay_results: list, key_verdicts: dict | None = None,
          derived: dict | None = None) -> ReplayScore:
    """Combine spec features with replay classifications into a scored episode.
    `key_verdicts` only rescues UNREPLAYABLE claims; `derived` turns a failed
    repro on a broken area into a weak reproduction, not a false positive."""
    out = ReplayScore()
    by_area = {r.get("area"): r for r in replay_results if r.get("area")}
    for r in replay_results:
        c = r.get("classification")
        out.counts[c] = out.counts.get(c, 0) + 1

    seeded = [f for f in features if str(f.get("state")) == "broken" and f.get("bug_id")]
    out.seeded_total = len(seeded)
    # Only areas carrying a `check:` can be replayed; counting the rest would
    # charge the agent for a gap in the corpus.
    out.replayable_possible = sum(1 for f in features if f.get("check"))
    out.replayable_claims = sum(
        1 for r in replay_results if r.get("classification") != rp.UNREPLAYABLE)

    for f in seeded:
        area = f["id"]
        res = by_area.get(area)
        cls = res.get("classification") if res else None
        if cls == rp.CONFIRMED:
            out.confirmed += 1
        elif cls == rp.REPRODUCED_SEEDED:
            # Demonstrated on the seeded build; only the clean-arm comparison broke
            # at a step (display defects change anchor text between builds). This
            # loop only visits seeded areas, whose ×3-derived truth already proved
            # the seeding causes the breakage — full credit, trust not docked.
            out.confirmed += 1
            out.notes.append(f"{area}: reproduced on the seeded build; the clean-arm "
                             f"pass could not complete — credited via derived truth")
        elif cls in (None, rp.UNREPLAYABLE):
            # No usable repro: fall back to what the agent SAID, so it costs
            # evidence, not credit.
            said = (key_verdicts or {}).get(area)
            if said == "deviates":
                out.confirmed += 1
                out.by_fallback += 1
                out.notes.append(f"{area}: counted from the agent's verdict — "
                                 f"{'no reproduction' if res is None else 'unreplayable'}")
            else:
                out.missed += 1
            if res is not None:
                out.unreplayable += 1
        elif cls in _FALSE and (derived or {}).get(area) == "broken":
            # Right about the area, weak about the evidence — credited, not
            # charged as both a miss and a false positive.
            out.confirmed += 1
            out.by_fallback += 1
        else:
            out.missed += 1
            if cls == rp.MISSED_DEFECT:
                out.notes.append(f"{area}: the agent's own repro shows the defect it "
                                 f"reported as working")

    for area, r in by_area.items():
        if r.get("claimed") != "deviates":
            continue
        label = (derived or {}).get(area)
        cls = r.get("classification")
        if cls not in _FALSE:
            # UNREPLAYABLE is non-punitive EXCEPT against an area measured to
            # work: an undemonstrable defect claim on a control is a false
            # report, else "claim everything, repros that don't replay" wins.
            # REPRODUCED_SEEDED against a derived-ok area is the same charge —
            # a repro that "violates" on a control contradicts the ×3 truth.
            if (cls in (rp.UNREPLAYABLE, rp.REPRODUCED_SEEDED)
                    and label not in ("broken", None, "", "undecidable")):
                out.false_positives += 1
                out.notes.append(f"{area}: reported broken, derived {label}, and its "
                                 f"reproduction could not be replayed at all")
            continue
        if label == "broken":
            # Measured as broken; the claim is CORRECT and only the evidence is weak.
            out.weak_repro += 1
            out.notes.append(f"{area}: correct (derived broken) but its reproduction "
                             f"did not replay — weak evidence, not a false report")
        elif label in (None, "", "undecidable"):
            out.undetermined += 1
            out.notes.append(f"{area}: no derived label — cannot adjudicate, not charged")
        else:
            out.false_positives += 1
            out.notes.append(f"{area}: reported broken, derived {label}, "
                             f"{r['classification']}")
    return out
