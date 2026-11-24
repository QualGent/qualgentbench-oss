#!/usr/bin/env python3
"""Draw the report sample people label to check the report-quality gates (QUA-3594).

The report-quality gates (docs/report-quality.md) are rules. Whether a rule agrees with
a person is measured on a label set: two people read the same customer-shaped reports,
blind to the gates and to each other, and answer whether each is a real bug, whether
they could reproduce it from the text, and whether they would send it as written. This
script draws that set, `rq-v1`, from saved journey episodes. No agent, no device, no
model.

    uv run python -m qualgentbench.report_quality <run dir>             # writes quality.json
    uv run python scripts/quality_label_sample.py <run dir> [<run dir> ...] --out <dir>

It reads each episode's `result.json` and `quality.json` and writes two files to `--out`:

    rq-v1-label-sample.json       for the labellers: report text and the app's name
    rq-v1-label-sample-key.json   NOT for the labellers: which episode each item is

The sample carries nothing a labeller must not see: no agent, model, run, episode, case
or defect id, no gate result, no build. `validate_sample` refuses any key outside the
labelling page's own whitelist, and the script runs it on what it is about to write.
The key file joins a label back to its report: `item_id` -> board, episode directory and
report index, which is the report at `reports[report_index]` of that episode's
`quality.json`.

What is drawn (defaults: 120 reports, about 20 from clean builds, 40 with step labels):

* The pool is every report of every scored episode that is not neutral (the gates do
  not run on a neutral report). Two reports of one app with the same text are one
  candidate; the key lists the others under `same_text`.
* Strata. A report on a clean build is in stratum `clean`. A report on a seeded build
  is in the stratum of the headline gates it FAILED: `pass` when it failed none,
  `fail:G1`, `fail:G1+G3`, ... A gate that did not run for a report is not a failure.
  A gate that ran and answered unknown is named beside them (`unknown:G2`,
  `fail:G3,unknown:G2`): such a report is kept apart from both passes and failures.
* The seeded strata share the reports left after the clean ones equally; a stratum with
  fewer candidates gives its unused share to the others, so failures of every gate are
  present as far as the boards have them.
* Inside a stratum, reports are taken one app at a time, so no app fills a stratum.
* The step subset is chosen first. A report can carry step labels when it came from a
  report tool call with a step list, the scorer matched it to a seeded defect, and the
  case file has that defect's reference report (it names the case step at which the
  defect shows). Such an item gets `step_items` (the agent's step list, one string per
  step) and `reference_steps` (the case's own steps up to that step).
* Every choice is ordered by a SHA-256 of the seed and the report's place (board,
  episode, index), and the items are written in such an order too. The same seed and
  the same episodes give the same bytes, on any machine and in any directory order.

Held-out apps: an episode of a held-out app puts that app's report text, and for the
step subset its case steps, into the output. The script refuses to write inside this
repository; give `--out` a private directory, or pass `--exclude-heldout`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import yaml

from qualgentbench import corpus, journey, reference_reports
from qualgentbench import report_quality as rq

REPO_ROOT = Path(__file__).resolve().parents[1]

# ── the sample file, as the labelling page reads it ────────────────────────────

SET_ID = "rq-v1"
SAMPLE_FORMAT = "rq-label-sample"
KEY_FORMAT = "rq-label-sample-key"
FORMAT_VERSION = 1
SOURCE = "qualgentbench"

SAMPLE_FILE = f"{SET_ID}-label-sample.json"
KEY_FILE = f"{SET_ID}-label-sample-key.json"

#: Every key the labelling page accepts, per level. Any other key is refused there.
SAMPLE_KEYS = ("format", "version", "set_id", "source", "items")
ITEM_KEYS = ("item_id", "app", "report", "step_items", "reference_steps")
REPORT_KEYS = ("summary", "steps", "expected", "actual", "failure_step", "paired_by")

ITEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{3,63}$")
MAX_ITEMS = 500
MAX_TEXT = 20_000          # per text field, in UTF-16 code units (the page's `length`)
MAX_STEPS = 60

DEFAULT_SEED = 3594
DEFAULT_TOTAL = 120
DEFAULT_CLEAN = 20
DEFAULT_STEPS = 40

STRATUM_CLEAN = "clean"
STRATUM_PASS = "pass"


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and len(value.encode("utf-16-le")) // 2 <= MAX_TEXT


def _text_list(value: Any) -> bool:
    return (isinstance(value, list) and 1 <= len(value) <= MAX_STEPS
            and all(_is_text(v) and v.strip() for v in value))


def validate_sample(doc: Any) -> list[str]:
    """Everything that would make the labelling page refuse `doc` as a sample file, or
    []. The same rules as the page's parser: a fixed key whitelist at every level, the
    header values, the item id pattern, unique ids, and the text and list limits."""
    if not isinstance(doc, dict):
        return ["sample: not a JSON object"]
    found: list[str] = []
    for key, want in (("format", SAMPLE_FORMAT), ("version", FORMAT_VERSION),
                      ("set_id", SET_ID), ("source", SOURCE)):
        if doc.get(key) != want or isinstance(doc.get(key), bool):
            found.append(f"sample: {key} must be {want!r}")
    found += [f"sample: key {k!r} is not allowed" for k in doc if k not in SAMPLE_KEYS]
    items = doc.get("items")
    if not isinstance(items, list) or not items:
        return [*found, "sample: items must be a non-empty list"]
    if len(items) > MAX_ITEMS:
        found.append(f"sample: more than {MAX_ITEMS} items")
    seen: set[str] = set()
    for i, item in enumerate(items):
        at = f"sample item {i}"
        if not isinstance(item, dict):
            found.append(f"{at}: not an object")
            continue
        found += [f"{at}: key {k!r} is not allowed" for k in item if k not in ITEM_KEYS]
        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not ITEM_ID_RE.match(item_id):
            found.append(f"{at}: item_id must match {ITEM_ID_RE.pattern}")
        elif item_id in seen:
            found.append(f"{at}: item_id {item_id} is repeated")
        else:
            seen.add(item_id)
        if not _is_text(item.get("app")) or not item["app"].strip():
            found.append(f"{at}: app must be the app's name")
        report = item.get("report")
        if not isinstance(report, dict):
            found.append(f"{at}: report must be an object")
        else:
            found += [f"{at}: report key {k!r} is not allowed"
                      for k in report if k not in REPORT_KEYS]
            if not _is_text(report.get("summary")) or not report["summary"].strip():
                found.append(f"{at}: report.summary must be text")
            if not _is_text(report.get("steps")):
                found.append(f"{at}: report.steps must be text (it may be empty)")
            for k in ("expected", "actual", "failure_step", "paired_by"):
                if report.get(k) is not None and not _is_text(report[k]):
                    found.append(f"{at}: report.{k} must be text or null")
        steps, reference = item.get("step_items"), item.get("reference_steps")
        if steps is not None and not _text_list(steps):
            found.append(f"{at}: step_items must be 1 to {MAX_STEPS} non-empty strings")
        if reference is not None:
            if steps is None:
                found.append(f"{at}: reference_steps needs step_items")
            elif not _text_list(reference):
                found.append(f"{at}: reference_steps must be 1 to {MAX_STEPS} "
                             "non-empty strings")
    return found


# ── the pool ───────────────────────────────────────────────────────────────────

#: (app id, case id, defect id) -> the case's steps up to the defect, or None.
ReferenceLookup = Callable[[str, str, str], list[str] | None]
#: app id -> the name a labeller sees.
AppNames = Callable[[str], str]


@dataclass
class Candidate:
    """One report that can be drawn."""
    board: str                 # the name of the run directory it was read from
    episode: str               # the episode directory, relative to the board, with "/"
    index: int                 # position in the episode's `metrics.reports`
    app_id: str
    case_id: str
    build: str                 # "clean" or "seeded"
    heldout: bool
    matched: str | None
    stratum: str
    report: dict[str, Any]     # the sample's `report` object
    step_items: list[str] | None = None        # set when it can carry step labels
    reference_steps: list[str] | None = None
    same_text: list[str] = field(default_factory=list)

    @property
    def place(self) -> str:
        return f"{self.board}/{self.episode}#{self.index}"


def stratum_of(build: str, gates: dict[str, Any], *, name_not_run: bool = False) -> str:
    """`clean` for a clean build; else the headline gates the report failed, as
    `fail:G1+G3`, or `pass` when it failed none. A gate that ran and could not decide
    (`unknown: true`) is neither a pass nor a failure, so it is named on its own:
    `unknown:G2`, `fail:G3,unknown:G2`. A gate that did not run (`pass` null without
    `unknown`, or absent from this quality version) is not named at all, so `pass`
    holds both "passed G4" and "G4 never ran".

    `name_not_run` (QUA-3607, off by default: `rq-v1` was drawn without it) names such a
    gate for a report that failed nothing, as `not_run:G4`: `pass` is then only a report
    that passed every headline gate. A report that failed a gate keeps its stratum,
    because no later gate was owed to it."""
    if build != "seeded":
        return STRATUM_CLEAN
    answers = {g: gates.get(g) or {} for g in rq.HEADLINE_GATES}
    failed = [g for g, a in answers.items() if a.get("pass") is False]
    unknown = [g for g, a in answers.items() if a.get("pass") is None and a.get("unknown")]
    not_run = [g for g, a in answers.items()
               if a.get("pass") is None and not a.get("unknown")]
    parts = [f"fail:{'+'.join(failed)}"] if failed else []
    if unknown:
        parts.append(f"unknown:{'+'.join(unknown)}")
    if name_not_run and not_run and not failed:
        parts.append(f"not_run:{'+'.join(not_run)}")
    return ",".join(parts) or STRATUM_PASS


def render_step(step: Any) -> str:
    """One entry of the agent's step list as one line of text. A string keeps its words
    (whitespace collapsed); anything else is shown as compact JSON with sorted keys.
    The line is sanitised as every other field of the customer-shaped report is
    (`report_quality.sanitize_text`: a credential after `Bearer`, the value of
    `password: ...`), so a secret an agent typed does not reach a labeller (QUA-3607).
    No step of the drawn `rq-v1` sample holds such text; its bytes are unchanged."""
    if not isinstance(step, str):
        step = json.dumps(step, sort_keys=True, ensure_ascii=False, separators=(", ", ": "))
    return rq.sanitize_text(" ".join(step.split()))


def render_steps(agent_steps: Any) -> list[str]:
    """The agent's step list as non-empty strings, in order."""
    if not isinstance(agent_steps, list):
        return []
    return [s for s in (render_step(a) for a in agent_steps) if s]


class CorpusLookup:
    """Case steps and app names from the case files (held-out ones through
    `QGB_HELDOUT_DIR`), read once per app."""

    def __init__(self) -> None:
        self._cases: dict[str, dict[str, dict]] = {}
        self._names: dict[str, str] = {}

    def _app_cases(self, app_id: str) -> dict[str, dict]:
        if app_id not in self._cases:
            try:
                doc = journey.load_cases(app_id) or {}
            except (OSError, yaml.YAMLError):
                doc = {}
            self._cases[app_id] = {str(c.get("id")): c for c in doc.get("test_cases") or []
                                   if isinstance(c, dict)}
        return self._cases[app_id]

    def reference_steps(self, app_id: str, case_id: str, defect_id: str) -> list[str] | None:
        """The case's own steps from step 1 to the step the defect's reference report
        names, or None when the case, the reference report or that step is not there."""
        case = self._app_cases(app_id).get(case_id)
        if case is None:
            return None
        try:
            ref = reference_reports.case_references(case, app_id).get(defect_id)
        except ValueError:
            return None
        steps = case.get("steps") or []
        if ref is None or ref.step_number is None or not 1 <= ref.step_number <= len(steps):
            return None
        upto = [" ".join(s.split()) for s in steps[:ref.step_number] if isinstance(s, str)]
        return upto if len(upto) == ref.step_number and all(upto) else None

    def app_name(self, app_id: str) -> str:
        if app_id not in self._names:
            name = ""
            try:
                spec = yaml.safe_load(corpus.spec_path(app_id).read_text(encoding="utf-8"))
                name = str(((spec or {}).get("app") or {}).get("name") or "")
            except (OSError, yaml.YAMLError, AttributeError):
                pass
            self._names[app_id] = name.strip() or app_id
        return self._names[app_id]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _report_fields(customer: dict[str, Any]) -> dict[str, Any] | None:
    """The sample's `report` object from a `quality.json` customer report, or None when
    the page would refuse it (no summary, or a field longer than the page allows)."""
    summary, steps = customer.get("summary"), customer.get("steps")
    if not _is_text(summary) or not summary.strip() or not _is_text(steps):
        return None
    out: dict[str, Any] = {"summary": summary, "steps": steps}
    for k in ("expected", "actual", "failure_step"):
        v = customer.get(k)
        if v is not None and not _is_text(v):
            return None
        out[k] = v
    if isinstance(customer.get("paired_by"), str):
        out["paired_by"] = customer["paired_by"]
    return out


def collect(boards: Iterable[tuple[str, Path]], *, reference: ReferenceLookup,
            exclude_heldout: bool = False, name_not_run: bool = False
            ) -> tuple[list[Candidate], dict[str, Any]]:
    """Every report that can be drawn from `boards` ((name, run directory) pairs), and
    what was read and left out. Reports of one app with the same text are one candidate:
    the one whose place sorts first, with the others in `same_text`."""
    stats: Counter[str] = Counter()
    versions: set[Any] = set()
    by_text: dict[tuple, Candidate] = {}
    for board, root in sorted(boards):
        for ep in rq.episode_dirs(root):
            result, quality = _read_json(ep / rq.RESULT_FILE), _read_json(ep / rq.QUALITY_FILE)
            metrics = result.get("metrics") if isinstance(result, dict) else None
            if not isinstance(metrics, dict) or not isinstance(metrics.get("reports"), list):
                stats["episodes_not_journey"] += 1
                continue
            if not isinstance(quality, dict) or not isinstance(quality.get("reports"), list):
                stats["episodes_without_quality"] += 1
                continue
            heldout = bool(metrics.get("heldout"))
            if heldout and exclude_heldout:
                stats["episodes_heldout_excluded"] += 1
                continue
            stats["episodes"] += 1
            versions.add(quality.get("quality_version"))
            app_id, case_id = str(metrics.get("app_id") or ""), str(metrics.get("case_id") or "")
            build = str(metrics.get("version") or "")
            episode = ep.relative_to(root).as_posix()
            for entry in quality["reports"]:
                index = entry.get("index") if isinstance(entry, dict) else None
                if not isinstance(index, int) or isinstance(index, bool):
                    stats["reports_unreadable"] += 1
                    continue
                stats["reports"] += 1
                if entry.get("neutral"):
                    stats["reports_neutral"] += 1
                    continue
                customer = entry.get("customer_report")
                report = _report_fields(customer) if isinstance(customer, dict) else None
                if report is None or not app_id or build not in ("clean", "seeded"):
                    stats["reports_unusable"] += 1
                    continue
                matched = entry.get("matched") if isinstance(entry.get("matched"), str) else None
                cand = Candidate(
                    board=board, episode=episode, index=index, app_id=app_id,
                    case_id=case_id, build=build, heldout=heldout, matched=matched,
                    stratum=stratum_of(build, entry.get("gates") or {},
                                       name_not_run=name_not_run), report=report)
                steps = render_steps(customer.get("agent_steps"))
                if (build == "seeded" and matched and _text_list(steps)
                        and customer.get("report_source") == rq.SOURCE_REPORT_TOOL):
                    upto = reference(app_id, case_id, matched)
                    if upto and _text_list(upto):
                        cand.step_items, cand.reference_steps = steps, upto
                    else:
                        stats["step_lists_without_reference"] += 1
                text = (app_id, cand.stratum,
                        *(report.get(k) for k in REPORT_KEYS[:5]))
                first = by_text.get(text)
                if first is None:
                    by_text[text] = cand
                    continue
                stats["reports_same_text"] += 1
                keep, drop = sorted((first, cand), key=lambda c: c.place)
                # Step labels belong to the text, so they survive whichever copy is kept.
                if keep.step_items is None and drop.step_items is not None:
                    keep.step_items, keep.reference_steps = drop.step_items, drop.reference_steps
                keep.same_text = sorted({*first.same_text, *cand.same_text, drop.place})
                by_text[text] = keep
    pool = sorted(by_text.values(), key=lambda c: c.place)
    info = {"read": dict(sorted(stats.items())),
            "quality_versions": sorted(versions, key=str)}
    return pool, info


# ── drawing ────────────────────────────────────────────────────────────────────

def _rank(seed: int, purpose: str, value: str) -> str:
    """A fixed pseudo-random position for `value`: the same on every machine and Python
    version, and unchanged for one report when other reports come or go."""
    return hashlib.sha256(f"{seed}\x1f{purpose}\x1f{value}".encode()).hexdigest()


def fill(sizes: dict[str, int], total: int) -> dict[str, int]:
    """Share `total` equally between the groups in `sizes`, no group taking more than it
    has; what a small group cannot take goes to the others. A remainder goes to groups
    in name order."""
    quota = dict.fromkeys(sizes, 0)
    left = max(total, 0)
    while left > 0:
        open_groups = sorted(g for g in sizes if quota[g] < sizes[g])
        if not open_groups:
            break
        share = max(left // len(open_groups), 1)
        for g in open_groups:
            take = min(share, sizes[g] - quota[g], left)
            quota[g] += take
            left -= take
            if left == 0:
                break
    return quota


def spread(cands: list[Candidate], n: int, seed: int, purpose: str,
           taken: Counter[str]) -> list[Candidate]:
    """Take `n` of `cands` one app at a time: each pick goes to the app with the fewest
    picks so far in `taken` (updated here), apps and reports in their fixed order."""
    queues: dict[str, list[Candidate]] = {}
    for c in sorted(cands, key=lambda c: _rank(seed, purpose, c.place)):
        queues.setdefault(c.app_id, []).append(c)
    out: list[Candidate] = []
    while len(out) < n and queues:
        app = min(queues, key=lambda a: (taken[a], _rank(seed, purpose + "/app", a)))
        out.append(queues[app].pop(0))
        taken[app] += 1
        if not queues[app]:
            del queues[app]
    return out


def draw(pool: list[Candidate], *, seed: int = DEFAULT_SEED, total: int = DEFAULT_TOTAL,
         clean: int = DEFAULT_CLEAN, steps: int = DEFAULT_STEPS
         ) -> tuple[list[Candidate], set[str]]:
    """The reports to label, in the order the labellers get them, and the places of the
    ones that carry step labels."""
    by_stratum: dict[str, list[Candidate]] = {}
    for c in pool:
        by_stratum.setdefault(c.stratum, []).append(c)
    n_clean_pool = len(by_stratum.get(STRATUM_CLEAN, []))
    seeded_sizes = {s: len(v) for s, v in by_stratum.items() if s != STRATUM_CLEAN}
    quota = fill(seeded_sizes, total - min(clean, n_clean_pool))
    # Seeded strata that cannot fill their share leave the rest to clean reports.
    quota[STRATUM_CLEAN] = min(n_clean_pool, max(total - sum(quota.values()), 0))

    step_sizes = {s: min(quota[s], sum(1 for c in v if c.step_items is not None))
                  for s, v in by_stratum.items()}
    step_quota = fill(step_sizes, steps)

    chosen: list[Candidate] = []
    with_steps: set[str] = set()
    for stratum in sorted(by_stratum):
        cands, taken = by_stratum[stratum], Counter()
        first = spread([c for c in cands if c.step_items is not None],
                       step_quota[stratum], seed, f"steps/{stratum}", taken)
        with_steps.update(c.place for c in first)
        rest = spread([c for c in cands if c.place not in with_steps],
                      quota[stratum] - len(first), seed, f"fill/{stratum}", taken)
        chosen += first + rest
    chosen.sort(key=lambda c: _rank(seed, "order", c.place))
    return chosen, with_steps


# ── the two files ──────────────────────────────────────────────────────────────

def render(doc: dict[str, Any]) -> str:
    """The exact text of an output file: two-space indent, keys in the order built, one
    final newline."""
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def build(boards: Iterable[tuple[str, Path]], *, seed: int = DEFAULT_SEED,
          total: int = DEFAULT_TOTAL, clean: int = DEFAULT_CLEAN, steps: int = DEFAULT_STEPS,
          exclude_heldout: bool = False, reference: ReferenceLookup | None = None,
          app_names: AppNames | None = None, name_not_run: bool = False
          ) -> tuple[dict[str, Any], dict[str, Any]]:
    """(sample, key) for `boards`. Pure but for reading the episodes and the case files:
    no clock, no host name, no absolute path in either document."""
    boards = sorted(boards)
    lookup = CorpusLookup()
    pool, info = collect(boards, reference=reference or lookup.reference_steps,
                         exclude_heldout=exclude_heldout, name_not_run=name_not_run)
    chosen, with_steps = draw(pool, seed=seed, total=total, clean=clean, steps=steps)
    name_of = app_names or lookup.app_name

    items: list[dict[str, Any]] = []
    key_items: list[dict[str, Any]] = []
    for n, c in enumerate(chosen, 1):
        item_id = f"rq-{n:04d}"
        item: dict[str, Any] = {"item_id": item_id, "app": name_of(c.app_id),
                                "report": c.report}
        in_steps = c.place in with_steps
        if in_steps:
            item["step_items"] = c.step_items
            item["reference_steps"] = c.reference_steps
        items.append(item)
        key_items.append({
            "item_id": item_id, "board": c.board, "episode": c.episode,
            "report_index": c.index, "app_id": c.app_id, "case_id": c.case_id,
            "build": c.build, "heldout": c.heldout, "matched": c.matched,
            "stratum": c.stratum, "step_subset": in_steps, "same_text": c.same_text})
    sample = {"format": SAMPLE_FORMAT, "version": FORMAT_VERSION, "set_id": SET_ID,
              "source": SOURCE, "items": items}

    def tally(cands: Iterable[Candidate], what: Callable[[Candidate], str]) -> dict[str, int]:
        return dict(sorted(Counter(what(c) for c in cands).items()))

    key = {
        "format": KEY_FORMAT, "version": FORMAT_VERSION, "set_id": SET_ID,
        "sample_sha256": hashlib.sha256(render(sample).encode("utf-8")).hexdigest(),
        "seed": seed,
        # `name_not_run` is recorded only when it was asked for, so a draw without it
        # writes the key it always wrote.
        "targets": {"total": total, "clean": clean, "step_subset": steps,
                    **({"name_not_run": True} if name_not_run else {})},
        "boards": [name for name, _ in boards],
        "quality_versions": info["quality_versions"],
        "read": info["read"],
        "pool": {"reports": len(pool),
                 "by_stratum": tally(pool, lambda c: c.stratum),
                 "step_capable_by_stratum": tally(
                     (c for c in pool if c.step_items is not None), lambda c: c.stratum)},
        "drawn": {"reports": len(chosen),
                  "clean": sum(1 for c in chosen if c.build == "clean"),
                  "step_subset": len(with_steps),
                  "heldout": sum(1 for c in chosen if c.heldout),
                  "by_stratum": tally(chosen, lambda c: c.stratum),
                  "step_subset_by_stratum": tally(
                      (c for c in chosen if c.place in with_steps), lambda c: c.stratum),
                  "by_app": tally(chosen, lambda c: c.app_id)},
        "items": key_items,
    }
    return sample, key


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Draw the rq-v1 label sample from saved, quality-scored journey "
                    "episodes. Writes the sample for the labellers and a key file that "
                    "is not for them.")
    ap.add_argument("run_dirs", type=Path, nargs="+",
                    help="run directories (boards) whose episodes hold quality.json; "
                         "each directory's name is its board name in the key file")
    ap.add_argument("--out", type=Path, required=True,
                    help="directory for the two files; must be outside this repository")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--total", type=int, default=DEFAULT_TOTAL, help="reports to draw")
    ap.add_argument("--clean", type=int, default=DEFAULT_CLEAN,
                    help="of which from clean builds")
    ap.add_argument("--steps", type=int, default=DEFAULT_STEPS,
                    help="of which carry step labels")
    ap.add_argument("--exclude-heldout", action="store_true",
                    help="leave out episodes of held-out apps")
    ap.add_argument("--name-not-run", action="store_true",
                    help="put a report that failed nothing and still waits for a gate in "
                         "its own stratum (`not_run:G4`), apart from `pass`. Off by "
                         "default: rq-v1 was drawn without it, and with it the same "
                         "episodes give other bytes")
    args = ap.parse_args(argv)

    if not 1 <= args.total <= MAX_ITEMS or args.clean < 0 or args.steps < 0:
        print(f"error: --total must be 1 to {MAX_ITEMS}; --clean and --steps not negative",
              file=sys.stderr)
        return 2
    boards = [(d.resolve().name, d) for d in args.run_dirs]
    for _, d in boards:
        if not d.is_dir():
            print(f"error: {d} is not a directory", file=sys.stderr)
            return 2
    names = [n for n, _ in boards]
    if len(set(names)) != len(names):
        print("error: two run directories have the same name; the key file tells boards "
              "apart by name, so rename one", file=sys.stderr)
        return 2
    if _inside_repo(args.out):
        print(f"error: --out {args.out} is inside this repository. A sample can hold "
              "held-out report text; write it somewhere private.", file=sys.stderr)
        return 2

    sample, key = build(boards, seed=args.seed, total=args.total, clean=args.clean,
                        steps=args.steps, exclude_heldout=args.exclude_heldout,
                        name_not_run=args.name_not_run)
    read, drawn = key["read"], key["drawn"]
    if not sample["items"]:
        print("error: no report to draw. "
              f"{read.get('episodes_without_quality', 0)} episode(s) have no quality.json: "
              "run `python -m qualgentbench.report_quality <run dir>` first.",
              file=sys.stderr)
        return 1
    if len(key["quality_versions"]) > 1:
        print(f"error: the episodes were scored under different quality versions "
              f"{key['quality_versions']}; strata would not mean one thing. Score them "
              "again with one version.", file=sys.stderr)
        return 1
    problems = validate_sample(sample)
    if problems:
        print("error: the sample would not load on the labelling page:\n  "
              + "\n  ".join(problems[:20]), file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / SAMPLE_FILE).write_text(render(sample), encoding="utf-8")
    (args.out / KEY_FILE).write_text(render(key), encoding="utf-8")

    print(f"{SET_ID}: drew {drawn['reports']} of {key['pool']['reports']} reports "
          f"(seed {args.seed}, quality v{key['quality_versions'][0]})")
    print(f"  clean builds: {drawn['clean']} (target {args.clean})   "
          f"step subset: {drawn['step_subset']} (target {args.steps})   "
          f"held-out: {drawn['heldout']}")
    print(f"  {'stratum':<24} drawn  pool  with steps")
    for s, n in key["pool"]["by_stratum"].items():
        print(f"  {s:<24} {drawn['by_stratum'].get(s, 0):>5} {n:>5} "
              f"{drawn['step_subset_by_stratum'].get(s, 0):>11}")
    print(f"  apps: {len(drawn['by_app'])}")
    for what, n in read.items():
        if what not in ("episodes", "reports"):
            print(f"  left out: {what} = {n}")
    for what, want, got in (("reports", args.total, drawn["reports"]),
                            ("clean-build reports", args.clean, drawn["clean"]),
                            ("step-label reports", args.steps, drawn["step_subset"])):
        if got < want:
            print(f"warning: {got} {what}, target {want}: the boards do not hold more",
                  file=sys.stderr)
    print(f"sample (send to labellers): {args.out / SAMPLE_FILE}")
    print(f"  sha256 {key['sample_sha256']}")
    print(f"key (keep private, never send): {args.out / KEY_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
