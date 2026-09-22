#!/usr/bin/env python3
"""Confirm an app's journey test cases by execution — the corpus gate, run once per
app (and again only when a case, a defect or the APK changes).

For every case in data/test-cases/<app>.yaml the harness-only `check:` runs twice:
  1. clean   — no defect on. The case must PASS: this is the clean version every
               agent runs, and a clean version that cannot pass fails every agent.
  2. seeded  — exactly the case's `bugs:` on. The measured verdict must match what
               the list implies (a functional bug → FAIL, display bugs only → PASS).
After every step the screen text is dumped; the difference between the two runs is
what the bugs changed on this route. Each display bug's `marker` must be in that
difference — otherwise it is not visible on the route and the case is wrong, not the
agent. The strings found are recorded and become the matcher's first signal.
A case expected to FAIL whose seeded version DIES with that difference EMPTY is refused
too: its defect is invisible on the route (`invisible_death`). So is a case whose screen
witness is already complete before the step the case tests (`witness_credited_early`):
completion would be credited to an agent that never performed the action. The five cases
that carry that weakness today say so in their own YAML (`witness_before_action:`).

Output: data/truth/journey-<app>.json (or --json). Every DISAGREE printed means the
case, the seeding or the marker is wrong — fix the YAML, never the JSON.
The APK is assumed installed (`adb install -r -g dist/<app>/buggy.apk`).

`--repeat N` runs each version N times (a fresh reset per trial) and demands the same
outcome every time. One trial cannot measure a margin: a forced interleaving, a crash or
a stuck-screen oracle is only a defect if it reproduces on every reset, so any such case
must be derived with --repeat >= 3 before it enters the corpus. There is no majority
vote — a version whose trials disagree is UNSTABLE, gets a `problems` entry and
`agrees: false`, and leaves the corpus rather than being averaged into it.

A trial that comes back INCONCLUSIVE is RETRIED inside the trial (`one_pass`), which is
right — an unresolved anchor is the replayer's problem, not the case's — but a retry that
leaves no trace makes a quietly flaky case read as a clean pass. Every trial therefore
records how many attempts it took (`attempts`, with the discarded attempts' own verdicts
under `retries`), on screen while the derive runs and in the row afterwards. The stored
screens and outcome are always the LAST attempt's, so `attempts: 2` says the row was
written by attempt 2. Both keys are written only when there WAS a retry: absent means
nothing was masked, so a clean derive's row is byte-identical to the rows already in the
corpus, and no reader may require them (QUA-2744). This is how the rotation leak
(QUA-2734) stayed invisible for a day: a case kept going INCONCLUSIVE, the retries agreed
because the re-pin happened to land, and the truth looked fine."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shlex
import sys
import time
from collections import Counter
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from qualgentbench import corpus
from qualgentbench import journey, replay as rp, truth             # noqa: E402
from qualgentbench.bugs import load_suite                          # noqa: E402
from qualgentbench.episode_runner import run_device_setup          # noqa: E402
from qualgentbench.submission import Claim, _parse_expect          # noqa: E402
from qualgentbench.verify.device import (_adb, append_text, dump_stats,   # noqa: E402
                                         dump_stats_since, dump_vh,
                                         grant_requested_permissions, ime_shown,
                                         relaunch, reset_dump_source, wait_stable)
from qualgentbench.verify.match import visible_texts               # noqa: E402

ROOT = Path(__file__).parents[1]

_TEXT_LIMIT = 600

# Wall-clock stamps differ between ANY two runs (a saved entry carries the minute it
# was saved), so they can never be a symptom. Masked before diffing: a DATE, and a
# time only when it follows a date ("Sep 2, 2026 2:49 PM"). A standalone time stays —
# a reminder set for 8:00 AM that renders as 9:00 AM IS a symptom (MedTimer). Every
# other number stays too: "Avg: 76 kg" vs "Avg: 79 kg" is the signal.
_DATE_RE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:, \d{4})?"
                      r"(?:,? \d{1,2}:\d{2}(?::\d{2})?\s?(?:[AaPp][Mm])?)?\b")
_NUMDATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}(?:,? \d{1,2}:\d{2}(?::\d{2})?\s?(?:[AaPp][Mm])?)?\b")


def mask(text: str) -> str:
    return _NUMDATE_RE.sub("<date>", _DATE_RE.sub("<date>", text))


async def run_with_dumps(serial: str, bundle: str, steps) -> tuple[rp.ReplayResult, list[list[str]]]:
    """The replay executor's step loop, recording the screen after every step.

    The device sees EXACTLY the calls replay.run_steps makes: a tap step's screen is
    taken from the hierarchy dump the NEXT tap fetches for its own anchor lookup, not
    from an extra dump of our own (an extra dump between two taps once dismissed a
    popup menu and the second tap landed on a card label). Only a step followed by a
    non-tap step (type, press, swipe, wait) or the last step gets an explicit dump."""
    dumps: list[list[str]] = []
    pending = False
    real_dump = rp.dump_vh

    async def spy(serial_: str, retries: int = 3) -> str:
        nonlocal pending
        xml = await real_dump(serial_, retries)
        if pending and xml:
            dumps.append(visible_texts(xml, limit=_TEXT_LIMIT))
            pending = False
        return xml

    async def record_now() -> None:
        nonlocal pending
        if pending:
            xml = await real_dump(serial)
            dumps.append(visible_texts(xml, limit=_TEXT_LIMIT) if xml else [])
            pending = False

    rp.dump_vh = spy
    ran = 0
    # Same crash window as replay.run_steps: opened before the launch step, consulted
    # on every failure path and once at the end, so a crash-seeded case measures
    # CRASHED (-> FAIL) here exactly as it would in episode verification.
    since = await rp.crash_window(serial)
    try:
        for index, step in enumerate(steps):
            try:
                if step.action not in ("tap", "long_press"):
                    await record_now()
                if step.action == "launch" or (step.action == "relaunch" and index == 0):
                    # replay's own `launch` step: cold start + re-pin portrait with the
                    # app in front (QUA-2734). Shared, not copied, so the two executors
                    # cannot disagree about the orientation a pass starts in. A route that
                    # OPENS with `relaunch` starts upright too, as in run_steps (QUA-2738).
                    await rp._launch(serial, bundle)
                elif step.action == "relaunch":
                    await relaunch(serial, bundle)
                elif step.action == "wait":
                    await wait_stable(serial)
                elif step.action in ("tap", "long_press"):
                    hold = 900 if step.action == "long_press" else 0
                    # `row` (a route's `{tap: X, row: Y}`) scopes the anchor to one list
                    # row exactly as replay.run_steps does — same resolver, same scope.
                    tapped, _tied, _c = await rp._tap_any(serial, step.value, hold_ms=hold,
                                                          row=step.row)
                    if not tapped and step.value.strip().lower() not in rp._DISMISS_LABELS:
                        if await rp._dismiss_overlays(serial, rounds=1):
                            await wait_stable(serial)
                            tapped, _tied, _c = await rp._tap_any(serial, step.value, hold_ms=hold,
                                                                  row=step.row)
                    if not tapped:
                        return await rp.crash_verdict(serial, bundle, since, rp.ReplayResult(
                            rp.INCONCLUSIVE,
                            f"step {ran + 1}: no element matching {rp._anchor_desc(step.value, step.row)}",
                            ran)), dumps
                elif step.action == "type":
                    await rp._type_text(serial, step.value)
                elif step.action == "append":
                    await append_text(serial, step.value)
                elif step.action == "press":
                    if (step.value.strip().lower() == "back" and index > 0
                            and steps[index - 1].action == "type" and not await ime_shown(serial)):
                        pass
                    else:
                        await rp._press(serial, step.value)
                elif step.action == "swipe":
                    await rp._swipe(serial, step.value)
                elif step.action == "rotate":
                    await rp._rotate(serial, step.value)
                else:
                    return rp.ReplayResult(rp.INCONCLUSIVE, f"unknown action {step.action}", ran), dumps
                ran += 1
                await asyncio.sleep(rp._SETTLE_S)
                pending = True
            except Exception as exc:  # noqa: BLE001
                return await rp.crash_verdict(serial, bundle, since, rp.ReplayResult(
                    rp.INCONCLUSIVE, f"step {ran + 1}: {exc}", ran)), dumps
        await wait_stable(serial)
        await record_now()
        return await rp.crash_verdict(serial, bundle, since,
                                      rp.ReplayResult(rp.HOLDS, "", ran)), dumps
    finally:
        rp.dump_vh = real_dump


async def evaluate(serial: str, bundle: str, expect, ran: int, since: str = "") -> rp.ReplayResult:
    """The post-condition on a route that ran with the app alive — the same order as
    `replay.replay`: the `stuck:` probe first (it needs the app up; the db read below
    force-stops it), then a standalone liveness mode HOLDS, then the state oracle."""
    if expect.stuck:
        probe = await rp._check_stuck(serial, bundle, expect, ran, since or await rp.crash_window(serial))
        if probe.outcome != rp.HOLDS or expect.mode == "stuck":
            return probe
    if expect.mode in ("crash", "anr"):
        return rp.ReplayResult(rp.HOLDS, "the app is alive after the route", ran)
    if expect.mode == "db":
        # Same cold read as the episode runner: a running AnkiDroid locks its collection.
        await rp._adb(serial, "shell", "am", "force-stop", bundle)
        await asyncio.sleep(1.0)
        return await rp._check_db(serial, bundle, expect, ran)
    if expect.mode == "file":
        return await rp._check_file(serial, bundle, expect, ran)
    if expect.mode == "content":
        return await rp._check_content(serial, bundle, expect, ran)
    xml = await dump_vh(serial)
    if not xml:
        return rp.ReplayResult(rp.INCONCLUSIVE, "could not read the UI hierarchy", ran)
    found = rp._present(xml, expect.text)
    holds = found if expect.mode == "present" else not found
    return rp.ReplayResult(rp.HOLDS if holds else rp.VIOLATED,
                           f"{expect.mode} {expect.text!r} → {'yes' if found else 'no'}", ran)


async def one_pass(serial, bundle, claim: Claim, flags, snap, shared, shared_snap,
                   device_setup, attempts: int = 2):
    """ONE trial: up to `attempts` runs of the route, each from a fresh reset, stopping
    at the first that is not INCONCLUSIVE.

    Returns `(result, screens, log)`. `log` carries EVERY attempt's verdict in order and
    its LAST entry IS `result`, so `len(log)` is the attempt count and `log[:-1]` is what
    the retry masked. Only the winning attempt's screens are kept: a discarded attempt is
    the same route on the same build, a screen list is most of a truth row's bytes, and
    an unresolved anchor is described by its own detail string rather than by its screens.
    The retry itself is deliberate — INCONCLUSIVE means the replayer could not JUDGE the
    pass, not that the case failed — but it must not be silent (QUA-2744)."""
    log: list[rp.ReplayResult] = []
    res, dumps = rp.ReplayResult(rp.INCONCLUSIVE, "not run"), []
    for _ in range(attempts):
        await rp._reset(serial, bundle, flags, snap, shared, shared_snap, device_setup=device_setup)
        since = await rp.crash_window(serial)
        res, dumps = await run_with_dumps(serial, bundle, claim.steps)
        if res.outcome == rp.HOLDS:
            res = await evaluate(serial, bundle, claim.expect, res.steps_run, since)
        # The seeded-site markers are read on EVERY pass here (the gate reads them
        # only on a crash): a marker on the clean pass, or one for a bug that is not
        # this case's, is a broken flag gate and judge_case must see it.
        fired = await rp._fired_safe(serial, bundle)
        if res.outcome == rp.CRASHED:
            res = rp.gate_crash(res, claim.expect, fired, flags)
        elif fired is not None:
            res.fired = fired
        log.append(res)
        if res.outcome != rp.INCONCLUSIVE:
            break
    return res, dumps, log


def _claim(case: dict) -> Claim | None:
    raw = case.get("check")
    if not isinstance(raw, dict):
        return None
    steps = truth._steps(raw.get("steps"))
    expect, error = _parse_expect(raw.get("expect"), case["id"], trusted=True)
    if not steps or expect is None:
        print(f"  {case['id']}: unusable check — {error}")
        return None
    return Claim(area=case["id"], verdict="", steps=steps, expect=expect)


def _diff(off: list[list[str]], on: list[list[str]]) -> list[dict]:
    out = []
    for i in range(min(len(off), len(on))):
        a, b = {mask(t) for t in off[i]}, {mask(t) for t in on[i]}
        added, removed = sorted(b - a), sorted(a - b)
        if added or removed:
            out.append({"step": i + 1, "added": added, "removed": removed})
    return out


# ── verdict: pure, device-free ────────────────────────────────────────────────
# One trial = (ReplayResult, per-step screen dumps). A version's trials are judged
# all-or-nothing, like the hunt's --repeat: no majority, no averaging.

# CRASHED is a FAIL: the app died on the route, so the case's expected outcome was
# not reached — the same thing a VIOLATED oracle says, with a stack attached.
_LABEL = {rp.HOLDS: "PASS", rp.VIOLATED: "FAIL", rp.CRASHED: "FAIL"}

#   (verdict, per-step screens, every attempt in order — see `one_pass`)
# The third element is optional: a two-element trial reads as a single attempt, so a
# caller or a fixture that predates the attempt log still judges exactly as before.
Trial = tuple[rp.ReplayResult, list[list[str]], list[rp.ReplayResult]]


def attempt_log(trial) -> list[rp.ReplayResult]:
    """Every attempt of `trial`, in order; the last is the one the row carries."""
    return list(trial[2]) if len(trial) > 2 and trial[2] else [trial[0]]


def _pass_entry(trial) -> dict:
    """One trial as the row records it. The single builder for `passes` and `trials`,
    so the two cannot drift.

    `attempts` and `retries` appear only when the trial needed more than one attempt:
    the row of a trial that ran once is byte-identical to the rows already in the corpus,
    and their PRESENCE is the whole signal — this trial was retried, and what it retried
    away is in `retries` (QUA-2744). Nothing may require them: every reader of a journey
    truth row must treat both as optional, because every row derived before 2026-09-22
    lacks them whether or not it was retried."""
    res, log = trial[0], attempt_log(trial)
    out = {"outcome": res.outcome, "detail": res.detail, "steps_run": res.steps_run}
    if len(log) > 1:
        out["attempts"] = len(log)
        out["retries"] = [{"outcome": r.outcome, "detail": r.detail} for r in log[:-1]]
    return out


def masked_retries(row: dict) -> list[dict]:
    """Every trial of a committed truth `row` that needed more than one attempt:
    `{version, trial, attempts, retries}`, trial numbers 1-based.

    Pure, and reads the row rather than the run, so the same function answers "was this
    case retried?" for a derive that is happening now and for one that happened in
    March. `trials` is present only at `--repeat > 1`; without it `passes` IS trial 1."""
    out = []
    by_version = row.get("trials") or {k: [v] for k, v in (row.get("passes") or {}).items()}
    for version, entries in by_version.items():
        for i, entry in enumerate(entries, 1):
            if (entry or {}).get("attempts", 1) > 1:
                out.append({"version": version, "trial": i, "attempts": entry["attempts"],
                            "retries": entry.get("retries") or []})
    return out


def retry_note(entry: dict) -> str:
    """The one-line human form of a masked retry, for the derive's own output."""
    masked = " · ".join(f"{r.get('outcome')}: {r.get('detail')}" for r in entry["retries"])
    return (f"{entry['version']} trial {entry['trial']} needed {entry['attempts']} attempts"
            + (f" — discarded {masked}" if masked else ""))


def summarise_trials(trials: list[rp.ReplayResult]) -> dict:
    """{"outcomes": {outcome: count}, "label": PASS|FAIL|undecidable, "stable": bool, "n": int}.

    `stable` only when every trial gave the identical outcome; the label is that
    outcome through the PASS/FAIL mapping. Anything else — a flip, or an INCONCLUSIVE
    that the per-trial retry could not clear — is `undecidable`: an unstable version
    has no label, because its flip rate IS the finding."""
    outcomes = Counter(t.outcome for t in trials)
    stable = len(outcomes) == 1
    label = _LABEL.get(trials[0].outcome, "undecidable") if stable else "undecidable"
    return {"outcomes": dict(outcomes), "label": label, "stable": stable, "n": len(trials)}


def _hits(diff: list[dict], marker: str) -> list[int]:
    """The steps whose clean/seeded diff carries `marker`, typographic spaces FOLDED.

    Android renders a 12-hour time as `9:00 AM` (U+202F narrow no-break space;
    U+00A0 on older images) while a marker is typed with a plain space, so a raw
    substring test misses it. Every other comparison in the pipeline already folds —
    the anchor matcher (`replay._fold`) and the scorer (`journey._norm`) — and this
    gate was the last raw `in`. Unfolded, it reports a display defect that IS on the
    route as "not visible on this route" for exactly the markers that name a time
    (measured 2026-09-15: medtimer's `reminder-time-display-shifted`, stable across
    3/3 trials, while the committed key derived on an image that still used U+0020)."""
    want = rp._fold(marker)
    return [d["step"] for d in diff
            if want and any(want in rp._fold(t) for t in d["added"] + d["removed"])]


def _carries(marker: str, text: str) -> bool:
    """Does `text` carry `marker`, under the same folding as `_hits`?"""
    want = rp._fold(marker)
    return bool(want) and want in rp._fold(text)


def _trial_diff(clean: Trial, seeded: Trial) -> list[dict]:
    if clean[0].outcome == rp.INCONCLUSIVE or seeded[0].outcome == rp.INCONCLUSIVE:
        return []
    return _diff(clean[1], seeded[1])


def invisible_death(expected: str | None, seeded_outcome: str | None, diff: list[dict]) -> bool:
    """The signature of a seeded defect no tester the brief describes can see: the case
    must FAIL, its seeded arm DIES (CRASHED — a crash or an ANR), and every recorded
    screen matches the clean arm's, so the clean/seeded `diff` is empty (QUA-2742).

    `cal-complete-task` shipped with exactly this shape. Its patch wrote the completion
    row and THEN threw, inside the task editor — a secondary activity — so Android
    finished that activity and restarted the process on the event list beneath it,
    which showed the task completed. Only logcat differed. The derive agreed (the seeded
    arm really did die), and the board charged a UI tester who correctly reported PASS
    with a missed crash AND a failed completion.

    A seeded arm that fails with the app ALIVE and an empty diff is not this: its state
    oracle is what failed, and the brief sends the tester to the screen that shows it
    (`contacts-phone`, `contacts-favorite`). Pure, so a committed truth row can be put
    through the same test (`expected`, `passes.seeded.outcome`, `diff`)."""
    return expected == "FAIL" and seeded_outcome == rp.CRASHED and not diff


# A case whose witness is already complete before the step it tests carries this key,
# naming the ticket that removes it. In data, not only in prose: completion is a
# headline number, and a scorer or a report has to be able to exclude these cases
# (`journey.load_cases(app)[...]["witness_before_action"]`). Five cases carry it today
# (QUA-2740); QUA-2768 is the ticket that re-authors them.
WITNESS_EXEMPT_KEY = "witness_before_action"


def action_step(steps: list) -> int:
    """The 1-based route step a case MEASURES: its last step that is not a `wait`.

    Routes are authored in one shape — produce the state, then do the ONE thing the
    case is about, then read it back — and a trailing `wait` only lets the screen
    settle before that read. So the last real interaction IS the action under test.
    Screens are recorded one per step, so this indexes the recorded lists directly.
    0 for a route of nothing but waits."""
    acts = [i for i, s in enumerate(steps, 1) if s.action != "wait"]
    return acts[-1] if acts else 0


def witness_before_action(witness_out: dict, action: int) -> dict[str, list[int]]:
    """Per witness string, the CLEAN route steps BEFORE `action` on which it was
    already visible — the evidence behind `witness_credited_early`. Reads the same
    per-step visibility `judge_witness` records, so a committed truth row can be put
    through it unchanged."""
    out: dict[str, list[int]] = {}
    for w, arms in (witness_out or {}).items():
        early = [s for s in (arms.get("clean") or []) if s < action]
        if early:
            out[w] = early
    return out


def witness_credited_early(witness_out: dict, action: int) -> bool:
    """Can the whole witness be earned WITHOUT doing the thing the case tests?

    `journey_verdict` matches each witness string against everything the device
    answered with over the WHOLE episode, in any order — so a witness set already
    complete before the action is a free completion point: read the screen once, report
    the verdict the clean build was always going to give, stop. True only when EVERY
    string has a pre-action sighting; one string that only the destination shows makes
    the set unearnable early, which is what a sound witness is (QUA-2740)."""
    if not witness_out or action <= 1:
        return False
    return len(witness_before_action(witness_out, action)) == len(witness_out)


def marker_visibility(clean: list[Trial], seeded: list[Trial], marker: str) -> list[bool]:
    """Per trial index, whether `marker` is in that trial's clean/seeded screen diff.
    Trials pair by index (clean #i against seeded #i); a pair with an INCONCLUSIVE side
    has no diff and counts as not seen."""
    return [bool(_hits(_trial_diff(c, s), marker)) for c, s in zip(clean, seeded)]


def _screen_has(texts: list[str], witness: str) -> bool:
    """The scorer's own match (`journey._word` over `journey._evidence`, on normalised
    text): a witness must be found on a recorded screen exactly the way the agent's
    device text will be searched for it."""
    needle = journey._evidence(witness)
    return bool(needle) and any(journey._word(needle, journey._norm(t)) for t in texts)


def _overlaps(witness: str, measured: str) -> bool:
    w, m = journey._norm(witness), journey._norm(measured)
    return bool(w and m) and (w in m or m in w)


def judge_witness(witness: list[str], trials: dict[str, list[Trial]], side_out: list[dict],
                  problems: list[str], action: int = 0, exempt: str = "") -> dict:
    """The case's `evidence:` witnesses against the recorded screens. Each must be on
    the CLEAN pass's FINAL screen — that is the screen the brief sends the agent to,
    and the scorer will demand the string from the agent's device text there — none
    may sit inside a display bug's measured `texts` (a string one arm shows and the
    other does not is a marker, not a witness), and the set must not already be
    complete BEFORE the step the case tests (`witness_credited_early`, QUA-2740: a
    witness readable before the action credits completion to an agent that never
    performed it). Returns, per string, the route steps on which it is visible on each
    arm (`{string: {"clean": [...], "seeded": [...]}}`); problems are appended in
    place. Both whole-route checks are skipped when the clean pass did not HOLD (that
    is already the case's problem).

    `action` is `action_step(route)`; `exempt` is the case's `WITNESS_EXEMPT_KEY`
    ticket, the only way past the early-credit refusal — and a marker on a case whose
    witness is NOT credited early is itself a problem, so the exemption cannot outlive
    the weakness it records."""
    out: dict[str, dict[str, list[int]]] = {}
    clean_res, clean_screens = trials["clean"][0][0], trials["clean"][0][1]
    for w in witness:
        out[w] = {version: [i + 1 for i, screen in enumerate(runs[0][1]) if _screen_has(screen, w)]
                  for version, runs in trials.items()}
        if clean_res.outcome == rp.HOLDS and not _screen_has(clean_screens[-1] if clean_screens else [], w):
            problems.append(f"witness {w!r} not on the clean route's final screen")
        for s in side_out:
            leak = [t for t in s["texts"] if _overlaps(w, t)]
            if leak:
                problems.append(f"witness {w!r} sits inside display bug {s['bug']}'s measured "
                                f"texts {leak} — a marker, not a witness")
    if out and clean_res.outcome == rp.HOLDS:
        early = witness_before_action(out, action)
        if witness_credited_early(out, action):
            if not exempt:
                problems.append(
                    f"witness {sorted(out)} is already complete on the clean route before the "
                    f"action this case tests (step {action}): {early} — completion can be "
                    f"credited to an agent that never performed it. Witness a string only the "
                    f"post-action screen carries, or mark the case `{WITNESS_EXEMPT_KEY}: "
                    f"<ticket>` when the route has none")
        elif exempt:
            problems.append(
                f"`{WITNESS_EXEMPT_KEY}: {exempt}` is stale — witness {sorted(out)} is no "
                f"longer complete before step {action}; drop the key")
    return out


def judge_case(design: dict, trials: dict[str, list[Trial]],
               witness: list[str] | None = None, action: int = 0, exempt: str = "") -> dict:
    """The per-case verdict row (everything but `name`) from collected trials.

    `passes`, `screens` and `diff` come from the FIRST trial of each version so the
    single-pass readers of journey-<app>.json keep working; with more than one trial
    the row also carries every trial (`trials`) and the per-version summary
    (`stability`), and any disagreement between trials is a problem. A trial that was
    RETRIED (`one_pass`) also carries `attempts` and the discarded attempts' verdicts
    (`_pass_entry`), in whichever of the two blocks holds it — absent when it ran once,
    so a retry-free row is unchanged. A case that declares `evidence:` passes it as
    `witness`: each string is verified on the clean
    route's final screen, against the display bugs' measured texts and against the
    step the case tests (`judge_witness`, `action`/`exempt`), and the row carries
    `witness` — absent otherwise, so the row of a case without one is byte-identical
    to before."""
    problems: list[str] = []
    summary = {k: summarise_trials([t[0] for t in v]) for k, v in trials.items()}
    n = summary["clean"]["n"]
    clean = trials["clean"][0]
    if not summary["clean"]["stable"]:
        problems.append(f"clean version unstable across {n} trials: {summary['clean']['outcomes']}")
    elif clean[0].outcome != rp.HOLDS:
        problems.append(f"clean version does not pass its oracle ({clean[0].outcome}: "
                        f"{clean[0].detail}) — check or app broken upstream")
    fired_clean = sorted({m for t in trials["clean"] for m in (t[0].fired or [])})
    if fired_clean:
        problems.append(f"seeded-site marker(s) {fired_clean} fired on the CLEAN version — "
                        f"the flag gate does not hold")
    measured, diff, side_out, unclaimed = None, [], [], []
    if "seeded" in trials:
        seeded = trials["seeded"][0]
        measured = summary["seeded"]["label"]
        if not summary["seeded"]["stable"]:
            problems.append(f"seeded version unstable across {summary['seeded']['n']} trials: "
                            f"{summary['seeded']['outcomes']}")
        elif measured != design["expected"]:
            why = f": {seeded[0].detail}" if measured == "undecidable" and seeded[0].detail else ""
            problems.append(f"bugs {design['bugs']} imply {design['expected']}, measured {measured}{why}")
        elif design.get("death") and seeded[0].outcome != rp.CRASHED:
            # The check names HOW the seeded arm fails (crash/anr/stuck); a seeded arm
            # that failed its state oracle with the app alive is a different defect.
            problems.append(f"check expects the seeded version to fail by {design['death']}, but it "
                            f"failed with the app alive ({seeded[0].outcome}: {seeded[0].detail})")
        fired_seeded = sorted({m for t in trials["seeded"] for m in (t[0].fired or [])})
        if fired_seeded and design.get("blocking") and design["blocking"] not in fired_seeded:
            problems.append(f"seeded-site marker(s) {fired_seeded} fired, but not the blocking "
                            f"bug's ({design['blocking']})")
        diff = _trial_diff(clean, seeded)
        # Judged per trial pair, all-or-nothing like a side marker: a trial whose death
        # left every screen identical is one on which a tester saw nothing. Only once
        # both versions are stable and the clean one holds — otherwise that is already
        # the case's problem, and an INCONCLUSIVE side has no diff to judge.
        if (summary["clean"]["stable"] and clean[0].outcome == rp.HOLDS
                and summary["seeded"]["stable"]):
            blind = [i + 1 for i, (c, s) in enumerate(zip(trials["clean"], trials["seeded"]))
                     if invisible_death(design["expected"], s[0].outcome, _trial_diff(c, s))]
            pairs = min(len(trials["clean"]), len(trials["seeded"]))
            if blind:
                where = "" if len(blind) == pairs else f" on trial(s) {blind} of {pairs}"
                problems.append(
                    f"seeded version dies ({seeded[0].outcome}) but its clean/seeded screen "
                    f"diff is empty{where} — the defect is invisible on this route: a tester "
                    f"who follows the brief sees what the clean build shows. Fault before the "
                    f"state it corrupts, or give the route a step that reads that state back")
        for s in design["side"]:
            marker = s["marker"]
            hits = _hits(diff, marker)
            # `texts` keeps the RAW screen string (the scorer folds/normalises it
            # itself); only the membership test folds.
            texts = sorted({t for d in diff for t in d["added"] + d["removed"]
                            if _carries(marker, t)})
            if not marker:
                problems.append(f"display bug {s['bug']} has no marker")
            elif not hits:
                problems.append(f"display bug {s['bug']}: marker {marker!r} is not in the clean/seeded "
                                f"screen diff — not visible on this route")
            if marker and n > 1:
                # A side bug is a bug: the same all-or-nothing rule as the versions.
                seen = marker_visibility(trials["clean"], trials["seeded"], marker)
                if 0 < sum(seen) < len(seen):
                    problems.append(f"display bug {s['bug']}: marker visibility unstable "
                                    f"(seen in {sum(seen)}/{len(seen)} trials)")
            side_out.append({"bug": s["bug"], "marker": marker, "visible_steps": hits, "texts": texts})
        unclaimed = [d for d in diff
                     if not any(_carries(s["marker"], t) for s in design["side"]
                                for t in d["added"] + d["removed"])]
    witness_out = (judge_witness(witness, trials, side_out, problems, action=action, exempt=exempt)
                   if witness else None)

    row = {
        "bugs": design["bugs"],
        "expected": design["expected"],
        "measured": measured,
        "blocking": design["blocking"],
        "side": side_out,
        "agrees": not problems,
        "problems": problems,
        "diff": diff,
        "unclaimed_diff": unclaimed,
        "passes": {k: _pass_entry(v[0]) for k, v in trials.items()},
        "screens": {k: v[0][1] for k, v in trials.items()},
    }
    if witness_out is not None:
        row["witness"] = witness_out
    if n > 1:
        row["trials"] = {k: [_pass_entry(t) for t in v] for k, v in trials.items()}
        row["stability"] = summary
    return row


async def stage(serial: str, suite: dict, tmp: Path) -> tuple[Path | None, list[str], Path | None]:
    """Episode-identical staging, snapshotted once so every pass starts equal."""
    bundle = suite["app"]["package"]
    reset_dump_source(serial)
    await _adb(serial, "shell", f"pm clear {bundle}")
    await grant_requested_permissions(serial, bundle)
    shared = rp.safe_shared_paths(suite.get("shared_storage"))
    for path in shared:
        await _adb(serial, "shell", f"rm -rf {shlex.quote(path)}")
        await _adb(serial, "shell", f"mkdir -p {shlex.quote(path)}")
    await run_device_setup(serial, suite.get("device_setup"))
    await rp.set_flags(serial, bundle, [])
    await relaunch(serial, bundle)
    # Upright before check_setup and the snapshot. A setup route need not start with
    # `launch` (fossify-calendar's opens on a tap), so it runs on THIS launch, and the
    # device may still be landscape from whatever ran on it last (QUA-2734).
    await rp.repin_portrait_after_launch(serial, bundle)
    await asyncio.sleep(3.0)
    await wait_stable(serial)
    setup = truth.setup_of(suite["exploration"])
    if setup:
        got = await rp.run_steps(serial, bundle, setup)
        print(f"  check_setup: {got.steps_run}/{len(setup)} steps"
              f"{'' if got.outcome == rp.HOLDS else '  FAILED: ' + got.detail}")
    snap = tmp / f"{suite['app']['id']}-{serial}.tar"
    if not await rp.snapshot(serial, bundle, snap):
        print("  WARNING: app-data snapshot empty; passes may not be deterministic")
        snap = None
    shared_snap = None
    if shared and suite.get("restore_shared", True):
        shared_snap = tmp / f"{suite['app']['id']}-{serial}-shared.tar"
        if not await rp.snapshot_shared(serial, shared, shared_snap):
            shared_snap = None
    return snap, shared, shared_snap


async def derive_app(app_id: str, serial: str, only: set[str] | None, tmp: Path,
                     repeat: int = 1) -> dict:
    suite = load_suite(corpus.spec_path(app_id))
    doc = journey.load_cases(app_id)
    if not doc:
        print(f"{app_id}: no test-case file"); return {}
    bundle = suite["app"]["package"]
    defects = journey.load_defects(doc)
    known = {b["id"] for b in suite.get("bugs", [])}
    if unknown := set(defects) - known:
        print(f"{app_id}: defects not in the benchmark spec: {sorted(unknown)}"); return {}
    cases = [c for c in doc.get("test_cases", []) if not only or c["id"] in only]
    print(f"\n{app_id} ({bundle}) · {len(cases)} test case(s) on {serial}")

    snap, shared, shared_snap = await stage(serial, suite, tmp)
    device_setup = suite.get("device_setup")
    out: dict = {}
    for case in cases:
        claim = _claim(case)
        if claim is None:
            continue
        design = journey.case_design(case, defects)
        print(f"\n  [{case['id']}] {len(claim.steps)} steps · bugs {design['bugs'] or '-'} · seeded expects {design['expected']}")
        trials: dict[str, list[Trial]] = {}

        async def run(name: str, flags: list[str]):
            # collect: N trials, each from a fresh reset (one_pass resets; its
            # INCONCLUSIVE-only retry stays inside the trial and is orthogonal).
            # TODO(stability): a CRASHED trial that died in the first step or two is
            # usually the HOST, not the case — the whole-corpus sweep (QUA-2707) put the
            # replayer's flip rate at 1/80 versions and its ONE flip was an
            # input-dispatch ANR at step 2 of a 14-step route that then went 10/10 on a
            # re-derive. Today that costs a manual re-derive per occurrence. If it
            # becomes common, consider re-running a trial whose death came before the
            # route reached the defect's screen — but do NOT fold it into the
            # INCONCLUSIVE retry, which exists to break anchor ties: a death the case
            # DOES explain (a crash/anr/stuck gate) must stay a trial outcome, or the
            # gate stops measuring the margin it was built to measure.
            runs: list[Trial] = []
            for i in range(repeat):
                t0 = time.monotonic()
                res, dumps, log = await one_pass(serial, bundle, claim, flags, snap, shared,
                                                 shared_snap, device_setup)
                runs.append((res, dumps, log))
                tag = name if repeat == 1 else f"{name} {i + 1}/{repeat}"
                # The retry is named on the trial's OWN line, while the operator is
                # watching: a trial that took two attempts and an honest one read the
                # same until QUA-2744, and the elapsed seconds are the only tell.
                masked = "" if len(log) < 2 else f"  [{len(log)} attempts]"
                print(f"    {tag:8} {res.outcome:12} {res.steps_run:2} steps "
                      f"{time.monotonic() - t0:4.0f}s  {res.detail}{masked}")
            trials[name] = runs

        dumps_before = dump_stats(serial)
        await run("clean", [])
        if design["bugs"]:
            await run("seeded", design["bugs"])

        witness = [str(e) for e in (case.get("evidence") or []) if str(e).strip()]
        row = {"name": case.get("name"),
               **judge_case(design, trials, witness=witness,
                            action=action_step(claim.steps),
                            exempt=str(case.get(WITNESS_EXEMPT_KEY) or "").strip())}
        # Which source served each of this case's hierarchy dumps, over every pass and
        # trial: builtin / u2 / none, plus built-in attempts SIGKILLed on the device.
        # A diagnostic, read by no scorer. The u2 fallback keeps a derive reading when
        # the built-in dump is dead, and without this row nothing showed that it had
        # happened (QUA-2741).
        row["dump_stats"] = dump_stats_since(serial, dumps_before)
        out[case["id"]] = row
        mark = "AGREES" if row["agrees"] else "DISAGREE"
        print(f"    => {mark}: clean {row['passes']['clean']['outcome']}"
              f"{'' if row['measured'] is None else ' · seeded ' + row['measured']}"
              f" · side {[(s['bug'], s['visible_steps']) for s in row['side']] or '-'}")
        for p in row["problems"]:
            print(f"       ! {p}")
        # A masked retry is not a problem — the trial was judged, and its verdict is the
        # row's — so it does not make the case DISAGREE. It is printed under the case
        # anyway, because a case that needs two attempts to be judged is the shape a
        # quietly flaky case has, and nothing else here says it happened.
        for m in masked_retries(row):
            print(f"       ~ retried: {retry_note(m)}")
        if set(row["dump_stats"]) - {"builtin"}:
            # Only when something other than the built-in dump served, or it was killed.
            print("       harness dumps: " + " · ".join(
                f"{k} {v}" for k, v in sorted(row["dump_stats"].items())))
        for d in row["unclaimed_diff"]:
            print(f"       unclaimed diff @step {d['step']}: +{d['added'][:5]} -{d['removed'][:5]}")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("apps", nargs="+", help="app ids with a data/test-cases/<app>.yaml")
    ap.add_argument("--device", default="emulator-5554")
    ap.add_argument("--case", action="append", help="derive only this case id (repeatable)")
    ap.add_argument("--json", help="write the result here (default data/truth/journey-<app>.json; "
                                   "merged with existing entries when --case is used)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="run each version (clean, seeded) this many times, each from a fresh "
                         "reset, and require every trial to give the same outcome. A case that "
                         "is not STABLE is not an oracle: its flip rate is the replayer's own "
                         "error rate, measured rather than assumed, and an unstable case must "
                         "leave the corpus — it is never averaged in. Required (>= 3) for any "
                         "case whose defect is a forced interleaving, a crash or a stuck-screen "
                         "oracle, since one trial cannot measure a margin. NOTE the reset "
                         "between trials restores the app-data snapshot and shared storage but "
                         "NOT time: a case that depends on the time of day (see TODO(fixture) "
                         "on medtimer-correct-dose-amount) can flip between trials for that reason "
                         "alone — such an instability report is a corpus finding, not a "
                         "replayer error.")
    args = ap.parse_args()
    if args.repeat < 1:
        ap.error("--repeat must be >= 1")
    tmp = ROOT / "runs" / "_derive_scratch"
    tmp.mkdir(parents=True, exist_ok=True)
    only = set(args.case or []) or None
    rc = 0
    checks, unstable, retried = 0, [], []
    for app_id in dict.fromkeys(args.apps):
        result = await derive_app(app_id, args.device, only, tmp, repeat=args.repeat)
        for case_id, row in result.items():
            for version, s in row.get("stability", {}).items():
                checks += 1
                if not s["stable"]:
                    unstable.append(f"{app_id}/{case_id}/{version}: {s['outcomes']}")
            # Collected from the ROW, so this block reports exactly what was written.
            retried += [f"{app_id}/{case_id}: {retry_note(m)}" for m in masked_retries(row)]
        # `journey.truth_path` resolves a held-out app into the held-out directory, so
        # a derived key never lands back in the repository.
        dest = Path(args.json) if args.json else journey.truth_path(app_id)
        if only and dest.exists():
            merged = json.loads(dest.read_text())
            merged.update(result)
            result = merged
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(result, indent=2))
        bad = [k for k, v in result.items() if not v.get("agrees")]
        print(f"\n{app_id}: {len(result) - len(bad)}/{len(result)} cases agree with the authored key"
              f"{'' if not bad else ' — DISAGREE: ' + ', '.join(bad)}")
        print(f"wrote {dest}")
        rc = rc or (1 if bad else 0)
    if args.repeat > 1:
        print(f"\nstability: {checks - len(unstable)}/{checks} checks gave the SAME label "
              f"in all {args.repeat} trials")
        for u in unstable:
            print(f"  UNSTABLE  {u}")
    # Printed at every --repeat, including 1: a single-trial derive can mask a retry too,
    # and this block is the one a long derive's operator actually reads. A retry does not
    # change the exit code — the trial WAS judged — but a case that needed two attempts
    # to be judged is the first thing to re-derive when its verdict is questioned.
    if retried:
        print(f"\nmasked retries: {len(retried)} trial(s) needed more than one attempt "
              f"(the row records `attempts` and what was discarded)")
        for r in retried:
            print(f"  RETRIED  {r}")
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
