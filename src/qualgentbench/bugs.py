"""Seeded-bug benchmark: suite loading and scoring. Results expose a scalar
``reward`` plus per-step ``criteria`` so the same runs can drive RL later."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from . import pricing, submission
from .contamination import scan as contamination_scan
from .interactions import KINDS as _INTERACTION_KINDS
from .task import BenchmarkTask
from .result import VerifierResult
from .transcript import TranscriptParser

_BENCHMARKS_DIR = Path(__file__).parent / "data" / "benchmarks"

# ── Scoring constants (see docs/scoring.md) ──────────────────
# reward(bug) = W(tier) × correct × efficiency; `correct` means the agent
# exercised the feature, reported it broken, and any bug oracle confirms it.
_TIER_WEIGHTS = {"l1": 1.0, "l2": 3.0, "l3": 6.0, "l4": 10.0}
_UNTIERED_WEIGHT = 1.0          # a spec without a tier can't out-earn a tiered one
_EFFICIENCY_FLOOR = 0.5         # a slow correct find still keeps half the weight
_FP_PENALTY = 3.0               # roughly the median bug weight
_EARLINESS_BONUS = 0.035        # guided-mode speed credit
_SPEED_WEIGHT = 0.035           # hunt-mode speed weight
# Cost of one false report, priced so blanket "everything deviates" loses to
# honestly reporting areas as blocked.
_HUNT_FP_PENALTY = 0.25
_MIN_CALLS_PER_CLAIM = 3        # device work required between banked verdicts

# How budget was charged when an episode ran, stamped into every result. Bump
# whenever the meaning of a charged step changes — the derivation scripts refuse
# to mix versions. v3 = one interaction: a tap, a swipe, a text entry, a read.
BUDGET_ACCOUNTING = "interaction-v3"


def tier_weight(tier: str | None) -> float:
    return _TIER_WEIGHTS.get(str(tier or "").strip().lower(), _UNTIERED_WEIGHT)


def _efficiency(ref_steps: int | None, actual_steps: int) -> float:
    """Bounded speed bonus, applied ONLY to a correct find: clamp(ref/actual, 0.5, 1).
    Speed can never let a wrong answer outscore a right one."""
    if not ref_steps or actual_steps <= 0:
        return 1.0
    return round(max(_EFFICIENCY_FLOOR, min(1.0, ref_steps / actual_steps)), 4)


def _earliness(actual_steps: int, budget: int | None) -> float:
    """Fraction of the step budget left unused, 0..1. The budget is the
    denominator on purpose — it is measured, not authored guesswork."""
    if not budget or budget <= 0 or actual_steps <= 0:
        return 0.0
    return round(max(0.0, min(1.0, 1.0 - actual_steps / budget)), 4)


def speed_factor(actual_steps: int, budget: int | None) -> float:
    """Hunt-mode slowness discount in [1 - W, 1]; W sits below the smallest
    quality gap so speed never outranks substance. No budget = no discount;
    zero steps takes the FULL discount so a missing measurement never wins."""
    if not budget or budget <= 0:
        return 1.0
    if actual_steps <= 0:
        return round(1.0 - _SPEED_WEIGHT, 4)
    return round(1.0 - _SPEED_WEIGHT * (1.0 - _earliness(actual_steps, budget)), 4)


def earliness_multiplier(actual_steps: int, budget: int | None) -> float:
    """Guided speed credit: a pure bonus, capped below the smallest tier-weighted
    recall gap in the corpus, so speed is a tie-breaker and never beats substance."""
    return round(1.0 + _EARLINESS_BONUS * _earliness(actual_steps, budget), 4)


def load_suite(path: Path) -> dict[str, Any]:
    """Load one app's benchmark spec (app meta + exploration + tasks)."""
    return yaml.safe_load(Path(path).read_text())


def hidden_resolver(features: list[dict]):
    """Map an agent's catch-all area (`other`, `other_1`, ...) onto the ONE hidden
    feature whose every probe keyword appears in the entry's own words. Hidden
    features are seeded defects the brief does not name — the agent has to notice
    them; a report that names no probe, or matches two, stays unmapped."""
    hidden = [f for f in features if f.get("hidden") and f.get("probe")]
    if not hidden:
        return None

    def resolve(area: str, entry: dict) -> str | None:
        if not area.lower().startswith("other"):
            return None
        text = " ".join(str(entry.get(k) or "") for k in ("actual", "expected", "expect")).lower()
        hits = [f["id"] for f in hidden
                if all(str(p).lower() in text for p in f["probe"])]
        return hits[0] if len(hits) == 1 else None
    return resolve


def load_apps(benchmarks_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load every registered app spec, sorted by difficulty then id."""
    order = {"easy": 0, "medium": 1, "hard": 2}
    specs = [
        yaml.safe_load(p.read_text())
        for p in sorted((benchmarks_dir or _BENCHMARKS_DIR).glob("*.yaml"))
    ]
    return sorted(
        specs,
        key=lambda s: (order.get(str(s.get("app", {}).get("difficulty", "")), 9),
                       str(s.get("app", {}).get("id", ""))),
    )


def suite_tasks(suite: dict[str, Any]) -> list[BenchmarkTask]:
    """Build in-memory BenchmarkTasks (with bug_spec) for every task in the suite."""
    app = suite["app"]
    tasks: list[BenchmarkTask] = []
    for t in suite.get("tasks", []):
        bug_spec = {
            "id": t.get("bug_id", t["id"]),
            # "bug" (self-report detection) | "clean" (programmatic device-state oracle)
            "type": str(t.get("type", "bug")).lower(),
            "expected_verdict": str(t.get("expected_verdict", "FAIL")).upper(),
            "symptom_keywords": [s.lower() for s in (t.get("symptom_keywords") or [])],
            "flow_steps": t.get("flow_steps") or [],
            "oracle": t.get("oracle"),                        # clean: completion; bug: confirms the bug
            "difficulty": str(t.get("difficulty", "")),
            "optimal_steps": t.get("optimal_steps"),          # reference minimum steps
            "tier": str(t.get("tier", "")),                   # L1..L4 → W(tier) in the reward
            "ref_steps": t.get("ref_steps") or t.get("optimal_steps"),   # efficiency reference
            # Hard per-task budget; crossing it kills the episode. A reviewed
            # number in the spec, never a formula.
            "step_budget": t.get("step_budget"),
            # Staged content pushed before launch; app-level so every task inherits it.
            "device_setup": suite.get("device_setup"),
            # Shared-storage dirs the app writes to, wiped before every episode —
            # neither `pm clear` nor an uninstall touches /sdcard.
            "shared_storage": suite.get("shared_storage"),
        }
        tasks.append(BenchmarkTask(
            id=str(t["id"]),
            name=str(t.get("title") or t["id"]),
            instruction=str(t.get("instruction") or "").strip() + "\n",
            app_file_id="",                       # local/HF APK, not a backend file id
            app_name=str(app.get("name") or app.get("id") or "Buggy Notebook"),
            platform=str(app.get("platform") or "android"),
            bundle_id=str(app.get("package") or ""),
            bug_spec=bug_spec,
        ))
    return tasks


def exploration_task(suite: dict[str, Any]) -> BenchmarkTask:
    """Build the single open-ended bug-hunt task (the default mode). Ground-truth
    bugs ride along in bug_spec for scoring but are NEVER put in the instruction."""
    app = suite["app"]
    ex = suite["exploration"]
    # Tiers are merged in from the guided task list so hunt recall is
    # tier-weighted on the same scale as guided.
    tier_by_bug = {
        str(t.get("bug_id")): str(t.get("tier", ""))
        for t in suite.get("tasks", []) if t.get("bug_id")
    }
    features = [
        {"id": str(f["id"]), "state": str(f.get("state", "ok")).lower(),
         "bug_id": f.get("bug_id"),
         "tier": tier_by_bug.get(str(f.get("bug_id")), ""),
         # Optional device-evidence keywords: a `broken` verdict only counts if
         # one was seen in the agent's device interactions.
         "probe": [str(p).lower() for p in (f.get("probe") or [])],
         "hidden": bool(f.get("hidden"))}
        for f in ex.get("features", [])
    ]
    total_bugs = sum(1 for f in features if f["state"] == "broken")
    # Complexity defaults to one unit per feature; a yaml `complexity` overrides.
    complexity = int(ex.get("complexity") or len(features))
    return BenchmarkTask(
        id=str(ex.get("id", "explore-bugs")),
        name=str(ex.get("title") or "Find bugs"),
        instruction=str(ex.get("instruction") or "").strip() + "\n",
        app_file_id="",
        app_name=str(app.get("name") or "Buggy Notebook"),
        platform=str(app.get("platform") or "android"),
        bundle_id=str(app.get("package") or ""),
        bug_spec={
            "mode": "explore",
            "app_id": str(app.get("id", "")),
            "difficulty": str(app.get("difficulty", "")),
            "complexity": complexity,
            # Explicit per-app budget when the spec carries one; the feature-count
            # fallback correlates poorly with real cost.
            "step_budget": int(ex.get("step_budget") or 10 * complexity),
            "features": features,
            "total_bugs": total_bugs,
            "device_setup": suite.get("device_setup"),
            "shared_storage": suite.get("shared_storage"),
        },
    )


def _count_tool_calls(transcript: str) -> int:
    """Total tool calls issued, across BOTH transcript shapes — reading only
    Claude's shape silently zeroed the count for codex episodes."""
    n = 0
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":                  # Claude stream-json
            for b in ev.get("message", {}).get("content", []):
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    n += 1
        elif ev.get("type") == "item.completed":           # Codex (item.started dups)
            if (ev.get("item") or {}).get("type") in ("mcp_tool_call", "command_execution"):
                n += 1
    return n


def _report_summary(parser: TranscriptParser) -> str:
    """The agent's OFFICIAL findings: the mobile_report_result input only,
    lower-cased — musing about a bug mid-run earns no credit."""
    for e in parser.events():
        if "mobile_report_result" in e.name:
            return json.dumps(e.input).lower()
    return ""


_BROKEN_WORDS = ("broken", "bug", "fail", "bad", "not work", "doesn't work")
_OK_WORDS = ("ok", "pass", "work", "good", "fine", "correct")


def _parse_result_line(summary: str, feature_ids: list[str]) -> dict[str, str]:
    """Parse `RESULT: feat=ok, feat=broken, …` for known feature ids — exact-label
    scoring, immune to prose phrasing. Returns {feature_id: 'ok'|'broken'}."""
    seg = summary
    m = re.search(r"result\s*:?(.*)", summary, re.DOTALL)
    if m:
        seg = m.group(1)

    def _norm(val: str) -> str | None:
        # Accepts both vocabularies. Deviation words first, so
        # "not_as_specified" cannot be read as "as_specified".
        if any(w in val for w in _DEVIATES) or any(w in val for w in _BROKEN_WORDS):
            return "broken"
        if any(w in val for w in _AS_SPECIFIED) or any(w in val for w in _OK_WORDS):
            return "ok"
        return None

    out: dict[str, str] = {}
    for fid in feature_ids:
        # `_` stays in the class so values like "as_specified" capture whole.
        fm = re.search(rf"{re.escape(fid)}\s*=\s*([a-z_\-' ]+)", seg)
        if fm:
            norm = _norm(fm.group(1))
            if norm:
                out[fid] = norm
    return out


def _last_result_segment(text: str) -> str:
    """Text from the LAST 'RESULT:' marker on — agents narrate mid-run, and the
    final RESULT line is the official verdict."""
    matches = list(re.finditer(r"result\s*:", text))
    return text[matches[-1].start():] if matches else text


# Shell-tool names across the supported CLIs — matching only "Bash" made the
# raw-mode counters blind to codex.
_SHELL_TOOLS = ("bash", "command_execution", "shell", "local_shell")

# THE definition of "an adb invocation", shared with the adapters' budget hook —
# two counters for one quantity always drift.
_ADB_INVOCATION = re.compile(r"(?:^|[|;&]|\s)adb\s")

_SUBMISSION_FILE = submission.FILENAME
# `cat > findings.yaml <<'EOF' … EOF` — the shell route to the same file.
_HEREDOC_RE = re.compile(
    r"<<-?\s*['\"]?(?P<tag>[A-Za-z_][A-Za-z0-9_]*)['\"]?\s*\n(?P<body>.*?)\n\s*(?P=tag)",
    re.DOTALL)


def _count_adb(text: str) -> int:
    return len(_ADB_INVOCATION.findall(text))


def _shell_command(e) -> str:
    """The command as the SHELL received it, never the serialized tool input —
    json.dumps escapes newlines, which breaks the adb regex. The budget hook
    reads the raw command, so the scorer must too or the two counts drift."""
    inp = getattr(e, "input", None)
    if isinstance(inp, dict):
        for key in ("command", "cmd", "script"):
            val = inp.get(key)
            if isinstance(val, str):
                return val
    return getattr(e, "input_str", "")


def _is_shell_event(e) -> bool:
    return any(t in e.name.lower() for t in _SHELL_TOOLS)


def _bash_adb_events(parser: TranscriptParser) -> list:
    """Shell calls that drive the device via adb — the raw arm's device signal."""
    return [
        e for e in parser.events()
        if _is_shell_event(e) and "adb" in e.input_str
    ]


def _device_actions(parser: TranscriptParser, tooling: str) -> int:
    """Device interactions on a common basis: each adb invocation (raw) or each
    mobile_* call (MCP) = 1 step, so counts compare across arms. Excludes lock
    plumbing and the final report."""
    if tooling == "raw":
        return sum(max(1, _count_adb(_shell_command(e))) for e in _bash_adb_events(parser))
    return sum(
        1 for e in parser.events()
        if "mobile_" in e.name and "report_result" not in e.name
    )


def _device_interaction_texts(parser: TranscriptParser, tooling: str) -> list[str]:
    """Lowercased text from the agent's device interactions, used to verify a
    `broken` verdict was exercised on-device rather than read off the source."""
    texts: list[str] = []
    for e in parser.events():
        if tooling == "raw":
            if _is_shell_event(e) and "adb" in e.input_str:
                # Command AND output: raw mode reads the screen via uiautomator
                # dump, so probe text lives in the result, not the command.
                texts.append(f"{e.input_str} {e.result_text}".lower())
        elif e.name.startswith("mcp__device"):
            texts.append(e.input_str.lower())
    texts += [t.lower() for t in parser.observation_texts()]
    return texts


# Incremental reporting: verdicts are banked as the agent goes. Truncation
# keeps partial credit, each verdict gets a call index, and the probe gate
# becomes temporal — the device work must come BEFORE the claim.
_AREA_RE = re.compile(
    r"area\s*:\s*(?P<area>[A-Za-z0-9_\-]+)\s*\|\s*verdict\s*:\s*(?P<verdict>[A-Za-z_]+)",
    re.I,
)
_DEVIATES = ("deviates", "broken", "fail", "not_as_specified", "bug")
_AS_SPECIFIED = ("as_specified", "ok", "pass", "works", "correct", "matches")
# "I could not test this." Without it, an agent that cannot reach an area is
# forced to lie one way (a miss) or the other (a false report).
_BLOCKED = ("blocked", "could_not_verify", "cannot_verify", "not_verified",
            "unverifiable", "not_tested", "untested")


def _normalize_verdict(raw: str) -> str | None:
    """Map a reported label onto ok|broken|blocked. Blocked first, then
    deviation words, so substring overlaps cannot misread a label."""
    val = raw.strip().lower()
    if any(w in val for w in _BLOCKED):
        return "blocked"
    if any(w in val for w in _DEVIATES):
        return "broken"
    if any(w in val for w in _AS_SPECIFIED):
        return "ok"
    return None


def _result_text(result: object) -> str:
    """Text content of a tool result, DISCARDING image blocks — a short probe
    can collide with an arbitrary base64 run, forging the evidence the gate
    exists to demand."""
    if isinstance(result, dict):
        blocks = result.get("content")
        if isinstance(blocks, list):
            return " ".join(
                str(b.get("text", "")) for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
        return json.dumps({k: v for k, v in result.items() if k != "content"})
    return "" if result is None else str(result)


def _findings_write(name: str, inp: object) -> str | None:
    """The YAML body of a tool call that writes the submission file, else None.
    Covers Write, Edit, and shell heredocs; fragment reassembly is deliberately
    not attempted — guessing at file contents would defeat the channel."""
    if not isinstance(inp, dict):
        return None
    lname = (name or "").lower()
    if lname in ("write", "edit", "create", "str_replace_editor", "notebookedit"):
        path = str(inp.get("file_path") or inp.get("path") or "")
        if not path.endswith(_SUBMISSION_FILE):
            return None
        for key in ("content", "new_string", "new_str", "file_text"):
            val = inp.get(key)
            if isinstance(val, str) and val.strip():
                return val
        return None
    if "bash" in lname or "shell" in lname:
        cmd = str(inp.get("command") or inp.get("cmd") or "")
        if _SUBMISSION_FILE not in cmd:
            return None
        m = _HEREDOC_RE.search(cmd)
        return m.group("body") if m else None
    return None


def _ordered_stream(transcript: str, tooling: str) -> list[tuple[str, str]]:
    """The episode as an ordered list of ('device'|'text', payload). Re-walks the
    raw JSONL because the temporal gate needs the interleaving, which
    TranscriptParser does not preserve. Handles Claude and Codex shapes."""
    out: list[tuple[str, str]] = []
    device_call_ids: dict[str, bool] = {}   # tool_use id -> was it a device call
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = ev.get("type")
        if etype == "assistant":                       # Claude
            for b in ev.get("message", {}).get("content", []):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    name = b.get("name", "")
                    payload = f"{name} {json.dumps(b.get('input', {}))}".lower()
                    is_dev = ("adb" in payload) if tooling == "raw" else ("mobile_" in name)
                    if b.get("id"):
                        device_call_ids[b["id"]] = is_dev
                    out.append(("device" if is_dev else "other", payload))
                    # The submission carried RAW (lowercasing would destroy the
                    # YAML), in ADDITION to the entry above so step accounting
                    # is untouched.
                    body = _findings_write(b.get("name", ""), b.get("input"))
                    if body is not None:
                        out.append(("findings", body))
                elif b.get("type") == "text":
                    out.append(("text", b.get("text", "")))
                elif b.get("type") == "thinking":
                    out.append(("text", b.get("thinking", "")))
        elif etype == "user":                          # Claude tool results
            for b in ev.get("message", {}).get("content", []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content", "")
                    text = (" ".join(x.get("text", "") for x in c if isinstance(x, dict))
                            if isinstance(c, list) else str(c))
                    # A result is device evidence ONLY if its call was a device
                    # call; unknown ids stay non-device — evidence must be
                    # proven, not assumed.
                    out.append(("device" if device_call_ids.get(b.get("tool_use_id"))
                                else "other", text.lower()))
        elif etype == "item.completed":                # Codex (item.started is a dup)
            it = ev.get("item") or {}
            kind = it.get("type")
            if kind == "agent_message":
                out.append(("text", it.get("text", "")))
            elif kind == "mcp_tool_call":
                name = str(it.get("tool") or "")
                payload = (f"{name} {json.dumps(it.get('arguments') or {})} "
                           f"{_result_text(it.get('result'))}").lower()
                out.append(("device" if "mobile_" in name else "other", payload))
            elif kind == "command_execution":
                cmd = str(it.get("command") or "")
                # Match the output too: raw mode reads the screen via uiautomator
                # dump, so probe text only ever appears in aggregated_output.
                payload = f"{cmd} {it.get('aggregated_output') or ''}".lower()
                is_dev = tooling == "raw" and "adb" in cmd.lower()
                out.append(("device" if is_dev else "other", payload))
    return out


def _bank_findings(
    transcript: str, features: list[dict], tooling: str,
) -> tuple[dict[str, dict], dict]:
    """Walk the episode in order, banking each verdict where it was claimed.
    `probed` = the feature's probe was seen in device output BEFORE the claim.
    Both channels feed one ordered stream; last banking wins."""
    by_id = {f["id"]: f for f in features}
    banked: dict[str, dict] = {}
    device_text: list[str] = []
    calls = 0
    last_claim_at = 0
    channel = {"findings_yaml_writes": 0, "findings_yaml_errors": [], "channels": set()}

    def _bank(fid: str, verdict: str, source: str) -> None:
        nonlocal last_claim_at
        feat = by_id.get(fid)
        if feat is None:
            return
        probes = [p.lower() for p in (feat.get("probe") or [])]
        keyword_ok = (not probes) or any(p in t for t in device_text for p in probes)
        # Device work must PRECEDE the claim — a floor on prior work, not on the
        # gap between claims, so "two calls then assert everything" is blocked
        # without punishing an agent that batches its write-up.
        worked = calls >= _MIN_CALLS_PER_CLAIM
        banked[fid] = {
            "verdict": verdict,
            "at_call": calls,
            "since_last": calls - last_claim_at,
            # No probe configured → cannot keyword-check, so the work rule carries it.
            "probed": keyword_ok and worked,
            "source": source,
        }
        last_claim_at = calls
        channel["channels"].add(source)

    known = set(by_id)
    resolve = hidden_resolver(features)
    for kind, payload in _ordered_stream(transcript, tooling):
        if kind == "device":
            calls += 1
            device_text.append(payload)
            continue
        if kind == "findings":
            channel["findings_yaml_writes"] += 1
            sub = submission.parse(payload, known_areas=known, resolve=resolve)
            # Errors are recorded, not scored: a malformed write banks nothing,
            # and the `AREA:` channel still carries the episode.
            channel["findings_yaml_errors"] = sub.errors[:10]
            for claim in sub.claims:
                # Both channels bank the INTERNAL vocabulary; the scorer
                # compares against `state`.
                verdict = _normalize_verdict(claim.verdict)
                if verdict is not None:
                    _bank(claim.area, verdict, "findings_yaml")
            continue
        if kind != "text":
            continue
        for m in _AREA_RE.finditer(payload):
            verdict = _normalize_verdict(m.group("verdict"))
            if verdict is not None:
                area = m.group("area")
                if area not in known and resolve is not None:
                    # An `other…` line describing a HIDDEN area: map it by the
                    # line's own words, exactly as the findings.yaml channel does.
                    area = resolve(area, {"actual": payload}) or area
                _bank(area, verdict, "area_line")
    channel["channels"] = sorted(channel["channels"])
    return banked, channel


def exploration_verdict(transcript: str, model: str, task: BenchmarkTask) -> VerifierResult:
    """Score an open-ended bug hunt by EXACT per-feature label match — buggy
    features marked broken earn recall, working ones marked broken cost
    precision. Ablation arms differ only in report channel and evidence rule."""
    spec = task.bug_spec or {}
    features = spec.get("features") or []
    feature_ids = [f["id"] for f in features]
    buggy = [f for f in features if f["state"] == "broken"]
    # Only an explicit `ok` is a control. `collateral` areas (also broken by a
    # seeded defect) earn nothing and cost nothing — charging them punished
    # agents for measuring reality.
    working = [f for f in features if f["state"] == "ok"]
    total = len(buggy)
    tooling = str(spec.get("tooling") or "")

    parser = TranscriptParser(transcript)
    contamination = contamination_scan(parser, spec.get("workspace"))
    status = parser.reported_status()
    if tooling:
        summary = _last_result_segment(_report_text(parser, transcript))
    else:
        summary = _report_summary(parser)
    # Banked verdicts are authoritative when present — they carry ordering.
    # Final-RESULT-only episodes still score.
    banked, channel = _bank_findings(transcript, features, tooling)
    # Also read the file as it finally stands on disk: Edit-appended fragments
    # do not parse from the stream alone. This can rescue a verdict, but an
    # already-banked area keeps its earlier at_call, so it never launders earliness.
    file_sub = submission.parse(spec.get("findings_file") or "", known_areas=set(feature_ids),
                                resolve=hidden_resolver(features))
    # Reproductions are captured, not scored; the claims go into the evidence
    # bundle for the replayer.
    replayable = [c for c in file_sub.claims if c.replayable]
    channel["claims_total"] = len(file_sub.claims)
    channel["claims_with_repro"] = len(replayable)
    channel["repro_claims"] = [
        {"area": c.area, "verdict": c.verdict,
         "steps": [s.as_dict() for s in c.steps],
         "expect": c.expect.as_dict() if c.expect else None}
        for c in replayable
    ]
    file_verdicts = {c.area: _normalize_verdict(c.verdict) for c in file_sub.claims}
    file_verdicts = {a: v for a, v in file_verdicts.items() if v is not None}
    if file_sub.claims:
        channel["channels"] = sorted(set(channel["channels"]) | {"findings_file"})

    verdicts = dict(_parse_result_line(summary, feature_ids))
    verdicts.update(file_verdicts)
    verdicts.update({fid: b["verdict"] for fid, b in banked.items()})

    # Evidence gate: a `broken` verdict counts only if the agent exercised THAT
    # feature on-device. Banked verdicts get the temporal check; final-RESULT-only
    # falls back to "seen anywhere". No probe = cannot check, not blocked.
    dev_texts = _device_interaction_texts(parser, tooling)

    def _probed_ok(f: dict) -> bool:
        if f["id"] in banked:
            return bool(banked[f["id"]]["probed"])
        probes = f.get("probe") or []
        return (not probes) or any(p in t for t in dev_texts for p in probes)

    def _found(f: dict) -> bool:  # buggy feature credited as found
        return verdicts.get(f["id"]) == "broken" and _probed_ok(f)

    # Blocked earns nothing and costs nothing: it beats guessing but loses to
    # testing, and blocked areas do not count toward coverage.
    blocked_ids = [fid for fid, v in verdicts.items() if v == "blocked"]
    # Flag blocked areas whose probe never appeared in device evidence, so
    # "tried and could not" stops looking like "did not try". The gate is weak
    # by design; blocked_share below carries the rest.
    _by_id = {f["id"]: f for f in features}
    unverified_blocked = [fid for fid in blocked_ids
                          if fid in _by_id and not _probed_ok(_by_id[fid])]
    blocked_share = round(len(blocked_ids) / len(features), 4) if features else 0.0

    tp = sum(1 for f in buggy if _found(f))
    fn = total - tp
    fp = sum(1 for f in working if verdicts.get(f["id"]) == "broken")
    found_bug_ids = [f.get("bug_id") or f["id"] for f in buggy if _found(f)]
    missed = [f.get("bug_id") or f["id"] for f in buggy if not _found(f)]
    # Claimed broken but not verified on-device (the read-the-code shortcut) —
    # counts as missed, surfaced for audit.
    unverified = [f.get("bug_id") or f["id"] for f in buggy
                  if verdicts.get(f["id"]) == "broken" and not _probed_ok(f)]
    false_alarms = [f["id"] for f in working if verdicts.get(f["id"]) == "broken"]

    # Recall is tier-weighted; precision stays count-based — a false report
    # costs a triage cycle whatever the feature.
    w_total = sum(tier_weight(f.get("tier")) for f in buggy)
    w_found = sum(tier_weight(f.get("tier")) for f in buggy if _found(f))
    recall = (w_found / w_total) if w_total else 0.0
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else (1.0 if tp else 0.0)
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    # Coverage counts VERIFIED areas only — blocked ones were reported but not tested.
    verified = sum(1 for v in verdicts.values() if v in ("ok", "broken"))
    coverage = verified / len(features) if features else 0.0

    tool_calls = _count_tool_calls(transcript)   # total tool calls (raw batches adb)
    device_actions = _device_actions(parser, tooling)  # comparable cross-condition basis
    # The socket-level meter sees work the transcript cannot (a script run by
    # path does its device work inside one tool call); prefer it when present.
    metered_total = spec.get("metered_total")
    if isinstance(metered_total, int) and metered_total > 0:
        device_actions = max(device_actions, metered_total)
    # Prefer the ENFORCED count: `steps` is displayed against the budget, so
    # anything else contradicts on truncation. device_actions and tool_calls
    # stay available as the work and item measures.
    steps = spec.get("hook_steps") or (device_actions if tooling else tool_calls)
    budget = spec.get("step_budget")
    within_budget = (steps <= budget) if budget else None

    observations = len(parser.observation_texts())
    device_calls = len(parser.successful_device_events())
    bash_adb = _bash_adb_events(parser)
    mcp_tool_calls = sum(1 for e in parser.events() if e.name.startswith("mcp__device"))
    if tooling == "raw":
        evidence = len(bash_adb) >= 1
    else:
        evidence = observations >= 1 and device_calls >= 1

    # passed = found every bug AND no false alarms, with on-device evidence.
    passed = bool(tp == total and total > 0 and fp == 0 and evidence)

    reasons: list[str] = []
    if not verdicts:
        reasons.append("no parseable RESULT: verdict line in the report")
    if not evidence:
        if tooling == "raw":
            reasons.append("no device interaction detected (zero adb commands in Bash calls)")
        else:
            reasons.append(
                f"weak evidence (observations={observations}, device_calls={device_calls})"
            )
    if unverified:
        reasons.append(
            f"{len(unverified)} bug(s) claimed broken but NOT exercised on-device "
            f"(no credit): {', '.join(unverified)}"
        )
    if unverified_blocked:
        reasons.append(
            f"{len(unverified_blocked)} area(s) reported blocked but never exercised "
            f"on-device: {', '.join(unverified_blocked)}"
        )
    if missed:
        reasons.append(f"missed {len(missed)} bug(s): {', '.join(missed)}")
    if false_alarms:
        reasons.append(f"false alarm on working feature(s): {', '.join(false_alarms)}")

    usage = parser.token_usage()
    reported_cost = usage.get("reported_cost_usd")
    cost = reported_cost if reported_cost is not None else pricing.compute_cost_usd(model, usage)
    # OVERALL = tier-weighted recall × speed − false-report cost; the speed term
    # is bounded below the smallest quality gap, so ranking stays quality-first.
    # Speed credit is EARNED: only probe-verified finds bank it.
    evidence_earned = tp > 0 and all(_probed_ok(f) for f in buggy if _found(f))
    # Earliness divides by the same unit the budget is denominated in;
    # device_actions stays the separate work measure.
    spent = spec.get("hook_steps") or device_actions
    early = _earliness(spent, budget) if evidence_earned else 0.0
    # No earned find → no speed credit (recall already zeroes it; kept explicit).
    speed = speed_factor(spent, budget) if evidence_earned else 1.0 - _SPEED_WEIGHT

    # Fabricated reports are worse than none, so the raw score can go negative.
    fp_cost = round(_HUNT_FP_PENALTY * fp, 4)
    # `overall` is the reported score, clamped to [0, 1]. Ranking uses the
    # signed `overall_raw`, so spraying stays below honest silence.
    overall_raw = round(recall * speed - fp_cost, 4)
    overall = round(max(0.0, overall_raw), 4)
    reward = overall_raw

    metrics = {
        # RL signals
        "reward": reward,
        "overall": overall,          # reported score, clamped to [0, 1]
        "overall_raw": overall_raw,  # signed; ranking uses this so spray < silence
        "earliness": early,
        "speed_factor": speed,       # the discount actually applied, in [1-W, 1]
        "speed_weight": _SPEED_WEIGHT,
        "speed_earned": evidence_earned,    # False → guessed, so no speed credit
        # Budget consumed in its own unit: hook_steps is what was SPENT,
        # device_actions what was DONE.
        "hook_steps": spec.get("hook_steps"),
        "budget_used": (round(spent / budget, 4) if budget else None),
        "fp_cost": fp_cost,
        "weighted_found": w_found,
        "weighted_total": w_total,
        # Clean-area count — the false-alarm rate's denominator, kept as a count
        # so rates aggregate correctly across apps.
        "working_total": len(working),
        # Call index each verdict landed on (incremental banking only) — finding
        # a bug at call 12 vs call 48 is not the same performance.
        "banked_at": {fid: b["at_call"] for fid, b in banked.items()},
        "banked_count": len(banked),
        # Area count of the spec THIS episode ran against — briefs gain and lose
        # areas over time.
        "areas_total": len(features),
        # Which emulator ran this: CPU contention under parallel fan-out can
        # truncate an episode that would have completed alone.
        "device_serial": spec.get("device_serial"),
        # The agent left the app under test — its verdicts describe the wrong app.
        "off_app": bool(spec.get("off_app")),
        "ended_in_package": spec.get("ended_in_package"),
        # Only an incremental reporter gets the temporal evidence check and
        # partial credit on truncation, so compliance is worth seeing.
        "banked_incremental": len({b["at_call"] for b in banked.values()}) > 1,
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "f1": round(f1, 4),
        "bugs_found": tp,
        "bugs_total": total,
        "false_positives": fp,
        "coverage": round(coverage, 4),
        "found_bug_ids": found_bug_ids,
        "unverified_broken": unverified,   # claimed broken w/o on-device evidence
        "unverified_count": len(unverified),
        # Areas reported untestable — neither a find nor a false report.
        "blocked": blocked_ids,
        "unverified_blocked": unverified_blocked,
        "unverified_blocked_count": len(unverified_blocked),
        # Share declared untestable; blocking is free by design, so keep it auditable.
        "blocked_share": blocked_share,
        "blocked_count": len(blocked_ids),
        "truncated": bool(spec.get("truncated")),   # killed at the step budget
        # Stopped by the wall clock with steps unspent — an environment limit,
        # not "found nothing".
        "timed_out": bool(spec.get("timed_out")),
        # No device evidence at all: nothing to score, and a 0 here would let
        # environment noise move the leaderboard.
        "infra_failure": device_actions == 0,
        # Killed for a reason outside the agent's control — excluded from the
        # board, not averaged. `not truncated` keeps it honest: a budget kill
        # with nothing banked is a real 0, not a missing result. A failed staging
        # (device_setup could not seed the start state) is env_failure regardless
        # of what the agent then did — it never had the specced app to test.
        "env_failure": (bool(spec.get("staging_failed"))
                        or (bool(spec.get("exit_code")) and not banked and not verdicts
                            and not spec.get("truncated"))),
        "staging_failed": spec.get("staging_failed") or "",
        # Reached the answer key. Not a QA result — and it can produce a perfect
        # score, which is why it is a classification, not a warning.
        **contamination.as_metrics(),
        # THE step unit — one interaction, identical in every arm; the per-kind
        # breakdown is what a later budget re-derivation calibrates against.
        **{k: spec.get(k) for k in
           ("interactions", *(f"interactions_{kind}" for kind in _INTERACTION_KINDS))},
        # Raw adb request counts — diagnostic only; they measure transport,
        # not work. Never budget on them.
        "metered_total": spec.get("metered_total"),
        "metered_actions": spec.get("metered_actions"),
        "metered_observations": spec.get("metered_observations"),
        # Keyed on the interaction count: an episode is v3 only if it was
        # actually measured in the v3 unit.
        "budget_accounting": BUDGET_ACCOUNTING if spec.get("interactions") else "per-adb-v1",
        "submission_channels": channel["channels"],
        "findings_yaml_writes": channel["findings_yaml_writes"],
        "findings_yaml_errors": channel["findings_yaml_errors"],
        # Reproduction coverage — not scored; it says whether differential
        # replay has anything to work with.
        "claims_total": channel.get("claims_total", 0),
        "claims_with_repro": channel.get("claims_with_repro", 0),
        "repro_claims": channel.get("repro_claims", []),
        "exit_code": spec.get("exit_code"),
        "app_id": spec.get("app_id", ""),
        "difficulty": spec.get("difficulty", ""),
        # `steps` is the cross-condition-comparable count for ablation runs;
        # `tool_calls` is the raw count (raw batches adb so it's lower).
        "steps": steps,
        "device_actions": device_actions,
        "tool_calls": tool_calls,
        "complexity": spec.get("complexity"),
        "step_budget": budget,
        "within_budget": within_budget,
        # device + cost
        "reported_status": status or "NONE",
        "device_tool_calls": device_calls,
        "observations": observations,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": cost,
        "cost_source": "reported" if reported_cost is not None
                       else ("estimated" if cost is not None else "unknown"),
    }
    criteria = (
        {f"found_{f.get('bug_id') or f['id']}": _found(f) for f in buggy}
        | {f"clean_{f['id']}": (verdicts.get(f["id"]) != "broken") for f in working}
        | {"evidence_attached": evidence}
    )
    if tooling:
        # Ablation diagnostics: how the agent worked, for auditing condition isolation.
        metrics.update({
            "condition": tooling,
            "raw_adb_calls": len(bash_adb),
            "raw_screencaps": sum(
                1 for e in bash_adb
                if "screencap" in e.input_str or "screenshot" in e.input_str
            ),
            "mcp_tool_calls": mcp_tool_calls,
            "source_reads": sum(
                1 for e in parser.events() if e.name in ("Read", "Glob", "Grep", "LS")
            ),
        })
        if tooling == "raw":
            # Any MCP tool call in a raw run means isolation leaked — run invalid.
            criteria["no_mcp_tools"] = mcp_tool_calls == 0
    return VerifierResult(
        passed=passed,
        score=round(recall, 4),
        # The schema bounds these to [0,1], so the headline lives in metrics.
        weighted_score=round(f1, 4),
        criteria=criteria,
        failure_reason="; ".join(reasons) or None,
        metrics=metrics,
    )


def clean_task_verdict(transcript: str, model: str, task: BenchmarkTask) -> VerifierResult:
    """Score a CLEAN guided task by a programmatic device-state oracle, not
    self-report — blind guessing cannot fake real device state. reward = 1.0 iff
    the oracle passes with evidence; flagging the working feature is a false alarm."""
    from .verify import device_oracle

    spec = task.bug_spec or {}
    oracle = spec.get("oracle") or {}
    pkg = task.bundle_id

    parser = TranscriptParser(transcript)
    status = parser.reported_status()
    observations = len(parser.observation_texts())
    device_calls = len(parser.successful_device_events())
    evidence = observations >= 1 and device_calls >= 1
    steps = _count_tool_calls(transcript)

    serial = spec.get("device_serial")  # set by run_episode; targets adb -s <serial>
    if not oracle:
        ok, detail = False, "no oracle configured for this clean task"
    elif not pkg:
        ok, detail = False, "task.bundle_id not set — cannot query device state"
    else:
        try:
            ok, detail = device_oracle.check(oracle, pkg, serial=serial)
        except Exception as exc:                              # noqa: BLE001 - report any adb failure
            ok, detail = False, f"oracle error: {exc}"

    passed = bool(ok and evidence)
    no_false_alarm = status != "FAIL"     # called a WORKING feature broken → false alarm

    reasons: list[str] = []
    if not ok:
        reasons.append(f"task not completed on device — {detail}")
    if not evidence:
        reasons.append(f"weak evidence (observations={observations}, device_calls={device_calls})")
    if not no_false_alarm:
        reasons.append("reported FAIL on a working feature (false alarm)")

    optimal = spec.get("optimal_steps")
    step_efficiency = (
        round(min(1.0, optimal / device_calls), 4) if optimal and device_calls else None
    )

    # The penalty dominates the completion credit on purpose — one false alarm
    # should erase a medium find, which is what deters spray-reporting.
    reward = (-_FP_PENALTY) if not no_false_alarm else (1.0 if passed else 0.0)

    usage = parser.token_usage()
    reported_cost = usage.get("reported_cost_usd")
    cost = reported_cost if reported_cost is not None else pricing.compute_cost_usd(model, usage)
    metrics = {
        "reward": reward,
        "task_kind": "clean",
        "oracle_passed": ok,
        "oracle_detail": detail,
        "no_false_alarm": no_false_alarm,
        "false_positive": not no_false_alarm,
        "infra_failure": device_calls == 0 and status is None,
        # Staging never seeded the specced start state — not the agent's result.
        "env_failure": bool(spec.get("staging_failed")),
        "staging_failed": spec.get("staging_failed") or "",
        "truncated": bool(spec.get("truncated")),   # killed at the step budget
        "fp_penalty": _FP_PENALTY if not no_false_alarm else 0.0,
        "tier": spec.get("tier", ""),
        "difficulty": spec.get("difficulty", ""),
        "optimal_steps": optimal,
        "step_efficiency": step_efficiency,
        "steps": steps,
        "reported_status": status or "NONE",
        "device_tool_calls": device_calls,
        "observations": observations,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": cost,
        "cost_source": "reported" if reported_cost is not None
                       else ("estimated" if cost is not None else "unknown"),
    }
    return VerifierResult(
        passed=passed,
        score=1.0 if passed else 0.0,
        weighted_score=1.0 if passed else 0.0,
        criteria={
            "oracle_satisfied": ok,
            "evidence_attached": evidence,
            "no_false_alarm": no_false_alarm,
        },
        failure_reason="; ".join(reasons) or None,
        metrics=metrics,
    )


def guided_verdict(transcript: str, model: str, task: BenchmarkTask) -> VerifierResult:
    """Dispatch a guided task to the right scorer by its type: clean tasks use the
    programmatic device oracle; everything else uses self-report bug detection."""
    if str((task.bug_spec or {}).get("type", "bug")).lower() == "clean":
        return clean_task_verdict(transcript, model, task)
    return guided_bug_verdict(transcript, model, task)


def _report_text(parser: TranscriptParser, transcript: str) -> str:
    """All agent-authored text relevant to the bug report: the report tool input
    plus any assistant text. Lower-cased for keyword matching."""
    parts: list[str] = []
    for e in parser.events():
        if "mobile_report_result" in e.name:
            parts.append(json.dumps(e.input))
    # Assistant free-text (Claude stream-json / our native adapter emit text blocks).
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for b in ev.get("message", {}).get("content", []):
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text", ""))
    return " ".join(parts).lower()


_TAP_TOOLS = ["mobile_tap", "mobile_tap_and_observe"]


def _step_completion(parser: TranscriptParser, flow_steps: list[dict]) -> tuple[dict[str, bool], float]:
    """Per-step booleans + completed fraction. screen_keywords credit reaching a
    screen; target_keywords credit the tap itself, since a buggy build never
    shows the outcome. Neither = any device interaction."""
    screens = parser.screens_visited()
    device_calls = len(parser.successful_device_events())
    results: dict[str, bool] = {}
    for s in flow_steps:
        screen_kws = [k.lower() for k in (s.get("screen_keywords") or [])]
        target_kws = s.get("target_keywords") or []
        if screen_kws:
            done = any(k in screens for k in screen_kws)
        elif target_kws:
            done = parser.interaction_performed(_TAP_TOOLS, target_kws)
        else:
            done = device_calls >= 1
        results[f"step_{s['id']}"] = bool(done)
    frac = (sum(results.values()) / len(results)) if results else 0.0
    return results, frac


def bug_verdict(transcript: str, model: str, task: BenchmarkTask) -> VerifierResult:
    """Score a bug-finding run. RL-ready: metrics['reward'] is the terminal reward."""
    spec = task.bug_spec or {}
    expected = str(spec.get("expected_verdict", "FAIL")).upper()
    symptom_keywords = spec.get("symptom_keywords") or []
    flow_steps = spec.get("flow_steps") or []

    parser = TranscriptParser(transcript)
    status = parser.reported_status()  # "PASS" | "FAIL" | "BLOCKED" | None
    report_text = _report_text(parser, transcript)

    step_results, step_completion = _step_completion(parser, flow_steps)
    observations = len(parser.observation_texts())
    device_calls = len(parser.successful_device_events())
    evidence = observations >= 1 and device_calls >= 1

    verdict_correct = status == expected
    symptom_match = any(kw in report_text for kw in symptom_keywords)

    if expected == "FAIL":            # buggy task: must report FAIL + name the symptom
        bug_found = verdict_correct and symptom_match
    else:                             # clean control: must not false-alarm
        bug_found = verdict_correct

    detection_score = 1.0 if bug_found else (0.5 if verdict_correct else 0.0)
    reward = round(0.5 * step_completion + 0.5 * detection_score, 4)
    passed = bool(bug_found and evidence)

    # Efficiency is exposed for analysis/RL shaping, NOT folded into reward.
    optimal = spec.get("optimal_steps")
    step_efficiency = (
        round(min(1.0, optimal / device_calls), 4)
        if optimal and device_calls else None
    )

    reasons: list[str] = []
    if status is None:
        reasons.append("agent never called mobile_report_result")
    elif not verdict_correct:
        reasons.append(f"reported {status}, expected {expected}")
    elif expected == "FAIL" and not symptom_match:
        reasons.append("reported FAIL but did not describe the seeded bug's symptom")
    if not evidence:
        reasons.append(f"weak evidence (observations={observations}, device_calls={device_calls})")

    criteria = {
        **step_results,
        "verdict_correct": verdict_correct,
        "symptom_match": symptom_match,
        "evidence_attached": evidence,
    }

    usage = parser.token_usage()
    reported_cost = usage.get("reported_cost_usd")
    cost = reported_cost if reported_cost is not None else pricing.compute_cost_usd(model, usage)
    metrics = {
        # RL signals
        "reward": reward,
        "step_completion": round(step_completion, 4),
        "steps_completed": sum(step_results.values()),
        "steps_total": len(step_results),
        # bug-detection diagnostics
        "bug_id": spec.get("id", task.id),
        "difficulty": spec.get("difficulty", ""),
        "optimal_steps": optimal,
        "step_efficiency": step_efficiency,
        "expected_verdict": expected,
        "reported_status": status or "NONE",
        "bug_found": bug_found,
        # device + cost
        "device_tool_calls": device_calls,
        "observations": observations,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": cost,
        "cost_source": "reported" if reported_cost is not None
                       else ("estimated" if cost is not None else "unknown"),
    }

    return VerifierResult(
        passed=passed,
        score=round(step_completion, 4),     # fraction of flow reached
        weighted_score=reward,               # leaderboard / RL reward
        criteria=criteria,
        failure_reason="; ".join(reasons) or None,
        metrics=metrics,
    )


def guided_bug_verdict(transcript: str, model: str, task: BenchmarkTask) -> VerifierResult:
    """Score a guided BUG task phrasing-proof: exact `RESULT: feature=<ok|broken>`
    label, falling back to the reported FAIL/PASS status. found = marked broken
    with on-device evidence."""
    spec = task.bug_spec or {}
    flow_steps = spec.get("flow_steps") or []

    parser = TranscriptParser(transcript)
    status = parser.reported_status()
    summary = _report_summary(parser)

    step_results, step_completion = _step_completion(parser, flow_steps)
    observations = len(parser.observation_texts())
    device_calls = len(parser.successful_device_events())
    evidence = observations >= 1 and device_calls >= 1

    # Exact-label verdict on the single feature token; fall back to FAIL = broken.
    verdicts = _parse_result_line(summary, ["feature"])
    if "feature" in verdicts:
        marked_broken = verdicts["feature"] == "broken"
    else:
        marked_broken = status == "FAIL"

    # ── Attempt-anchor: a blind "broken" guess must score 0 — the agent must have
    # exercised the feature, and any bug oracle must confirm the bug is real.
    anchored = (step_completion >= 1.0) if flow_steps else (device_calls >= 1)

    bug_oracle = spec.get("oracle") or {}
    oracle_confirms, oracle_detail = True, "no bug oracle configured"
    if bug_oracle and task.bundle_id:
        from .verify import device_oracle
        try:
            oracle_confirms, oracle_detail = device_oracle.check(
                bug_oracle, task.bundle_id, serial=spec.get("device_serial")
            )
        except Exception as exc:  # noqa: BLE001 - an adb failure must not silently pass
            oracle_confirms, oracle_detail = False, f"oracle error: {exc}"

    bug_found = bool(marked_broken and evidence)          # legacy detection signal
    correct = bool(bug_found and anchored and oracle_confirms)
    detection_score = 1.0 if bug_found else (0.5 if marked_broken else 0.0)

    # Never touched the device AND never reported = infra failure, not a model
    # miss; flagged here and excluded from the aggregates.
    infra_failure = device_calls == 0 and status is None

    weight = tier_weight(spec.get("tier"))
    # Speed credit against the derived step budget, not hand-authored ref_steps.
    # Pure bonus — overrunning is already punished by the hard stop.
    budget = spec.get("step_cap")
    early = _earliness(device_calls, budget) if correct else 0.0
    efficiency = earliness_multiplier(device_calls, budget) if correct else 0.0
    # `reward` is the tier-weighted signal (can exceed 1); the schema's
    # score/weighted_score stay normalized.
    normalized = round((1.0 if correct else 0.0) * efficiency, 4)
    reward = round(weight * normalized, 4)
    # The bonus can push `normalized` above 1.0 and weighted_score is bounded
    # le=1.0, so clamp for the schema; unclamped values stay in metrics.
    schema_score = min(1.0, normalized)
    passed = correct

    optimal = spec.get("optimal_steps")
    step_efficiency = (
        round(min(1.0, optimal / device_calls), 4) if optimal and device_calls else None
    )

    reasons: list[str] = []
    if not marked_broken:
        reasons.append(f"did not flag the feature broken (reported {status or 'nothing'})")
    if not evidence:
        reasons.append(f"weak evidence (observations={observations}, device_calls={device_calls})")
    if marked_broken and not anchored:
        reasons.append("flagged broken without exercising the feature (attempt-anchor failed)")
    if marked_broken and not oracle_confirms:
        reasons.append(f"device does not confirm the bug — {oracle_detail}")

    usage = parser.token_usage()
    reported_cost = usage.get("reported_cost_usd")
    cost = reported_cost if reported_cost is not None else pricing.compute_cost_usd(model, usage)
    metrics = {
        "reward": reward,
        "task_kind": "bug",
        "step_completion": round(step_completion, 4),
        "steps_completed": sum(step_results.values()),
        "steps_total": len(step_results),
        "bug_id": spec.get("id", task.id),
        "difficulty": spec.get("difficulty", ""),
        "tier": spec.get("tier", ""),
        "tier_weight": weight,
        "correct": correct,
        "anchored": anchored,
        "oracle_confirms": oracle_confirms,
        "oracle_detail": oracle_detail,
        "ref_steps": spec.get("ref_steps"),
        "step_cap": budget,
        "earliness": early,
        "efficiency": efficiency,
        "detection_only": detection_score,   # pre-anchor signal, kept for diagnosis
        "truncated": bool(spec.get("truncated")),   # killed at the step budget
        "infra_failure": infra_failure,
        "optimal_steps": optimal,
        "step_efficiency": step_efficiency,
        "reported_status": status or "NONE",
        "bug_found": bug_found,
        "device_tool_calls": device_calls,
        "observations": observations,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": cost,
        "cost_source": "reported" if reported_cost is not None
                       else ("estimated" if cost is not None else "unknown"),
    }
    return VerifierResult(
        passed=passed,
        score=round(step_completion, 4),
        weighted_score=schema_score,
        criteria={
            **step_results,
            "flagged_broken": marked_broken,
            "evidence_attached": evidence,
            "exercised_feature": anchored,
            "device_confirms_bug": oracle_confirms,
        },
        failure_reason="; ".join(reasons) or None,
        metrics=metrics,
    )
