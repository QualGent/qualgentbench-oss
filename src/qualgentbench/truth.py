"""Derive what is actually broken by running each spec `check` with the seeded
defects on and again off; the difference gives broken/ok/upstream/inverted/
undecidable. A `bug_id` check that does not flip means bad check or seeding."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import replay as rp
from .submission import Claim, Step, _parse_expect

logger = logging.getLogger(__name__)

BROKEN = "broken"
OK = "ok"
UPSTREAM = "upstream"
INVERTED = "inverted"
UNDECIDABLE = "undecidable"

# `collateral` is a scoring policy laid over "broken" — the on/off difference
# cannot tell the two apart, so treat them as agreeing.
_SAME_AS = {"collateral": BROKEN}


@dataclass
class Derived:
    area: str
    declared: str | None          # the hand-written `state:`, for diffing
    derived: str
    on: rp.ReplayResult | None = None
    off: rp.ReplayResult | None = None

    @property
    def agrees(self) -> bool:
        if self.declared is None:
            return False
        return _SAME_AS.get(self.declared, self.declared) == self.derived

    def as_dict(self) -> dict:
        return {"area": self.area, "declared": self.declared, "derived": self.derived,
                "agrees": self.agrees,
                "on": self.on.as_dict() if self.on else None,
                "off": self.off.as_dict() if self.off else None}


def _steps(raw: object) -> list[Step]:
    steps: list[Step] = []
    for item in raw or []:  # type: ignore[union-attr]
        if isinstance(item, str):
            steps.append(Step(item.strip().lower()))
        elif isinstance(item, dict) and len(item) == 1:
            (k, v), = item.items()
            steps.append(Step(str(k).strip().lower(), str(v if v is not None else "").strip()))
    return steps


def setup_of(exploration: dict) -> list[Step]:
    """The app's `check_setup:` steps — HARNESS-ONLY, run once before the
    snapshot to get past first-run onboarding; the agent never sees it."""
    raw = (exploration or {}).get("check_setup")
    return _steps(raw.get("steps") if isinstance(raw, dict) else raw)


def check_of(feature: dict) -> Claim | None:
    """The area's check as a replayable Claim, or None if it has no `check:` yet."""
    raw = feature.get("check")
    if not isinstance(raw, dict):
        return None
    steps = _steps(raw.get("steps"))
    # Same parser the agent's reproductions use, so the two cannot drift apart;
    # spec checks may additionally use the harness-only `db` oracle.
    expect, error = _parse_expect(raw.get("expect"), feature["id"])
    if not steps or expect is None:
        if error:
            logger.warning("%s: unusable check — %s", feature["id"], error)
        return None
    return Claim(area=feature["id"], verdict="", steps=steps, expect=expect)


def classify(on: rp.ReplayResult, off: rp.ReplayResult | None) -> str:
    if on.outcome == rp.INCONCLUSIVE or (off is not None and off.outcome == rp.INCONCLUSIVE):
        return UNDECIDABLE
    if on.outcome == rp.HOLDS:
        # Correct with defects live: a control if also correct without them;
        # if the seeding is what makes it work, the episode is unsafe.
        if off is None or off.outcome == rp.HOLDS:
            return OK
        return INVERTED
    return BROKEN if off is not None and off.outcome == rp.HOLDS else UPSTREAM


async def _pass(serial: str, bundle: str, claim: Claim, flags: Sequence[str],
                snap: Path | None, attempts: int = 2,
                shared: Sequence[str] | None = None,
                shared_snap: Path | None = None,
                device_setup: dict | None = None) -> rp.ReplayResult:
    """The replayer's own reset-and-replay (`replay._pass`), so derivation and
    episode verification resolve an ambiguous anchor the same way: INCONCLUSIVE
    retries the OTHER candidate. A private copy here once lacked that bump and
    derived three selection-mode areas `undecidable` that replay could prove."""
    return await rp._pass(serial, bundle, claim, flags, snap, attempts=attempts,
                          shared=shared, shared_snap=shared_snap,
                          device_setup=device_setup)


async def derive_area(serial: str, bundle: str, feature: dict,
                      seeded: Sequence[str], snap: Path | None = None,
                      shared: Sequence[str] | None = None,
                      shared_snap: Path | None = None,
                      device_setup: dict | None = None) -> Derived | None:
    claim = check_of(feature)
    if claim is None:
        return None
    declared = feature.get("state")

    on = await _pass(serial, bundle, claim, seeded, snap,
                     shared=shared, shared_snap=shared_snap, device_setup=device_setup)
    if on.outcome == rp.INCONCLUSIVE:
        return Derived(feature["id"], declared, UNDECIDABLE, on)

    # Always run the clean pass: an area that only works BECAUSE of the seeding
    # (inverted) is otherwise invisible.
    off = await _pass(serial, bundle, claim, [], snap,
                      shared=shared, shared_snap=shared_snap, device_setup=device_setup)
    return Derived(feature["id"], declared, classify(on, off), on, off)


async def derive_app(serial: str, bundle: str, features: Sequence[dict],
                     seeded: Sequence[str], snap: Path | None = None,
                     progress=None, shared: Sequence[str] | None = None,
                     shared_snap: Path | None = None,
                     device_setup: dict | None = None) -> list[Derived]:
    out: list[Derived] = []
    for i, feature in enumerate(features, 1):
        got = await derive_area(serial, bundle, feature, seeded, snap,
                                shared, shared_snap, device_setup=device_setup)
        if got is None:
            continue
        out.append(got)
        if progress:
            progress(i, len(features), got)
    return out
