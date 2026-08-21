#!/usr/bin/env python3
"""Adversary check: can an agent score well WITHOUT testing? Four synthetic hunters
(spray, crud-prior, oracle-guess, honest) run through the real scorer; honest must
win, or the harness measures priors rather than testing."""

from __future__ import annotations

import argparse
import json
import re
import sys

from qualgentbench import bugs

MUTATE = re.compile(r"delete|edit|rename|archive|save|pin|star|favou?rite|sort|search"
                    r"|check|remove|clear|quantity|create|add|backspace|decimal")
READ = re.compile(r"view|list|detail|overview|navigation|settings|content|select")


def _text(s: str) -> str:
    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text", "text": s}]}})


def _device(payload: str, i: int) -> str:
    """One device tool call plus its observation, carrying `payload` as visible text."""
    call = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}",
         "name": "mcp__device__mobile_tap_and_observe", "input": {"device": "d"}}]}})
    res = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{i}",
         "content": [{"type": "text", "text": payload}]}]}})
    return call + "\n" + res


def _report(status: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "rep", "name": "mcp__device__mobile_report_result",
         "input": {"status": status, "summary": "done"}}]}})


def _area(fid: str, deviates: bool) -> str:
    v = "deviates" if deviates else "as_specified"
    return _text(f"AREA: {fid} | VERDICT: {v} | EXPECTED: spec | ACTUAL: observed")


def build(features: list[dict], mode: str) -> str:
    """Synthesise one agent's episode."""
    lines: list[str] = []
    i = 0
    if mode == "spray":
        lines += [_device("home screen", 0), _device("home screen", 1)]
        lines.append(_text(" ".join(
            f"AREA: {f['id']} | VERDICT: deviates | EXPECTED: s | ACTUAL: o"
            for f in features)))
        lines.append(_report("FAIL"))
        return "\n".join(lines) + "\n"

    for f in features:
        if mode == "crud":
            guess = bool(MUTATE.search(f["id"])) and not READ.search(f["id"])
        elif mode == "oracle":
            guess = f["state"] == "broken"
        else:                                   # honest
            guess = f["state"] == "broken"
        # Pace claims so the work-between-claims rule passes either way; only
        # `honest` puts the feature's probe keyword in the device text.
        for _ in range(4):
            payload = (f"{(f.get('probe') or ['screen'])[0]} screen"
                       if mode == "honest" else "generic screen")
            lines.append(_device(payload, i))
            i += 1
        lines.append(_area(f["id"], guess))
    lines.append(_report("FAIL"))
    return "\n".join(lines) + "\n"


def _hybrid_gate(suites: list) -> bool:
    """The board scores via hybrid_score over replay, which had its own hole:
    UNREPLAYABLE claims left the precision denominator, so spray-everything with
    non-replaying reproductions got full recall and zero false positives."""
    from qualgentbench.hybrid_score import combine
    from qualgentbench.replay_score import score as replay_score
    from qualgentbench import replay as rp

    print(f"\n{'app':20s}{'spray-unreplayable':>20s}{'honest':>12s}")
    sprays, honests = [], []
    for suite in suites:
        feats = bugs.exploration_task(suite).bug_spec["features"]
        derived = {f["id"]: f.get("state") for f in feats}
        seeded = [f for f in feats if f.get("state") == "broken"]

        # A1': every area claimed broken, no reproduction ever replays.
        spray_res = [{"area": f["id"], "claimed": "deviates",
                      "classification": rp.UNREPLAYABLE} for f in feats]
        spray_verd = {f["id"]: "deviates" for f in feats}
        s = replay_score(feats, spray_res, key_verdicts=spray_verd, derived=derived)
        sh = combine(feats, {"bugs_found": len(seeded), "steps": 10,
                             "step_budget": 500}, s)

        # H: the seeded defects, each demonstrated; controls reported working.
        hon_res = [{"area": f["id"], "claimed": "deviates",
                    "classification": rp.CONFIRMED} for f in seeded]
        h = replay_score(feats, hon_res,
                         key_verdicts={f["id"]: "deviates" for f in seeded},
                         derived=derived)
        hh = combine(feats, {"bugs_found": len(seeded), "steps": 10,
                             "step_budget": 500}, h)

        sprays.append(sh.overall); honests.append(hh.overall)
        print(f"{suite['app']['id']:20s}{sh.overall:20.3f}{hh.overall:12.3f}")

    ms, mh = sum(sprays)/len(sprays), sum(honests)/len(honests)
    print(f"{'MEAN':20s}{ms:20.3f}{mh:12.3f}")
    if ms >= mh:
        print(f"\nFAIL: hybrid/board scorer — spray-unreplayable ({ms:.3f}) >= "
              f"honest ({mh:.3f}); an undemonstrable claim is free")
        return False
    print(f"\nPASS: hybrid/board scorer — honest ({mh:.3f}) beats "
          f"spray-unreplayable ({ms:.3f})")
    return True


def main_hunt() -> int:
    suites = [s for s in bugs.load_apps() if s["app"].get("difficulty") == "easy"]
    modes = ("spray", "crud", "oracle", "honest")
    print(f"{'app':20s} " + "".join(f"{m:>12s}" for m in modes))
    totals = {m: [] for m in modes}
    for suite in suites:
        task = bugs.exploration_task(suite)
        feats = task.bug_spec["features"]
        row = []
        for m in modes:
            v = bugs.exploration_verdict(build(feats, m), "m", task)
            row.append(v.metrics["overall_raw"])
            totals[m].append(v.metrics["overall_raw"])
        print(f"{suite['app']['id']:20s} " + "".join(f"{x:12.3f}" for x in row))
    print(f"{'MEAN':20s} " +
          "".join(f"{sum(v)/len(v):12.3f}" for v in totals.values()))

    hybrid_ok = _hybrid_gate(suites)

    mean = {m: sum(v) / len(v) for m, v in totals.items()}
    ok = hybrid_ok
    for guesser in ("spray", "crud", "oracle"):
        if mean[guesser] >= mean["honest"]:
            print(f"\nFAIL: '{guesser}' ({mean[guesser]:.3f}) >= honest "
                  f"({mean['honest']:.3f}) — the harness cannot tell testing from guessing")
            ok = False
    if ok:
        print(f"\nPASS: honest ({mean['honest']:.3f}) beats every guesser "
              f"(best {max(mean[g] for g in ('spray','crud','oracle')):.3f})")
    return 0 if ok else 1


def main() -> int:
    argparse.ArgumentParser(description=__doc__.split("\n", 1)[0]).parse_args()
    return main_hunt()


if __name__ == "__main__":
    sys.exit(main())
