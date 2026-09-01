"""Per-episode audit bundle: the ordered tool calls with screenshots, written next
to result.json so a score can be checked rather than trusted. Pure post-processing
of the transcript, best-effort — a failed bundle must never fail a scored episode."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .redaction import _redact_record, _sanitize_payload, extract_mcp_images
from .evidence_manifest import write_manifest
from .evidence_report import render_report
from .frame_capture import load_frames
from .transcript import clean_result_text

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Result text is context, not the payload; long replies are truncated to keep
# steps.jsonl readable.
_RESULT_CHARS = 1500

_OBSERVE_TOOLS = ("mobile_observe_screen", "mobile_tap_and_observe")
_REPORT_TOOLS = ("mobile_report_result",)


@dataclass
class _Call:
    """One tool call recovered from a transcript, agent-format-independent."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    content: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    ok: bool = True


# ── transcript → calls ────────────────────────────────────────────────────────


def _walk(transcript: str) -> list[tuple[str, Any]]:
    """The episode as ordered ("call", _Call) / ("text", str) events — a claim only
    means something relative to the calls before it. Replies arrive in separate
    messages and are patched into the pending call; unparseable lines are skipped."""
    events: list[tuple[str, Any]] = []
    pending: dict[str, _Call] = {}

    for line in transcript.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        etype = event.get("type")

        if etype == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    events.append(("text", text))
                continue
            call = _codex_call(item)
            if call is not None:
                events.append(("call", call))
            continue

        if etype == "assistant":
            for block in _content_blocks(event.get("message")):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    events.append(("text", block["text"]))
                elif block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
                    events.append(("text", block["thinking"]))
                elif block.get("type") == "tool_use" and block.get("id"):
                    args = block.get("input")
                    call = _Call(
                        name=str(block.get("name") or ""),
                        args=args if isinstance(args, dict) else {},
                    )
                    pending[str(block["id"])] = call
                    events.append(("call", call))
            continue

        if etype == "user":
            for block in _content_blocks(event.get("message")):
                if block.get("type") != "tool_result":
                    continue
                call = pending.get(str(block.get("tool_use_id") or ""))
                if call is None:
                    continue
                content = block.get("content")
                call.content = [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []
                call.text = _text_of(call.content) or (content if isinstance(content, str) else "")
                call.ok = not block.get("is_error")

    return events


def _tool_calls(transcript: str) -> Iterator[_Call]:
    """Just the calls, in call order."""
    return (call for kind, call in _walk(transcript) if kind == "call")


def _codex_call(item: Any) -> _Call | None:
    """A codex item.completed record → a call, or None. Shell commands count too:
    the raw arm drives the device via adb, and those are its only evidence."""
    if not isinstance(item, dict):
        return None
    itype = item.get("type")

    if itype == "mcp_tool_call":
        args = item.get("arguments")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        content = result.get("content")
        blocks = [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []
        return _Call(
            name=str(item.get("tool") or ""),
            args=args if isinstance(args, dict) else {},
            content=blocks,
            text=_text_of(blocks),
            ok=not item.get("error") and item.get("status") in (None, "completed"),
        )

    if itype == "command_execution":
        command = item.get("command")
        if not isinstance(command, str):
            return None
        return _Call(
            name="command_execution",
            args={"command": command},
            text=str(item.get("aggregated_output") or item.get("output") or ""),
            ok=item.get("exit_code") in (0, None),
        )

    return None


def _content_blocks(message: Any) -> list[dict[str, Any]]:
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []


def _text_of(blocks: list[dict[str, Any]]) -> str:
    parts = [b.get("text", "") for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str)]
    return "\n".join(p for p in parts if p).strip()


# ── screenshots ───────────────────────────────────────────────────────────────


def _images(blocks: list[dict[str, Any]]) -> list[tuple[bytes, str]]:
    """Decode image blocks to (bytes, ext). MCP puts the payload at data/mimeType;
    Claude Code re-wraps the same image with the payload under source."""
    images = [(data, "png" if mime == "image/png" else "jpg") for data, mime in extract_mcp_images(blocks)]

    for block in blocks:
        source = block.get("source")
        if block.get("type") != "image" or not isinstance(source, dict):
            continue
        payload = source.get("data")
        mime = source.get("media_type") or source.get("mediaType")
        if not isinstance(payload, str) or mime not in ("image/jpeg", "image/png"):
            continue
        try:
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError):
            continue
        if raw:
            images.append((raw, "png" if mime == "image/png" else "jpg"))

    return images


# ── step shaping ──────────────────────────────────────────────────────────────


def _base_name(name: str) -> str:
    """Strip the MCP namespace: mcp__device__mobile_tap → mobile_tap. Claude Code
    prefixes tools with the server name; codex reports bare names."""
    return name.split("__")[-1] if name.startswith("mcp__") else name


def _kind(name: str) -> str:
    name = _base_name(name)
    if any(t in name for t in _OBSERVE_TOOLS):
        return "observe"
    if any(t in name for t in _REPORT_TOOLS):
        return "report"
    if name.startswith("mobile_"):
        return "action"
    if name == "command_execution":
        return "command"
    return "other"


def _summary(call: _Call) -> str:
    """One human-readable line per step — what the viewer shows next to the screenshot."""
    name, args = _base_name(call.name), call.args
    if name == "mobile_observe_screen":
        return "Observed screen"
    if name == "mobile_tap" or name == "mobile_tap_and_observe":
        x, y = args.get("x"), args.get("y")
        target = f"({x}, {y})" if isinstance(x, (int, float)) and isinstance(y, (int, float)) else "element"
        return f"Tapped {target}"
    if name in ("mobile_type_text", "mobile_edit_field"):
        return "Typed text"          # never echo the value — it may be a credential
    if name == "mobile_swipe":
        return f"Swiped {args.get('direction') or ''}".strip()
    if name == "mobile_swipe_coordinates":
        return (f"Swiped ({args.get('start_x')}, {args.get('start_y')}) → "
                f"({args.get('end_x')}, {args.get('end_y')})")
    if name == "mobile_press_button":
        return f"Pressed {args.get('button') or 'button'}"
    if name == "mobile_report_result":
        return f"Reported {str(args.get('status') or '').upper() or 'result'}"
    if name == "command_execution":
        return f"Ran: {str(args.get('command') or '')[:120]}"
    return name


def _observation(call: _Call) -> dict[str, Any]:
    """Elements + screen size out of an observe reply, when it carries them."""
    if not call.text:
        return {}
    try:
        payload = json.loads(call.text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    elements = payload.get("elements")
    if isinstance(elements, list):
        out["elements"] = [e for e in elements if isinstance(e, str)]
    if isinstance(payload.get("screen_size"), dict):
        out["screen_size"] = payload["screen_size"]
    if isinstance(payload.get("surface"), str):
        out["surface"] = payload["surface"]
    return out


def _scrub(value: Any, secrets: tuple[str, ...]) -> Any:
    """Remove known plaintext secrets (the raw arm's credential) from any payload."""
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, secrets) for k, v in value.items()}
    return value


# ── per-bug index ─────────────────────────────────────────────────────────────


def _claims(events: list[tuple[str, Any]], known: set[str],
            resolve=None) -> list[tuple[str, str, int]]:
    """AREA/VERDICT claims as (feature, verdict, step_at_which_claimed). Parsed with
    the scorer's own regex so the index cannot disagree about what was said; the
    step count here is calls made, a different unit from the scorer's banked_at.
    An `other…` area is mapped onto its hidden feature with the scorer's own
    resolver — otherwise the index under-counts every hidden true positive."""
    from .bugs import _AREA_RE, _normalize_verdict

    out: list[tuple[str, str, int]] = []
    calls = 0
    for kind, payload in events:
        if kind == "call":
            calls += 1
            continue
        for match in _AREA_RE.finditer(str(payload)):
            feature = match.group("area")
            verdict = _normalize_verdict(match.group("verdict"))
            claimed_as = None
            if feature not in known and resolve is not None:
                resolved = resolve(feature, {"actual": str(payload)})
                if resolved:
                    claimed_as, feature = feature, resolved
            if feature in known and verdict is not None:
                out.append((feature, verdict, calls, claimed_as))
    return out


def _probe_steps(steps: list[dict[str, Any]], span: range, probes: list[str]) -> list[int]:
    """Steps in a span mentioning a feature's probe keywords. Coarse on purpose —
    good for jumping to the right neighbourhood, not for attribution."""
    if not probes:
        return []
    lowered = [p.lower() for p in probes]
    hits = []
    for step in steps:
        if step["step"] not in span:
            continue
        blob = json.dumps([step.get("summary"), step.get("result"),
                           step.get("elements")], default=str).lower()
        if any(p in blob for p in lowered):
            hits.append(step["step"])
    return hits


def _evidence_screen(steps: list[dict[str, Any]], at_step: int) -> str | None:
    """Nearest image at or before a claim. Prefers the agent's own screenshot,
    falls back to the independently captured frame."""
    best: str | None = None
    for step in steps:
        if step["step"] > at_step:
            break
        if step.get("screens"):
            best = step["screens"][0]
        elif step.get("frame"):
            best = step["frame"]["path"]
    return best


def _build_findings(
    steps: list[dict[str, Any]],
    events: list[tuple[str, Any]],
    features: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """Index the episode by seeded bug: where each area was tested and how it scored.
    Nothing here influences a score — outcomes are read from the scorer's metrics;
    a disagreement is reported (agrees_with_score), never resolved."""
    from .bugs import hidden_resolver
    known = {str(f.get("id")) for f in features if f.get("id")}
    claims = _claims(events, known, resolve=hidden_resolver(features))
    last_claim = {feature: (verdict, at, claimed_as)
                  for feature, verdict, at, claimed_as in claims}

    # Areas claimed at the same position were written up together, so their segments
    # overlap and none of them can be attributed to one area alone.
    shared: dict[int, int] = {}
    for _verdict, at, _alias in last_claim.values():
        shared[at] = shared.get(at, 0) + 1

    credited = set(metrics.get("found_bug_ids") or [])
    unverified = set(metrics.get("unverified_broken") or [])
    blocked = set(metrics.get("blocked") or [])
    boundaries = sorted({at for _, at, _alias in last_claim.values()})
    last_step = steps[-1]["step"] if steps else 0

    findings = []
    for feature in features:
        fid = str(feature.get("id") or "")
        if not fid:
            continue
        truth = "broken" if feature.get("state") == "broken" else "ok"
        bug_id = feature.get("bug_id")
        claim = last_claim.get(fid)
        verdict = claim[0] if claim else None

        if fid in blocked or verdict == "blocked":
            outcome = "blocked"
        elif verdict is None:
            outcome = "unreported"
        elif truth == "broken":
            outcome = ("true_positive" if (bug_id in credited if bug_id else verdict == "broken")
                       else "claimed_but_ungated" if fid in unverified or bug_id in unverified
                       else "miss")
        else:
            outcome = "false_positive" if verdict == "broken" else "correct_ok"

        entry: dict[str, Any] = {
            "feature": fid,
            "bug_id": bug_id,
            "truth": truth,
            "verdict": verdict,
            "outcome": outcome,
        }

        if claim is None:
            entry["attribution"] = "unattributable"
            findings.append(entry)
            continue

        at = claim[1]
        if claim[2]:
            # The transcript claimed this under an `other…` alias; record it so an
            # independent checker can find the claim without re-deriving the mapping.
            entry["claimed_as"] = claim[2]
        previous = max((b for b in boundaries if b < at), default=0)
        entry["claim_step"] = at
        entry["segment"] = [previous + 1, at]
        entry["scorer_at_call"] = (metrics.get("banked_at") or {}).get(fid)
        entry["probe_steps"] = _probe_steps(
            steps, range(previous + 1, at + 1), feature.get("probe") or [])
        if screen := _evidence_screen(steps, at):
            entry["evidence_screen"] = screen
        # Everything claimed in one closing message has no usable segment: the whole
        # episode is "the segment" for every area at once.
        entry["attribution"] = (
            "batched" if at >= last_step and shared.get(at, 0) > 2
            else "shared" if shared.get(at, 0) > 1
            else "banked"
        )
        findings.append(entry)

    # Contradiction check: the counts come from two independent places, so a
    # mismatch means the index describes a different episode than the score.
    tp = sum(1 for f in findings if f["outcome"] == "true_positive")
    fp = sum(1 for f in findings if f["outcome"] == "false_positive")
    agrees = (tp == (metrics.get("bugs_found") or 0)
              and fp == (metrics.get("false_positives") or 0))

    return {
        "areas": findings,
        "claims_found": len(last_claim),
        "areas_total": len(known),
        "agrees_with_score": agrees,
        "source": "outcomes from result.json metrics; positions from the transcript",
    }


def _attach_frames(run_dir: Path, steps: list[dict[str, Any]]) -> int:
    """Pair step N with the frame recorded at hook count N. The counters can drift;
    drift shows as steps with no frame, never a wrong screenshot, and hook_count
    stays on the step so a pairing can be rechecked."""
    frames = load_frames(run_dir)
    if not frames:
        return 0

    attached = 0
    for step in steps:
        record = frames.get(step["step"])
        if not record:
            continue
        step["frame"] = {
            "path": record["file"],
            "hook_count": record["hook_count"],
            # The transcript has no timestamps, so capture time is what dates a step.
            "captured_at": record.get("captured_at"),
            # Capture can overlap the call it is named for — the screen around the
            # call, not a precise pre-state.
            "when": "around this call",
        }
        attached += 1
    return attached


def _link_neighbouring_screens(steps: list[dict[str, Any]]) -> None:
    """Give a screenshot-less step the nearest frames on either side — a tap returns
    no image and is unreviewable without the screen it was aimed at. These are
    references to another step's frame (from_step), not captures at this instant."""
    owned = [(i, s["screens"][0]) for i, s in enumerate(steps) if s.get("screens")]
    if not owned:
        return

    for index, step in enumerate(steps):
        if step.get("screens"):
            continue
        before = [(i, p) for i, p in owned if i < index]
        after = [(i, p) for i, p in owned if i > index]
        if before:
            i, path = before[-1]
            step["screen_before"] = {"path": path, "from_step": steps[i]["step"]}
        if after:
            i, path = after[0]
            step["screen_after"] = {"path": path, "from_step": steps[i]["step"]}


# ── entry point ───────────────────────────────────────────────────────────────


def write_episode_evidence(
    run_dir: Path,
    transcript: str,
    *,
    meta: dict[str, Any] | None = None,
    secrets: tuple[str, ...] = (),
    features: list[dict[str, Any]] | None = None,
    metrics: dict[str, Any] | None = None,
) -> Path | None:
    """Write <run_dir>/evidence/ for one episode. Returns the dir, or None.
    meta is stored verbatim; secrets are plaintext values to strip. features +
    metrics enable the per-bug findings.json index; absent just means no index."""
    out = run_dir / "evidence"
    screens = out / "screens"
    try:
        screens.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("evidence: could not create %s: %s", out, exc)
        return None

    steps: list[dict[str, Any]] = []
    shots = 0

    events = _walk(transcript)

    for index, call in enumerate((c for kind, c in events if kind == "call"), start=1):
        if not call.name:
            continue
        try:
            args, redacted = _redact_record(call.args)
            step: dict[str, Any] = {
                "step": index,
                "kind": _kind(call.name),
                "tool": call.name,
                "summary": _sanitize_payload(_summary(call), call.args),
                "args": args,
                "ok": bool(call.ok),
            }
            if redacted:
                step["args_redacted"] = True

            paths = []
            for offset, (raw, ext) in enumerate(_images(call.content)):
                name = f"{index:04d}.{ext}" if offset == 0 else f"{index:04d}_{offset + 1}.{ext}"
                (screens / name).write_bytes(raw)
                paths.append(f"screens/{name}")
            if paths:
                step["screens"] = paths
                shots += len(paths)

            observation = _observation(call)
            step.update(observation)

            # An observe reply IS its elements — keeping the raw text too would store
            # the same screen twice.
            text = "" if observation else clean_result_text(call.text)
            if text:
                step["result"] = _sanitize_payload(text[:_RESULT_CHARS], call.args)

            steps.append(_scrub(step, secrets))
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("evidence: skipped step %d (%s): %s", index, call.name, exc)

    _link_neighbouring_screens(steps)
    framed = _attach_frames(run_dir, steps)

    findings: dict[str, Any] | None = None
    if features:
        try:
            findings = _build_findings(steps, events, features, metrics or {})
        except Exception as exc:  # noqa: BLE001 - an index is a convenience, not a result
            logger.warning("evidence: per-bug index not built: %s", exc)

    try:
        with (out / "steps.jsonl").open("w") as handle:
            for step in steps:
                handle.write(json.dumps(step) + "\n")

        if findings is not None:
            (out / "findings.json").write_text(json.dumps(findings, indent=2))
            if not findings["agrees_with_score"]:
                logger.warning(
                    "evidence: per-bug index disagrees with the scored result for %s — "
                    "the index is indicative only; result.json is authoritative",
                    run_dir.name)

        (out / "meta.json").write_text(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "episode": _scrub(meta or {}, secrets),
            "counts": {
                "steps": len(steps),
                "screenshots": shots,
                "frames": framed,
                "actions": sum(1 for s in steps if s["kind"] in ("action", "command")),
                "observations": sum(1 for s in steps if s["kind"] == "observe"),
            },
        }, indent=2))
    except OSError as exc:
        logger.warning("evidence: could not write bundle in %s: %s", out, exc)
        return None

    # Manifest before page: the page quotes the digests, and the page itself is
    # derived so it is not covered by them.
    write_manifest(out)
    render_report(out)

    logger.info("evidence: %d step(s), %d screenshot(s) → %s", len(steps), shots, out)
    return out
