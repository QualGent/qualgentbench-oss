#!/usr/bin/env python3
"""Journey adversary check: can an agent be credited on a test case WITHOUT running it?

Hunt mode has had `scripts/adversary_check.py` between it and a published number since
the beginning; journey mode had nothing, and it paid for that — a report of
`observed: "anything at all"` on contacts-delete~seeded matched the blocking bug (a
derived blocking text of `A`, the contacts section index) and so earned recall AND
completion, because a blocked case completes on "fail + the blocking bug named".

Six synthetic guessers run through the real `journey.journey_verdict` against the real
corpus — offline, no device, no agent, no tokens:

  short-spray    quotes a one-character screen value on every case, with vague prose
  generic-spray  vague prose only, nothing quoted from any screen
  dead           writes the luckiest possible verdict and never touches the device
  brief-echo     quotes the BRIEF back: every phrase the test case put in quotation
                 marks and every capitalised word in its steps, with vague prose
  dialog-echo    quotes the platform's own crash/ANR sentence ("the app is not
                 responding", "<App> keeps stopping") with vague prose
  type-then-quote  TYPES every string brief-echo and dialog-echo would quote into the
                 device's text-entry tools, reads back DevLoop's acknowledgement
                 (`Set focused field to: '<text>'`, `Typed: '<text>'`, web_fill's
                 `{"ok": true, "value": …}`), then quotes it: the device "said" it.
                 Since QUA-2817 it also feeds each string to every other tool whose
                 reply hands an argument back — `mobile_open_url` (`Opened URL: <url>`),
                 `mobile_launch_app`, `mobile_terminate_app`, `mobile_device_logs`
                 (`<package_id> is not running …`) — the ECHO_ROSTER below
  argument-echo  (QUA-2819) hands the same strings to READ tools as arguments and quotes
                 the REFUSAL back: DevLoop's `Element '<text>' not found` from
                 mobile_tap_and_observe, `Unknown match '<match>'` from
                 mobile_await_element (the error-reply channel), and pydantic's
                 `input_value='<x>'` for a mistyped or missing argument (the
                 validation-error channel) — `ARG_ECHO_ROSTER` below. A read cannot be
                 marked `echo`, so what guards this is the scorer's refused-reply rule
                 (`journey._refused_reply`); the gate also runs it in codex's transcript
                 format and proves every roster entry, in both formats, earns with the
                 rule off

Each must be credited NOTHING: no bug found, no completion, anywhere — and through BOTH
report channels (QUA-2777): the `findings.yaml` file, and DevLoop's `mobile_report_result`
call, which the scorer reads as its lowest-precedence report source. A report source that
takes free text is only safe if it goes through the same matcher and grounds nothing on
its own reply; `CHANNELS` runs every guesser through each so that stays proven. The tool
carries one bug per call, so the guesser writes the strongest single call it can: every
quote joined into `actual`/`expected`, every filler sentence into `summary`. The last two are
QUA-2717: the first three write strings that identify nothing (below the `_evidence`
floor, or absent), which is a much weaker attack than writing a string that is genuinely
ON THE SCREEN and yet costs nothing to produce. `observed: "Standup"` — a title the
route itself types and the brief spells out — earned the blocking bug on
cal-switch-back-to-list, and `observed: "the app is not responding"` earned it on both
freeze exemplars, and neither was catchable here, so five exemplars shipped on a gate
that could not see them.

A SIXTH adversary, `symptom-spray`, is measured rather than gated. It writes the corpus's
own symptom vocabulary as prose with nothing quoted, and it EARNS credit — that route is
the only report a functional defect with no string to quote (a dropped field, a lost
reminder) can ever have, so closing it would take the honest report down with it. What
holds the line there is POLARITY, so that is what is asserted: the sprayer must pay a
false report on every clean episode it touches. A future change that let it keep the
recall and stop paying is the regression this catches.

The positive control is `honest`, whose reports quote the screen strings
`derive_journey.py` measured for each seeded defect; it must find them. `honest-text` is
the same agent with its prose removed, which measures whether the measured strings ALONE
still identify the defect — where they do not, the corpus is the finding (a defect whose
only signal was a string too short to be evidence), not the matcher. Those are printed,
never silently passed.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import re
import sys

import yaml

from qualgentbench import bugs, interactions, journey

# Prose with no defect-specific content: it must name no screen value and contain no
# symptom word from any defect in the corpus (`_no_symptom_leaks_into_the_filler_prose`
# below proves it, so a new defect cannot quietly hand the guessers a match).
VAGUE = "the screen did not look the way the test case describes"
VAGUER = "something about this felt off while I was working through it"
FILLER = (VAGUE, VAGUER)
# Adversaries that must earn exactly nothing, anywhere.
GUESSERS = ("short-spray", "generic-spray", "dead", "brief-echo", "dialog-echo",
            "type-then-quote", "argument-echo")
# Adversaries that DO earn credit and are measured against what it costs them.
PRICED = ("symptom-spray",)
MODES = GUESSERS + ("honest", "honest-text")
# The report channels every guesser is run through. `findings` is the file of record;
# `report_tool` is DevLoop's `mobile_report_result`, read only when no file exists.
CHANNELS = ("findings", "report_tool")


# ── synthesising an episode ────────────────────────────────────────────────────

def _mcp_lines(fmt: str, cid: str, tool: str, args: dict, reply: str,
               is_error: bool = False) -> str:
    """One MCP call and its reply as the agent records it: claude-code stream-json
    (`mcp__device__` name, `tool_result`, `is_error`) or codex `exec --json`
    (`item.completed` `mcp_tool_call`, bare name beside `server`, status `failed`)."""
    if fmt == "codex":
        return json.dumps({"type": "item.completed", "item": {
            "id": cid, "type": "mcp_tool_call", "server": "device", "tool": tool,
            "arguments": args, "error": None,
            "status": "failed" if is_error else "completed",
            "result": {"content": [{"type": "text", "text": reply}],
                       "structured_content": None}}})
    return "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": cid, "name": f"mcp__device__{tool}", "input": args}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": cid, "is_error": is_error,
             "content": [{"type": "text", "text": reply}]}]}})])


def _device(payload: str, i: int, fmt: str = "claude") -> str:
    """One device tool call plus its observation, carrying `payload` as visible text."""
    if fmt == "codex":
        return _mcp_lines(fmt, f"t{i}", "mobile_tap_and_observe", {"device": "d"}, payload)
    call = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}",
         "name": "mcp__device__mobile_tap_and_observe", "input": {"device": "d"}}]}})
    res = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{i}",
         "content": [{"type": "text", "text": payload}]}]}})
    return call + "\n" + res


# DevLoop-MCP's real argument-echo replies (tools/input.py, tools/web.py,
# tools/devices.py, tools/logs.py on `dev`), verbatim in shape: each hands the caller's
# string back. (roster name, tool, argument key, extra args, reply(text) -> str.) Every
# entry must be `echo` in `interactions.MCP_TOOL_RULES`; `echo_liveness` below proves
# each one still EARNS credit with its flag off, so a dead entry cannot pass for a guard.
_NEXT = ("Next: tap the Submit/Next button with mobile_tap_and_observe, "
         "or call mobile_observe_screen to confirm validation state.")


def _preview(text: str) -> str:
    return text[:50] + ("…" if len(text) > 50 else "")


ECHO_ROSTER = (
    ("type_text", "mobile_type_text", "text", {},
     lambda t: f"Set focused field to: {_preview(t)!r}\n" + _NEXT),
    ("type_text-append", "mobile_type_text", "text", {"replace_existing": False},
     lambda t: f"Typed: {_preview(t)!r}\n" + _NEXT),
    ("edit_field", "mobile_edit_field", "value", {},
     lambda t: f"Typed: {_preview(t)!r}\n" + _NEXT),
    ("web_fill", "mobile_web_fill", "value", {"selector": "input"},
     lambda t: json.dumps({"ok": True, "value": t}, indent=2)),
    # QUA-2817. The URL is handed back whole, and DevLoop took it as-is (no scheme check).
    ("open_url", "mobile_open_url", "url", {},
     lambda t: f"Opened URL: {t}\nNext: call mobile_observe_screen after the page/app loads."),
    ("launch_app", "mobile_launch_app", "package_id", {},
     lambda t: (f"Error executing tool mobile_launch_app: Failed to launch {t}: no launcher "
                "activity found. Make sure the package name is correct and the app is "
                "installed.")),
    ("terminate_app", "mobile_terminate_app", "package_id", {},
     lambda t: f"Terminated {t}"),
    ("device_logs", "mobile_device_logs", "package_id", {},
     lambda t: json.dumps({"source": "logcat", "matched": 0, "entries": [], "pids": [],
                           "filtered_by_pid": False, "recovered_pids": [],
                           "searched": "last 1600 lines",
                           "note": f"{t} is not running (pids: []), so entries were matched "
                                   "by name in the log text"}, indent=2)),
)
ECHO_NAMES = tuple(r[0] for r in ECHO_ROSTER)
# Replies DevLoop sends as a refused call (claude-code's `is_error`). The scorer grounds on
# them all the same, which is the point of including one.
_ERROR_REPLIES = {"launch_app"}


def _typed(text: str, i: int, roster: tuple[str, ...] = ECHO_NAMES) -> str:
    """`text` fed to every echoing tool in `roster`, each answered as DevLoop answers it,
    so every echo shape is attacked with every string."""
    lines = []
    for j, (name, tool, key, extra, reply) in enumerate(ECHO_ROSTER):
        if name not in roster:
            continue
        cid = f"k{i}-{j}"
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": cid, "name": f"mcp__device__{tool}",
             "input": {"device": "d", key: text, **extra}}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": cid, "is_error": name in _ERROR_REPLIES,
             "content": [{"type": "text", "text": reply(text)}]}]}}))
    return "\n".join(lines)


@contextlib.contextmanager
def _echo_flag_off(tool: str):
    """`tool`'s `echo` flag switched off for the duration — the scorer as it was before
    the tool was marked. Only ever used to prove an attack is LIVE (`echo_liveness`)."""
    rules = interactions.MCP_TOOL_RULES
    saved = rules[tool]
    rules[tool] = dataclasses.replace(saved, echo=False)
    try:
        yield
    finally:
        rules[tool] = saved


def echo_liveness(tasks: list) -> dict[str, int]:
    """Per `ECHO_ROSTER` entry: seeded defects type-then-quote earns through that ONE
    tool with its `echo` flag OFF. Every count must be > 0, or the entry attacks nothing
    and its 0 in the gate proves nothing — the same reason the honest control has to earn
    through the report tool. The refused-reply rule (QUA-2819) is off too, so each flag
    is proven on its own: `launch_app`'s echo is a refusal, which that rule also drops."""
    out: dict[str, int] = {}
    for name, tool, *_ in ECHO_ROSTER:
        with _echo_flag_off(tool), _refusal_rule_off():
            out[name] = sum(len(run(t, "type-then-quote", roster=(name,))["bugs_found"])
                            for t in tasks)
    return out


# ── the argument-echo guesser's roster (QUA-2819) ───────────────────────────────
# DevLoop's REFUSALS on read tools, verbatim in shape (src/devloop_mcp/tools/observe.py,
# tools/waits.py on the epic branch; the envelope and the validation text are FastMCP's
# and pydantic's own, see `_validation_refusal`). (roster name, channel, tool, argument
# key, extra args, reply(text) -> str.) None of these tools is `echo` — a read's answer
# is the screen — so the only guard is the refused-reply rule; `arg_echo_liveness`
# proves each entry EARNS with that rule off, in both transcript formats.
_RETRY = " Please fix the issue and try again."


def _validation_refusal(tool: str, fields: dict, args: dict) -> str:
    """The exact text FastMCP returns when pydantic refuses `args` for `tool`: the
    argument model is `<tool>Arguments`, and the error is wrapped in the same envelope
    as a tool error. Built with pydantic itself, so its `input_value` rendering (a long
    string truncated in the MIDDLE, a missing field printing the whole argument dict)
    is the real one, not a guess at it."""
    import pydantic
    model = pydantic.create_model(f"{tool}Arguments", **{k: (t, ...) for k, t in fields.items()})
    try:
        model.model_validate(args)
    except pydantic.ValidationError as e:
        return f"Error executing tool {tool}: {e}"
    raise AssertionError(f"{tool}: {args} validated — not a refusal")


ARG_ECHO_ROSTER = (
    ("tap_not_found", "error", "mobile_tap_and_observe", "element_text", {},
     lambda t: (f"Error executing tool mobile_tap_and_observe: Element {t!r} not found. "
                "Visible: 'Contacts', 'Favorites'. If the target is an unlabeled icon, call "
                "mobile_observe_screen, read its coordinates from the screenshot, then use "
                "x+y instead." + _RETRY)),
    ("await_unknown_match", "error", "mobile_await_element", "match", {"text": "Contacts"},
     lambda t: (f"Error executing tool mobile_await_element: Unknown match {t!r}. "
                "Use 'contains' or 'equals'." + _RETRY)),
    # A string where mobile_tap wants an integer coordinate: `input_value='<x>'`.
    ("tap_int_parsing", "validation", "mobile_tap", "x", {"y": 1},
     lambda t: _validation_refusal("mobile_tap", {"device": str, "x": int, "y": int},
                                   {"device": "d", "x": t, "y": 1})),
    # The coordinates left out entirely: `input_value={'device': 'd', 'text': '<x>'}`.
    ("hit_test_missing", "validation", "mobile_hit_test", "text", {},
     lambda t: _validation_refusal("mobile_hit_test", {"device": str, "x": int, "y": int},
                                   {"device": "d", "text": t})),
)
ARG_ECHO_NAMES = tuple(r[0] for r in ARG_ECHO_ROSTER)
FORMATS = ("claude", "codex")


def _arg_echoed(text: str, i: int, roster: tuple[str, ...], fmt: str) -> str:
    """`text` handed to every read tool in `roster` as an argument, each refused as
    DevLoop refuses it (claude `is_error`, codex status `failed`)."""
    return "\n".join(
        _mcp_lines(fmt, f"a{i}-{j}", tool, {"device": "d", key: text, **extra}, reply(text),
                   is_error=True)
        for j, (name, _chan, tool, key, extra, reply) in enumerate(ARG_ECHO_ROSTER)
        if name in roster)


@contextlib.contextmanager
def _refusal_rule_off():
    """The scorer before QUA-2819: a refused call's reply counts as device text. Only
    ever used to prove an attack is LIVE."""
    saved = journey._refused_reply
    journey._refused_reply = lambda reply: False
    try:
        yield
    finally:
        journey._refused_reply = saved


def arg_echo_liveness(tasks: list) -> dict[tuple[str, str], int]:
    """Per (`ARG_ECHO_ROSTER` entry, transcript format): seeded defects argument-echo
    earns through that ONE refusal shape with the refused-reply rule OFF. Every count
    must be > 0, or the entry attacks nothing and its 0 in the gate proves nothing."""
    out: dict[tuple[str, str], int] = {}
    with _refusal_rule_off():
        for name in ARG_ECHO_NAMES:
            for fmt in FORMATS:
                out[(name, fmt)] = sum(
                    len(run(t, "argument-echo", arg_roster=(name,), fmt=fmt)["bugs_found"])
                    for t in tasks)
    return out


def _text(s: str) -> str:
    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text", "text": s}]}})


def _usable(texts) -> list[str]:
    """Strings carrying a `<placeholder>` are dropped: they are normalised truth, not
    anything a screen ever showed."""
    return [t for t in (texts or []) if t and "<" not in t and journey._evidence(t)]


def _quotes(spec: dict, bug_id: str) -> list[str]:
    """The screen strings the corpus MEASURED for one active defect that an honest
    report can put under `observed` — things the SEEDED build actually showed.

    All three of the blocking bug's observable lists, in the order `match_report`
    reads them. `echo_texts` is in here on purpose: a brief noun the route types and
    the platform's crash dialog really are on the screen, and an honest agent really
    does quote them — the control has to prove that still earns the bug. It does,
    because the control also reads them back off the device (see `_episode`), which is
    exactly the grounding that route now requires."""
    out: list[str] = []
    for s in spec.get("side") or []:
        if s.get("bug") == bug_id:
            out += [s.get("marker") or ""] + list(s.get("texts") or [])
    if bug_id == spec.get("blocking"):
        # A death case has no screen diff: its evidence is the exception the case names
        # and the platform dialog (journey.crash_evidence) rather than the diff lists.
        out += (list(spec.get("blocking_texts") or []) + list(spec.get("crash_texts") or [])
                + list(spec.get("echo_texts") or []))
    return _usable(out)


def _absences(spec: dict, bug_id: str) -> list[str]:
    """What the CLEAN build showed here and this one lost. A defect that manifests as
    an ABSENCE (a repeat that stopped repeating, a field that stopped rendering) leaves
    nothing on the seeded screen to quote, so the honest report names the value it
    EXPECTED and did not get — the field the brief's own example puts it in."""
    if bug_id != spec.get("blocking"):
        return []
    return _usable(spec.get("absence_texts"))


_QUOTED_RE = re.compile(r'"([^"]{2,})"')
_CAPS_RE = re.compile(r"\b([A-Z][A-Za-z0-9'’\-]+)\b")


def brief_strings(spec: dict) -> list[str]:
    """Everything a brief-echoing agent can write with the app never started: the phrases
    the test case puts in quotation marks (`Enter the title "Standup"`) and the
    capitalised words in its steps (`Event`, `Save`, `Search`). Read off the same three
    fields `journey.brief()` composes and nothing else — this adversary is not allowed
    to see the route, the truth file or the defect list."""
    blob = " \n ".join([str(spec.get("name") or ""), str(spec.get("expected_outcome") or "")]
                       + [str(s) for s in (spec.get("steps") or [])])
    out = _QUOTED_RE.findall(blob) + _CAPS_RE.findall(blob)
    seen, uniq = set(), []
    for t in out:
        k = journey._norm(t)
        if k and k not in seen and journey._evidence(t):
            seen.add(k)
            uniq.append(t)
    return uniq


def dialog_strings(app_name: str) -> list[str]:
    """The platform's own crash and ANR wording, bare and qualified with the app's
    display name — which the brief hands over in its first line. No device needed, and
    identical on every death case in the corpus, which is the whole point."""
    wordings = list(journey._CRASH_DIALOG_TEXTS) + list(journey._ANR_DIALOG_TEXTS)
    out = list(wordings) + [f"the app {w}" for w in journey._ANR_DIALOG_TEXTS]
    if app_name.strip():
        out += [f"{app_name.strip()} {w}" for w in wordings]
    return [t for t in out if journey._evidence(t)]


def _spray(quotes: list[str]) -> list[dict]:
    """One bug entry per string, alternating the two filler descriptions. The string is
    written into EVERY field an entry has that is not the claim — `observed` (the
    sighting route), `expected` (the absence route) and `screen` (a label the agent
    picks for itself) — so the only thing this adversary is not doing is describing a
    misbehaviour. That is the point: it isolates the QUOTE as the variable. An
    adversary that writes defect vocabulary as prose is a different attack and is
    measured separately (`symptom-spray`, in `PRICED`)."""
    return [{"step": i, "screen": q, "observed": q, "expected": q,
             "description": FILLER[i % len(FILLER)]}
            for i, q in enumerate(quotes, 1)]


def _symptom(spec: dict, bug_id: str) -> str:
    """The defect's own longest symptom phrase — the vocabulary an honest tester writes
    when the misbehaviour has no string to quote (a dropped field, a lost reminder)."""
    syms = ((spec.get("defects") or {}).get(bug_id) or {}).get("symptoms") or []
    return max(syms, key=len) if syms else ""


def _episode(spec: dict, mode: str, app_name: str = "",
             roster: tuple[str, ...] = ECHO_NAMES,
             arg_roster: tuple[str, ...] = ARG_ECHO_NAMES,
             fmt: str = "claude") -> tuple[str, str]:
    """(transcript, findings file) for one synthetic agent on one task. ``roster`` names
    the `ECHO_ROSTER` entries type-then-quote feeds its strings to (default: all);
    ``arg_roster`` the `ARG_ECHO_ROSTER` refusals argument-echo provokes; ``fmt`` the
    transcript format of every device call (`claude` | `codex`)."""
    expected = str(spec.get("expected") or "PASS").lower()
    active = list(spec.get("active_bugs") or [])
    payloads: list[str] = ["the app is on screen"]
    entries: list[dict] = []
    typed: list[str] = []
    refused: list[str] = []

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
    elif mode == "brief-echo":
        # Everything the test case spelled out for it, quoted back as if seen. The
        # device payload stays the generic one: this agent launched nothing.
        verdict = "fail"
        entries = _spray(brief_strings(spec))
    elif mode == "dialog-echo":
        verdict = "fail"
        entries = _spray(dialog_strings(app_name))
    elif mode == "type-then-quote":
        # QUA-2805: the device "answers" with every string because the agent typed it
        # there first. Its one screen read is the generic payload, so nothing it quotes
        # was ever SHOWN; only the text-entry echo carries it.
        verdict = "fail"
        typed = list(dict.fromkeys(brief_strings(spec) + dialog_strings(app_name)))
        entries = _spray(typed)
    elif mode == "argument-echo":
        # QUA-2819: the same strings, handed to READ tools as arguments; the device
        # "answers" with each one inside a refusal. Its one screen read is the generic
        # payload, so nothing it quotes was ever SHOWN.
        verdict = "fail"
        refused = list(dict.fromkeys(brief_strings(spec) + dialog_strings(app_name)))
        entries = _spray(refused)
    elif mode == "symptom-spray":
        # The corpus's own symptom vocabulary for every defect the app declares —
        # readable straight out of the public test-case file — with nothing quoted.
        verdict = "fail"
        entries = [{"step": i, "screen": "the app", "observed": "", "expected": "",
                    "description": sym}
                   for i, sym in enumerate(sorted({s for d in (spec.get("defects") or {}).values()
                                                   for s in (d.get("symptoms") or []) if s}), 1)]
    else:
        verdict = expected
        for i, bug_id in enumerate(active, 1):
            quotes = _quotes(spec, bug_id)
            absent = _absences(spec, bug_id)
            observed = quotes[0] if quotes else ""
            if observed:
                # The honest agent read this off the screen, so the device answered
                # with it. That is what grounds the `echo_texts` route.
                payloads.append(observed)
            entries.append({"step": i, "screen": "the app", "observed": observed,
                            "expected": absent[0] if absent else "",
                            "description": VAGUE if mode == "honest-text"
                            else (_symptom(spec, bug_id) or VAGUE)})
    lines = [_device(p, i, fmt) for i, p in enumerate(payloads)] + \
        [_typed(t, i, roster) for i, t in enumerate(typed)] + \
        [_arg_echoed(t, i, arg_roster, fmt) for i, t in enumerate(refused)]
    return "\n".join(lines) + "\n", yaml.safe_dump({"verdict": verdict, "bugs": entries},
                                                   sort_keys=False)


def _report_tool_call(findings: str, spec: dict) -> str:
    """The same report as ONE `mobile_report_result` call (QUA-2777): the verdict as its
    status and every entry folded into the one bug the tool can carry — quotes joined
    into `actual` and `expected`, prose into `summary` — the strongest single call the
    same guess can make. The server's reply echoes the arguments back, as DevLoop's
    does, which is exactly the text that must never ground a quote. `code_investigation`
    is filled so the call is one DevLoop would accept."""
    doc = yaml.safe_load(findings) or {}
    entries = [b for b in doc.get("bugs") or [] if isinstance(b, dict)]
    if spec.get("blocking") in (spec.get("active_bugs") or []) and entries:
        entries = entries[:1]           # honest: one call = one bug, the blocking one first
    status = "FAIL" if str(doc.get("verdict")).lower() == "fail" else "PASS"

    def _join(key: str) -> str:
        return " ".join(dict.fromkeys(str(b.get(key) or "") for b in entries if b.get(key)))

    args = {"device": "d", "status": status, "flow": str(spec.get("name") or ""),
            "summary": _join("description") or "done", "steps": [],
            "failure_step": str(entries[0].get("step") or "") if entries else "",
            "expected": _join("expected"), "actual": _join("observed"),
            "code_investigation": "no source available" if status == "FAIL" else None}
    reply = f"STATUS: {status}\n{args['summary']}\nActual: {args['actual']}\nExpected: {args['expected']}"
    call = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "report", "name": "mcp__device__mobile_report_result",
         "input": {k: v for k, v in args.items() if v is not None}}]}})
    res = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "report",
         "content": [{"type": "text", "text": reply}]}]}})
    return call + "\n" + res + "\n"


def run(task, mode: str, channel: str = "findings",
        roster: tuple[str, ...] = ECHO_NAMES,
        arg_roster: tuple[str, ...] = ARG_ECHO_NAMES, fmt: str = "claude") -> dict:
    """One synthetic episode through the real scorer. The spec is copied, so the oracle
    stays exactly as the corpus defines it — offline, a `db:` oracle is unevaluated,
    which is the same thing the runner reports when the device cannot answer.
    ``channel`` picks how the report reaches the scorer (`CHANNELS`); ``fmt`` the
    transcript format of the device calls (the report tool's call stays claude's)."""
    spec = dict(task.bug_spec)
    transcript, findings = _episode(spec, mode, task.app_name, roster, arg_roster, fmt)
    spec["tooling"] = "mcp"
    if channel == "report_tool":
        # `run` writes the spec back onto the task, so an earlier findings-channel run
        # of ANOTHER mode (the honest control) would otherwise still be on disk here.
        spec.pop("findings_file", None)
        transcript += _report_tool_call(findings, spec)
    else:
        spec["findings_file"] = findings
    task.bug_spec = spec
    return journey.journey_verdict(transcript, "synthetic", task).metrics


# ── the gate ───────────────────────────────────────────────────────────────────

def _tasks(app_ids: list[str] | None, version: str) -> list:
    out = []
    for suite in bugs.load_apps():
        app_id = str(suite["app"].get("id") or "")
        if not journey.has_cases(app_id) or (app_ids and app_id not in app_ids):
            continue
        for t in journey.journey_tasks(suite):
            spec = t.bug_spec
            if spec.get("version") != version:
                continue
            # A case with no `bugs:` has no seeded arm; its clean arm is still a clean
            # arm, and that is where a sprayer pays.
            if version == "seeded" and not spec.get("active_bugs"):
                continue
            out.append(t)
    return out


def _seeded_tasks(app_ids: list[str] | None) -> list:
    return _tasks(app_ids, "seeded")


def _no_symptom_leaks_into_the_filler_prose(tasks: list) -> list[str]:
    """The FILLER prose — the two constants every zero-credit guesser attaches to its
    entries — must be defect-free BY CONSTRUCTION. If a new defect lists a symptom word
    that appears in it, the gate would start failing for a reason that has nothing to do
    with the scorer, so say so instead.

    Scope, and why `symptom-spray` is not a contradiction (QUA-2717): this is an
    invariant on the FILLER, not on every adversary. Holding it over all prose is what
    kept the roster from ever containing the one attack most likely to work — an
    adversary whose whole method is writing defect vocabulary cannot draw its prose from
    strings that are required to carry none. `symptom-spray` therefore takes its prose
    from the corpus `defects` map instead of from `FILLER`, and sits in `PRICED` rather
    than `GUESSERS`: it is not asserted to earn zero (it cannot — that route is the only
    report a dropped field ever has), it is asserted to PAY. The two coexist because
    they are claims about different things: the filler must identify nothing, and the
    symptom sprayer must not get its recall for free."""
    hay = " ".join(FILLER).lower()
    leaks = []
    for task in tasks:
        for bug_id, d in (task.bug_spec.get("defects") or {}).items():
            for sym in d.get("symptoms") or []:
                if sym and journey._word(sym.lower(), hay):
                    leaks.append(f"{bug_id}: symptom {sym!r} appears in the filler prose")
    return sorted(set(leaks))


def _price(tasks: list, mode: str) -> tuple[int, int, list[str]]:
    """What an adversary pays on the CLEAN arm. Nothing is active on a clean build, so
    every report there is false by definition — returns (clean episodes with ≥1 false
    report, clean episodes, the ones that got away)."""
    paid = free = 0
    quiet: list[str] = []
    for task in tasks:
        m = run(task, mode)
        if int(m.get("false_reports") or 0) >= 1:
            paid += 1
        else:
            free += 1
            quiet.append(task.id)
    return paid, paid + free, quiet


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--app", help="comma-separated app ids (default: every app with test cases)")
    args = ap.parse_args()
    app_ids = [a.strip() for a in args.app.split(",")] if args.app else None

    tasks = _seeded_tasks(app_ids)
    if not tasks:
        print("FAIL: no seeded journey tasks found — nothing was checked")
        return 1

    leaks = _no_symptom_leaks_into_the_filler_prose(tasks)
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
            quotable = _quotes(spec, bug_id) or _absences(spec, bug_id)
            where = uncreditable if quotable else unquotable
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
        print("\nFAIL: the guessers' filler prose is no longer defect-free:")
        for line in leaks:
            print(f"  {line}")

    # ── the second report channel (QUA-2777) ──────────────────────────────────
    # The same guesses, delivered as a `mobile_report_result` call instead of a file.
    # Every guesser must still earn nothing; the honest control must still be credited
    # through it, or the channel is dead and this block proves nothing.
    tool_leaked: dict[str, list[str]] = {m: [] for m in GUESSERS}
    tool_honest = tool_present = 0
    for task in tasks:
        for mode in GUESSERS:
            m = run(task, mode, "report_tool")
            if m.get("bugs_found") or m.get("completed") is True:
                tool_leaked[mode].append(
                    f"{task.id}: {len(m.get('bugs_found') or [])} bug(s) credited"
                    f"{' + completion' if m.get('completed') is True else ''}")
        m = run(task, "honest", "report_tool")
        tool_honest += len(m.get("bugs_found") or [])
        tool_present += 1 if m.get("bugs_present") else 0
    print(f"\nREPORT TOOL: guessers via mobile_report_result — "
          + ", ".join(f"{mode} {len(tool_leaked[mode])}" for mode in GUESSERS)
          + f" episode(s) credited; honest (one bug per call) credited {tool_honest} "
          f"defect(s) over {tool_present} seeded episode(s)")
    for mode in GUESSERS:
        if tool_leaked[mode]:
            ok = False
            print(f"\nFAIL: '{mode}' earned credit through the report tool on "
                  f"{len(tool_leaked[mode])} episode(s):")
            for line in tool_leaked[mode][:12]:
                print(f"  {line}")
    if not tool_honest:
        ok = False
        print("\nFAIL: the honest control earned nothing through the report tool — the "
              "channel is dead and the block above proves nothing")

    # ── the echo attack is live (QUA-2817) ──────────────────────────────────────
    # type-then-quote's 0 above is only a guard if each echoing tool it uses WOULD pay
    # without its `echo` flag. Prove it per tool, or the roster entry is dead weight.
    for name, tool, *_ in ECHO_ROSTER:
        if not interactions.mcp_echoes_argument(tool):
            ok = False
            print(f"\nFAIL: {tool} is in the type-then-quote roster but not marked `echo` "
                  f"in interactions.MCP_TOOL_RULES")
    live = echo_liveness(tasks)
    print("\nECHO LIVENESS: type-then-quote through one tool with its echo flag OFF would "
          "earn — " + ", ".join(f"{n} {k}" for n, k in live.items()) + " seeded defect(s)")
    for name, k in live.items():
        if not k:
            ok = False
            print(f"  FAIL: '{name}' earns nothing even unmarked — the attack is dead and "
                  f"its 0 above proves nothing")

    # ── argument echoes in refusals (QUA-2819) ──────────────────────────────────
    # The table above ran argument-echo in claude-code's format through both report
    # channels. Run it again in codex's, per channel of refusal, and prove each refusal
    # shape would pay in BOTH formats without the refused-reply rule.
    codex_leaked: list[str] = []
    for task in tasks:
        for chan in ("error", "validation"):
            names = tuple(r[0] for r in ARG_ECHO_ROSTER if r[1] == chan)
            m = run(task, "argument-echo", arg_roster=names, fmt="codex")
            if m.get("bugs_found") or m.get("completed") is True:
                codex_leaked.append(f"{task.id} ({chan}): {len(m.get('bugs_found') or [])} "
                                    f"bug(s){' + completion' if m.get('completed') is True else ''}")
    print(f"\nARGUMENT ECHO: argument-echo in codex's transcript format credited "
          f"{len(codex_leaked)} episode(s) (error-reply and validation-error channels)")
    if codex_leaked:
        ok = False
        for line in codex_leaked[:12]:
            print(f"  FAIL: {line}")
    arg_live = arg_echo_liveness(tasks)
    print("ARGUMENT ECHO LIVENESS: with the refused-reply rule OFF, one refusal shape would "
          "earn — " + ", ".join(f"{n}/{f} {k}" for (n, f), k in arg_live.items())
          + " seeded defect(s)")
    for (name, fmt), k in arg_live.items():
        if not k:
            ok = False
            print(f"  FAIL: '{name}' ({fmt}) earns nothing even with the rule off — the "
                  f"attack is dead and its 0 above proves nothing")

    # ── the priced adversaries ────────────────────────────────────────────────
    # These EARN credit, and that is not a bug to be fixed: symptom prose with nothing
    # quoted is the only report a functional defect with no string to quote ever has,
    # so a matcher that refused it would refuse the honest report too (12 of the 39
    # seeded defects below are exactly that shape). What is asserted instead is the
    # PRICE. Nothing is active on a clean build, so every report there is false — a
    # sprayer that buys recall must buy a dirty night with it, on EVERY clean episode
    # it touches. Recall without that price is the regression.
    clean = _tasks(app_ids, "clean")
    for mode in PRICED:
        found = present = 0
        for task in tasks:
            m = run(task, mode)
            present += len(list(m.get("bugs_present") or []))
            found += len(list(m.get("bugs_found") or []))
        paid, n_clean, quiet = _price(clean, mode)
        pct = f"{100 * paid / n_clean:.0f}%" if n_clean else "—"
        print(f"\nPRICED: '{mode}' earned {found}/{present} seeded defect(s) on prose alone "
              f"and paid a false report on {paid}/{n_clean} clean episode(s) ({pct}). "
              f"That is the trade a catch rate on this corpus has to be read against — "
              f"never the catch rate alone.")
        if not n_clean:
            ok = False
            print(f"  FAIL: no clean episodes to price '{mode}' against")
        elif quiet:
            ok = False
            print(f"  FAIL: '{mode}' got {len(quiet)} clean episode(s) for free — it keeps "
                  f"the recall and stops paying for it:")
            for line in quiet[:12]:
                print(f"    {line}")

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
        print(f"\nPASS: all {len(GUESSERS)} guessers earned 0 bugs and 0 completions over "
              f"{len(tasks)} seeded episode(s), through the findings file AND the report "
              f"tool — including the four that quote real screen "
              f"text they never had to look at (one of them after typing it into "
              f"the device, one after handing it to a read tool and quoting the "
              f"refusal, in both transcript formats); honest found "
              f"{tot['honest'][0]}/{tot['honest'][1]} and every defect it missed has no "
              f"quotable evidence in the corpus; every priced adversary paid in full")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
