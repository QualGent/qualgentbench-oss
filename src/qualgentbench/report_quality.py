"""Report quality for saved journey episodes: a block BESIDE the ranking score (QUA-3584).

The journey scorer (`journey.journey_verdict`) decides whether a report is a catch. It
says nothing about whether a developer could act on the report. This module scores that,
one yes/no gate at a time, and writes the answer to `quality.json` next to the episode's
`result.json`.

Three rules keep it apart from the ranking:

* It only READS `result.json` (and, for later gates, the other saved artifacts). It never
  writes `result.json` and never changes a verdict, a rate or a ranking key.
* The scoring modules never import it, so it is not in the pinned scorer closure
  (`tests/scorer_closure.py`) and `journey.SCORER_VERSION` does not move when it changes.
  It imports nothing from the package either: what it knows about a report is what the
  scorer already saved.
* It has its own version, `QUALITY_VERSION`, stamped into every `quality.json`.

A rescore is a pure function of the saved episode: no clock, no host name, no random
order. Scoring the same episode twice writes the same bytes.

    python -m qualgentbench.report_quality <run dir>

walks every episode under `<run dir>` and writes one `quality.json` per episode.
See docs/report-quality.md for the file format and the version rule.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The version of the quality rules, stamped into every `quality.json`. Bump it when a
#: change can alter the `quality.json` of an episode that a published board already
#: scored: a gate's rule, a gate added or removed, a change to who is counted. A change
#: that cannot alter any output (a refactor, a comment) keeps the version.
#:
#:   1  the frame, with gate G1 (valid).
QUALITY_VERSION = 1

RESULT_FILE = "result.json"
QUALITY_FILE = "quality.json"

#: The gates a report must ALL pass to be customer-ready. A gate named here that is not
#: registered in `GATES` yet (or that did not run for a report) leaves `customer_ready`
#: unknown (null) for a report that failed nothing, never true.
HEADLINE_GATES = ("G1", "G2", "G3", "G4")


class QualityError(ValueError):
    """This directory is not an episode this module can score. The message says why."""


@dataclass(frozen=True)
class GateResult:
    """One gate's answer for one report. `passed` None means the gate did not run for
    this report (for example a gate that only runs on valid reports); `reason` then says
    why. A gate that did not run is in no pass rate."""
    passed: bool | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"pass": self.passed, "reason": self.reason}


@dataclass(frozen=True)
class Episode:
    """What a gate may read: the episode directory and its saved `result.json`."""
    dir: Path
    result: dict[str, Any]

    @property
    def metrics(self) -> dict[str, Any]:
        return self.result.get("metrics") or {}


#: A gate: (episode, the report as `result.json` saved it, its index) -> GateResult.
GateFn = Callable[[Episode, dict[str, Any], int], GateResult]


@dataclass(frozen=True)
class Gate:
    id: str            # "G1", "G2", ... the key in a report's `gates` map
    name: str          # one word, for tables: "valid", "located", ...
    fn: GateFn


def is_neutral(report: dict[str, Any]) -> bool:
    """A neutral report is neither a find nor a false report: it matched no seeded defect
    and the scorer marked it a known clean-build behaviour or a downstream effect of a
    found defect. Neutral reports are listed in `quality.json` but pass through no gate
    and are in no total, the same way the scorer leaves them out of every rate."""
    return report.get("matched") is None and (
        report.get("known") is not None or report.get("consequence_of") is not None)


def gate_valid(episode: Episode, report: dict[str, Any], index: int) -> GateResult:
    """G1, valid: the scorer matched this report to a seeded defect. Nothing is decided
    here; the answer is the `matched` field the scorer saved."""
    matched = report.get("matched")
    if matched:
        return GateResult(True, f"matched seeded defect {matched}")
    return GateResult(False, "matched no seeded defect")


#: Every gate, in the order it is evaluated and listed. Later tickets append here.
GATES: tuple[Gate, ...] = (
    Gate("G1", "valid", gate_valid),
)


def _customer_ready(gates: dict[str, dict[str, Any]]) -> bool | None:
    """False as soon as one headline gate failed; true only when every headline gate ran
    and passed; otherwise unknown."""
    answers = [(gates.get(g) or {}).get("pass") for g in HEADLINE_GATES]
    if any(a is False for a in answers):
        return False
    if all(a is True for a in answers):
        return True
    return None


def _score_report(episode: Episode, report: dict[str, Any], index: int) -> dict[str, Any]:
    neutral = is_neutral(report)
    gates: dict[str, dict[str, Any]] = {}
    if not neutral:
        for gate in GATES:
            gates[gate.id] = gate.fn(episode, report, index).as_dict()
    return {
        "index": index,
        "step": report.get("step"),
        "matched": report.get("matched"),
        "known": report.get("known"),
        "consequence_of": report.get("consequence_of"),
        "neutral": neutral,
        "gates": gates,
        "customer_ready": None if neutral else _customer_ready(gates),
    }


def _totals(reports: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in reports if not r["neutral"]]
    gates = {}
    for gate in GATES:
        answers = [r["gates"][gate.id]["pass"] for r in scored]
        gates[gate.id] = {"name": gate.name,
                          "evaluated": sum(1 for a in answers if a is not None),
                          "passed": sum(1 for a in answers if a is True)}
    return {
        "reports": len(reports),
        "neutral": len(reports) - len(scored),
        "scored": len(scored),
        "gates": gates,
        "customer_ready": sum(1 for r in scored if r["customer_ready"] is True),
        "customer_ready_unknown": sum(1 for r in scored if r["customer_ready"] is None),
    }


def load_episode(episode_dir: Path | str) -> Episode:
    """Read an episode's `result.json`. Raises `QualityError` when the directory holds no
    readable result, or a result with no journey report list (a hunt or guided episode,
    or a verdict saved before reports were recorded)."""
    episode_dir = Path(episode_dir)
    path = episode_dir / RESULT_FILE
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise QualityError(f"no {RESULT_FILE} in {episode_dir}") from None
    except (OSError, ValueError) as exc:
        raise QualityError(f"unreadable {path}: {exc}") from None
    if not isinstance(result, dict) or not isinstance(result.get("metrics"), dict):
        raise QualityError(f"{path} is not an episode result")
    reports = result["metrics"].get("reports")
    if not isinstance(reports, list) or not all(isinstance(r, dict) for r in reports):
        raise QualityError(f"{path} has no journey report list")
    return Episode(episode_dir, result)


def score_episode(episode_dir: Path | str) -> dict[str, Any]:
    """The quality block of one saved journey episode, as `quality.json` holds it. Reads
    only; `write_quality` saves it. Raises `QualityError` for a directory that is not a
    scorable journey episode."""
    episode = load_episode(episode_dir)
    metrics = episode.metrics
    reports = [_score_report(episode, r, i) for i, r in enumerate(metrics["reports"])]
    return {
        "quality_version": QUALITY_VERSION,
        # The scorer whose `matched` / neutral marks the gates read (None on a verdict
        # saved before the scorer stamped its version).
        "scorer_version": metrics.get("scorer_version"),
        "report_source": metrics.get("report_source") or "",
        "reports": reports,
        "totals": _totals(reports),
    }


def render(quality: dict[str, Any]) -> str:
    """The exact text of `quality.json`: sorted keys, two-space indent, one final
    newline. One function, so every writer produces the same bytes."""
    return json.dumps(quality, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_quality(episode_dir: Path | str) -> Path:
    """Score one episode and write `quality.json` beside its `result.json`, through a
    temp file and `os.replace` so a reader never sees half a file. Touches no other
    file. Returns the path written."""
    episode_dir = Path(episode_dir)
    text = render(score_episode(episode_dir))
    path = episode_dir / QUALITY_FILE
    tmp = episode_dir / f".{QUALITY_FILE}.{os.getpid()}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def episode_dirs(root: Path | str) -> Iterator[Path]:
    """Every episode directory at or under `root`, sorted: a directory that holds a
    `result.json`. Nothing inside an episode is searched, so a `result.json` an agent
    left in its workspace is never taken for an episode."""
    root = Path(root)
    if (root / RESULT_FILE).is_file():
        yield root
        return
    for child in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
        yield from episode_dirs(child)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m qualgentbench.report_quality",
        description="Write quality.json beside result.json for every journey episode "
                    "under a run directory. Reads saved episodes only; never changes a "
                    "score.")
    ap.add_argument("run_dir", type=Path,
                    help="a runs directory, one run's directory, or one episode directory")
    args = ap.parse_args(argv)
    if not args.run_dir.is_dir():
        print(f"error: {args.run_dir} is not a directory", file=sys.stderr)
        return 2
    written = skipped = 0
    for ep in episode_dirs(args.run_dir):
        try:
            write_quality(ep)
            written += 1
        except QualityError as exc:
            skipped += 1
            print(f"skipped: {exc}", file=sys.stderr)
    print(f"quality v{QUALITY_VERSION}: wrote {written} {QUALITY_FILE}, skipped {skipped}")
    if written == 0:
        print(f"error: no journey episode found under {args.run_dir}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
