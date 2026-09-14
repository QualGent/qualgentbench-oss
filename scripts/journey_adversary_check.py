#!/usr/bin/env python3
"""Journey adversary check: can an agent be credited on a test case WITHOUT running it?

Hunt mode has had `scripts/adversary_check.py` between it and a published number since
the beginning; journey mode had nothing, and it paid for that — a report of
`observed: "anything at all"` on contacts-delete~seeded matched the blocking bug (a
derived blocking text of `A`, the contacts section index) and so earned recall AND
completion, because a blocked case completes on "fail + the blocking bug named".

Three synthetic guessers run through the real `journey.journey_verdict` against the real
corpus — offline, no device, no agent, no tokens:

  short-spray    quotes a one-character screen value on every case, with vague prose
  generic-spray  vague prose only, nothing quoted from any screen
  dead           writes the luckiest possible verdict and never touches the device

Each must be credited NOTHING: no bug found, no completion, anywhere. The positive
control is `honest`, whose reports quote the screen strings `derive_journey.py` measured
for each seeded defect; it must find them. `honest-text` is the same agent with its prose
removed, which measures whether the measured strings ALONE still identify the defect —
where they do not, the corpus is the finding (a defect whose only signal was a string too
short to be evidence), not the matcher. Those are printed, never silently passed.
"""

from __future__ import annotations

import argparse
import json
import sys

import yaml

from qualgentbench import bugs, journey

# Prose with no defect-specific content: it must name no screen value and contain no
# symptom word from any defect in the corpus (`_no_symptom_leaks_into_the_guessers`
# below proves it, so a new defect cannot quietly hand the guessers a match).
VAGUE = "the screen did not look the way the test case describes"
VAGUER = "something about this felt off while I was working through it"
GUESSERS = ("short-spray", "generic-spray", "dead")
MODES = GUESSERS + ("honest", "honest-text")


# ── synthesising an episode ────────────────────────────────────────────────────

def _device(payload: str, i: int) -> str:
    """One device tool call plus its observation, carrying `payload` as visible text."""
    call = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}",
         "name": "mcp__device__mobile_tap_and_observe", "input": {"device": "d"}}]}})
    res = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{i}",
         "content": [{"type": "text", "text": payload}]}]}})
    return call + "\n" + res


def _text(s: str) -> str:
    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text", "text": s}]}})


def _quotes(spec: dict, bug_id: str) -> list[str]:
    """The screen strings the corpus MEASURED for one active defect — what an honest
    report can quote. Strings carrying a `<placeholder>` are dropped: they are
    normalised truth, not anything a screen ever showed."""
    out: list[str] = []
    for s in spec.get("side") or []:
        if s.get("bug") == bug_id:
            out += [s.get("marker") or ""] + list(s.get("texts") or [])
    if bug_id == spec.get("blocking"):
        out += list(spec.get("blocking_texts") or [])
    return [t for t in out if t and "<" not in t and journey._evidence(t)]


def _symptom(spec: dict, bug_id: str) -> str:
    """The defect's own longest symptom phrase — the vocabulary an honest tester writes
    when the misbehaviour has no string to quote (a dropped field, a lost reminder)."""
    syms = ((spec.get("defects") or {}).get(bug_id) or {}).get("symptoms") or []
    return max(syms, key=len) if syms else ""


def _episode(spec: dict, mode: str) -> tuple[str, str]:
    """(transcript, findings file) for one synthetic agent on one seeded task."""
    expected = str(spec.get("expected") or "PASS").lower()
    active = list(spec.get("active_bugs") or [])
    payloads: list[str] = ["the app is on screen"]
    entries: list[dict] = []

    if mode == "dead":
        # No device work at all, and the luckiest verdict it could have guessed.
        return _text(f"I believe the outcome is {expected}.") + "\n", \
            yaml.safe_dump({"verdict": expected, "bugs": []}, sort_keys=False)
    if mode == "short-spray":
        verdict = "fail"
        entries = [{"step": 1, "screen": "the app", "observed": "1", "expected": "2",
                    "description": VAGUE},
                   {"step": 2, "screen": "the app", "observed": "0", "expected": "1",
                    "description": VAGUER}]
    elif mode == "generic-spray":
        verdict = "fail"
        entries = [{"step": 1, "screen": "the app", "description": VAGUE},
                   {"step": 2, "screen": "the next screen", "description": VAGUER}]
    else:
        verdict = expected
        for i, bug_id in enumerate(active, 1):
            quotes = _quotes(spec, bug_id)
            observed = quotes[0] if quotes else ""
            if observed:
                payloads.append(observed)
            entries.append({"step": i, "screen": "the app", "observed": observed,
                            "description": VAGUE if mode == "honest-text"
                            else (_symptom(spec, bug_id) or VAGUE)})
    lines = [_device(p, i) for i, p in enumerate(payloads)]
    return "\n".join(lines) + "\n", yaml.safe_dump({"verdict": verdict, "bugs": entries},
                                                   sort_keys=False)


def run(task, mode: str) -> dict:
    """One synthetic episode through the real scorer. The spec is copied, so the oracle
    stays exactly as the corpus defines it — offline, a `db:` oracle is unevaluated,
    which is the same thing the runner reports when the device cannot answer."""
    spec = dict(task.bug_spec)
    transcript, findings = _episode(spec, mode)
    spec["tooling"] = "mcp"
    spec["findings_file"] = findings
    task.bug_spec = spec
    return journey.journey_verdict(transcript, "synthetic", task).metrics


# ── the gate ───────────────────────────────────────────────────────────────────

def _seeded_tasks(app_ids: list[str] | None) -> list:
    out = []
    for suite in bugs.load_apps():
        app_id = str(suite["app"].get("id") or "")
        if not journey.has_cases(app_id) or (app_ids and app_id not in app_ids):
            continue
        out += [t for t in journey.journey_tasks(suite)
                if (t.bug_spec.get("version") == "seeded") and t.bug_spec.get("active_bugs")]
    return out


def _no_symptom_leaks_into_the_guessers(tasks: list) -> list[str]:
    """The guessers' prose must be defect-free BY CONSTRUCTION. If a new defect lists a
    symptom word that appears in it, the gate would start failing for a reason that has
    nothing to do with the scorer — say so instead."""
    hay = f"{VAGUE} {VAGUER}".lower()
    leaks = []
    for task in tasks:
        for bug_id, d in (task.bug_spec.get("defects") or {}).items():
            for sym in d.get("symptoms") or []:
                if sym and journey._word(sym.lower(), hay):
                    leaks.append(f"{bug_id}: symptom {sym!r} appears in the guesser prose")
    return sorted(set(leaks))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--app", help="comma-separated app ids (default: every app with test cases)")
    args = ap.parse_args()
    app_ids = [a.strip() for a in args.app.split(",")] if args.app else None

    tasks = _seeded_tasks(app_ids)
    if not tasks:
        print("FAIL: no seeded journey tasks found — nothing was checked")
        return 1

    leaks = _no_symptom_leaks_into_the_guessers(tasks)
    by_app: dict[str, list] = {}
    for t in tasks:
        by_app.setdefault(str(t.bug_spec.get("app_id")), []).append(t)

    # per mode: bugs credited, bugs present, completions, episodes
    tot = {m: [0, 0, 0, 0] for m in MODES}
    leaked: dict[str, list[str]] = {m: [] for m in GUESSERS}
    # An honest miss is a SCORER failure when the corpus measured a quotable screen
    # string for the defect and quoting it still earned nothing. When the corpus measured
    # nothing quotable, the finding is the corpus: a display defect whose only signal was
    # below the evidence floor has no honest report left, and a functional defect with no
    # string to quote is prose-only by nature.
    unquotable: dict[str, list[str]] = {"honest": [], "honest-text": []}
    uncreditable: dict[str, list[str]] = {"honest": [], "honest-text": []}

    def _classify(mode: str, task, missed: set[str]) -> None:
        spec = task.bug_spec
        for bug_id in sorted(missed):
            kind = ((spec.get("defects") or {}).get(bug_id) or {}).get("kind") or "?"
            where = uncreditable if _quotes(spec, bug_id) else unquotable
            where[mode].append(f"{task.id}: {bug_id} ({kind})")

    print(f"{'app':18s}{'episodes':>10s}" + "".join(f"{m:>16s}" for m in MODES))
    print(f"{'':18s}{'':>10s}" + "".join(f"{'bugs / done':>16s}" for m in MODES))
    for app_id, app_tasks in sorted(by_app.items()):
        cells = []
        for mode in MODES:
            found = present = done = 0
            for task in app_tasks:
                m = run(task, mode)
                active = list(m.get("bugs_present") or [])
                credited = list(m.get("bugs_found") or [])
                present += len(active)
                found += len(credited)
                done += 1 if m.get("completed") is True else 0
                if mode in GUESSERS and (credited or m.get("completed") is True):
                    leaked[mode].append(
                        f"{task.id}: {len(credited)} bug(s) credited"
                        f"{' + completion' if m.get('completed') is True else ''}")
                if mode in unquotable and set(active) - set(credited):
                    _classify(mode, task, set(active) - set(credited))
            t = tot[mode]
            t[0] += found; t[1] += present; t[2] += done; t[3] += len(app_tasks)
            cells.append(f"{found}/{present} · {done}")
        print(f"{app_id:18s}{len(app_tasks):>10d}" + "".join(f"{c:>16s}" for c in cells))
    print(f"{'TOTAL':18s}{len(tasks):>10d}" +
          "".join(f"{f'{tot[m][0]}/{tot[m][1]} · {tot[m][2]}':>16s}" for m in MODES))
    print("\nbugs = seeded defects credited / present · done = episodes scored completed")

    ok = True
    for mode in GUESSERS:
        if leaked[mode]:
            ok = False
            print(f"\nFAIL: '{mode}' earned credit without testing on {len(leaked[mode])} "
                  f"episode(s) — the harness cannot tell a report from a guess:")
            for line in leaked[mode][:12]:
                print(f"  {line}")
    for mode in ("honest", "honest-text"):
        if uncreditable[mode]:
            ok = False
            print(f"\nFAIL: '{mode}' quoted the screen string the corpus measured and was "
                  f"credited nothing for {len(uncreditable[mode])} defect(s) — the scorer "
                  f"cannot read an honest report:")
            for line in uncreditable[mode]:
                print(f"  {line}")
    if leaks:
        ok = False
        print("\nFAIL: the guessers' prose is no longer defect-free:")
        for line in leaks:
            print(f"  {line}")

    # Everything below is a CORPUS finding, printed on every run: the scorer behaved, the
    # key is thin. A display defect here has no honest report left at all — its only
    # measured signal is below the evidence floor — and that is a corpus fix (re-derive
    # truth on a device), never a reason to lower the floor.
    for mode, what in (("honest", "cannot be reported at all"),
                       ("honest-text", "cannot be reported by quoting the screen")):
        if not unquotable[mode]:
            continue
        display = [x for x in unquotable[mode] if "(display)" in x]
        print(f"\nCORPUS: {len(unquotable[mode])} seeded defect(s) the honest control "
              f"{what} — nothing quotable was measured for them "
              f"({len(display)} of them DISPLAY defects, whose whole content is a screen "
              f"string, so thin evidence there is a corpus gap; the rest are functional "
              f"defects, which are behaviour and legitimately need prose):")
        for line in unquotable[mode]:
            print(f"  {line}")

    if ok:
        print(f"\nPASS: every guesser earned 0 bugs and 0 completions over {len(tasks)} "
              f"seeded episode(s); honest found {tot['honest'][0]}/{tot['honest'][1]} "
              f"and every defect it missed has no quotable evidence in the corpus")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
