#!/usr/bin/env python3
"""Check that an episode's evidence bundle actually matches its transcript, re-deriving
from the raw transcript with an independent parser (no ``episode_evidence`` import) so
a bundle-writer bug cannot confirm itself. Prototype of the submission ingest check."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from pathlib import Path

AREA_RE = re.compile(r"AREA:\s*(?P<area>[a-z0-9_]+)\s*\|\s*VERDICT:\s*(?P<verdict>[A-Za-z ]+)")


def normalize(verdict: str) -> str:
    """"AS EXPECTED" -> ok, "DEVIATES FROM EXPECTED" -> broken. Kept independent of
    bugs.py on purpose — importing the scorer's normaliser would test it against itself."""
    verdict = verdict.strip().lower()
    if verdict.startswith("as"):
        return "ok"
    if verdict.startswith("deviates"):
        return "broken"
    if verdict.startswith("blocked"):
        return "blocked"
    return verdict


def parse(transcript: str) -> tuple[list[str], list[str], list[tuple[int, str]]]:
    """(tool names in call order, base64 images, (calls_so_far, agent text))."""
    calls: list[str] = []
    images: list[str] = []
    texts: list[tuple[int, str]] = []

    for line in transcript.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        kind = event.get("type")
        if kind == "item.completed":                                    # codex
            item = event.get("item") or {}
            itype = item.get("type")
            if itype == "mcp_tool_call":
                calls.append(item.get("tool"))
                for block in ((item.get("result") or {}).get("content") or []):
                    if isinstance(block, dict) and block.get("type") == "image" and block.get("data"):
                        images.append(block["data"])
            elif itype == "command_execution":
                calls.append("command_execution")
            elif itype == "agent_message":
                texts.append((len(calls), item.get("text") or ""))
        elif kind == "assistant":                                       # claude code
            for block in ((event.get("message") or {}).get("content") or []):
                if block.get("type") == "tool_use":
                    calls.append(block.get("name"))
                elif block.get("type") == "text":
                    texts.append((len(calls), block.get("text") or ""))
        elif kind == "user":
            for block in ((event.get("message") or {}).get("content") or []):
                if block.get("type") != "tool_result":
                    continue
                content = block.get("content")
                for part in (content if isinstance(content, list) else []):
                    if not isinstance(part, dict) or part.get("type") != "image":
                        continue
                    data = part.get("data") or (part.get("source") or {}).get("data")
                    if data:
                        images.append(data)

    return calls, images, texts


def validate(run: Path) -> list[str]:
    evidence = run / "evidence"
    transcript = (run / "agent" / "transcript.txt").read_text(errors="replace")
    steps = [json.loads(line) for line in (evidence / "steps.jsonl").read_text().splitlines() if line.strip()]
    meta = json.loads((evidence / "meta.json").read_text())
    result = json.loads((run / "result.json").read_text())

    calls, images, texts = parse(transcript)
    failures: list[str] = []

    print(f"transcript: {len(calls)} tool calls, {len(images)} images, {len(texts)} agent messages")
    print(f"bundle:     {len(steps)} steps, {meta['counts']['screenshots']} screenshots")

    if len(calls) != len(steps):
        failures.append(f"COUNT: transcript has {len(calls)} calls, bundle has {len(steps)} steps")

    mismatches = [i + 1 for i, (name, step) in enumerate(zip(calls, steps)) if name != step["tool"]]
    print(f"order/name mismatches: {len(mismatches)}")
    if mismatches:
        failures.append(f"ORDER: steps diverge from the transcript at step {mismatches[0]}")

    # A stored screenshot must be a byte-exact decode of a blob the model was actually sent.
    sent = set()
    for blob in images:
        try:
            sent.add(hashlib.sha256(base64.b64decode(blob, validate=True)).hexdigest())
        except (ValueError, TypeError):
            pass
    stored = sorted((evidence / "screens").glob("*"))
    unmatched = [p.name for p in stored if hashlib.sha256(p.read_bytes()).hexdigest() not in sent]
    print(f"screens: {len(stored)} on disk, {len(stored) - len(unmatched)} byte-exact decodes of transcript blobs")
    if unmatched:
        failures.append(f"SCREENS: {len(unmatched)} not present in the transcript: {unmatched[:3]}")

    # Frames are ours, captured out-of-band. Finding one inside the transcript would mean
    # the "independent" source was really the agent's own image.
    frames = sorted((evidence / "frames").glob("*.jpg"))
    leaked = [p.name for p in frames if hashlib.sha256(p.read_bytes()).hexdigest() in sent]
    print(f"frames:  {len(frames)} on disk, {len(frames) - len(leaked)} independent of the transcript")
    if leaked:
        failures.append(f"FRAMES: {len(leaked)} are copies of transcript images: {leaked[:3]}")

    findings_path = evidence / "findings.json"
    if findings_path.exists():
        findings = json.loads(findings_path.read_text())
        claims = {}
        for at, text in texts:
            for match in AREA_RE.finditer(text):
                claims[match.group("area")] = (normalize(match.group("verdict")), at)
        print(f"AREA claims in transcript: {len(claims)}; areas in index: {len(findings['areas'])}")

        for area in findings["areas"]:
            claimed = claims.get(area.get("claimed_as") or area["feature"])
            if claimed is None:
                if area.get("claim_step") is not None:
                    failures.append(f"CLAIM: {area['feature']} indexed at step "
                                    f"{area['claim_step']} but never claimed in the transcript")
                continue
            verdict, at = claimed
            if area.get("claim_step") != at:
                failures.append(f"CLAIM: {area['feature']} indexed at step "
                                f"{area.get('claim_step')}, transcript says {at}")
            if area.get("verdict") != verdict:
                failures.append(f"VERDICT: {area['feature']} index says {area.get('verdict')}, "
                                f"transcript says {verdict}")

        metrics = result.get("metrics", result)
        found = sum(1 for a in findings["areas"] if a["outcome"] == "true_positive")
        print(f"result.json: bugs_found={metrics.get('bugs_found')} "
              f"fp={metrics.get('false_positives')} score={result.get('score')}")
        if found != metrics.get("bugs_found"):
            failures.append(f"SCORE: index shows {found} true positives, "
                            f"result.json says {metrics.get('bugs_found')}")

    return failures


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    worst = 0
    for target in sys.argv[1:]:
        run = Path(target)
        print(f"=== {run.name} ===")
        try:
            failures = validate(run)
        except (OSError, ValueError, KeyError) as exc:
            print(f"  could not validate: {exc}\n")
            worst = 1
            continue
        print(f"\nFAILURES: {len(failures)}")
        for failure in failures:
            print(f"  - {failure}")
        print()
        worst = max(worst, 1 if failures else 0)

    return worst


if __name__ == "__main__":
    raise SystemExit(main())
