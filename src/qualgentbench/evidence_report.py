"""evidence/index.html — the bundle as one readable page: findings up top, then
every step with its screen. Images are referenced, not inlined, so the shareable
unit is the evidence/ dir; rendered from the bundle, so regenerating is safe."""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Outcomes carry the meaning of the whole page, so they get the only strong colour.
_OUTCOME_CLASS = {
    "true_positive": "good",
    "correct_ok": "good",
    "false_positive": "bad",
    "miss": "bad",
    "claimed_but_ungated": "warn",
    "blocked": "warn",
    "unreported": "muted",
}

_KIND_CLASS = {"observe": "k-observe", "action": "k-action",
               "command": "k-action", "report": "k-report"}

_CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#111; --dim:#666; --line:#e2e2e2;
        --panel:#f7f7f8; --good:#0a7f3f; --bad:#c02626; --warn:#a86400; --accent:#2c5fd6; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16181c; --fg:#e8e8ea; --dim:#9a9aa2; --line:#2c2f36; --panel:#1e2127;
          --good:#4ec27f; --bad:#ff6b6b; --warn:#e0a33a; --accent:#7aa2f7; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,
       BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 24px 20px 80px; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 32px 0 10px; text-transform: uppercase;
     letter-spacing: .06em; color: var(--dim); }
.sub { color: var(--dim); margin-bottom: 20px; }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
         gap: 1px; background: var(--line); border: 1px solid var(--line);
         border-radius: 8px; overflow: hidden; }
.fact { background: var(--panel); padding: 10px 12px; }
.fact .l { color: var(--dim); font-size: 11px; text-transform: uppercase;
           letter-spacing: .05em; }
.fact .v { font-size: 15px; margin-top: 2px; word-break: break-word; }
table.budget { width: 100%; border-collapse: collapse; margin: 8px 0 4px; }
table.budget td { padding: 3px 8px 3px 0; vertical-align: middle; }
table.budget td.k { color: var(--dim); width: 90px; }
table.budget td.n { text-align: right; width: 52px; font-variant-numeric: tabular-nums; }
table.budget td.pct { text-align: right; width: 44px; color: var(--dim);
                      font-variant-numeric: tabular-nums; }
table.budget td.bar { width: auto; }
table.budget td.bar span { display: block; height: 9px; background: var(--accent, #5b8def); }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th { text-align: left; font-weight: 600; color: var(--dim); font-size: 11px;
     text-transform: uppercase; letter-spacing: .05em; padding: 6px 10px;
     border-bottom: 1px solid var(--line); }
td { padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: middle; }
td.thumb { width: 48px; padding: 4px 10px; }
td.thumb img { height: 56px; width: auto; border-radius: 3px;
               border: 1px solid var(--line); display: block; }
.good { color: var(--good); font-weight: 600; }
.bad { color: var(--bad); font-weight: 600; }
.warn { color: var(--warn); font-weight: 600; }
.muted { color: var(--dim); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.bar { position: sticky; top: 0; background: var(--bg); padding: 10px 0;
       border-bottom: 1px solid var(--line); z-index: 5; display: flex; gap: 8px;
       align-items: center; flex-wrap: wrap; }
button { font: inherit; font-size: 12px; padding: 4px 12px; border-radius: 999px;
         border: 1px solid var(--line); background: var(--panel); color: var(--fg);
         cursor: pointer; }
button.on { background: var(--accent); border-color: var(--accent); color: #fff; }
.step { display: flex; gap: 14px; padding: 12px 0; border-bottom: 1px solid var(--line);
        scroll-margin-top: 60px; }
.step.hit { background: color-mix(in srgb, var(--accent) 9%, transparent); }
.n { color: var(--dim); font-variant-numeric: tabular-nums; min-width: 40px;
     font-size: 12px; padding-top: 2px; }
.n .t { font-size: 11px; opacity: .65; margin-top: 2px; }
.verdict { font-size: 17px; margin: 4px 0 18px; line-height: 1.45; }
.verdict .muted { font-size: 13px; }
.timeline { display: flex; gap: 3px; margin-bottom: 10px; }
.seg { flex-basis: 0; min-width: 0; padding: 9px 8px; border-radius: 5px;
       background: var(--panel); border: 1px solid currentColor; text-decoration: none;
       overflow: hidden; }
.seg span { display: block; font-size: 11px; font-weight: 600; white-space: nowrap;
            overflow: hidden; text-overflow: ellipsis; }
.seg small { display: block; color: var(--dim); font-size: 10px; margin-top: 2px; }
.block { margin: 20px 0 0; padding: 8px 12px; border-left: 3px solid currentColor;
         background: var(--panel); border-radius: 0 5px 5px 0; font-size: 12px; }
.block b { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.body { flex: 1; min-width: 0; }
.sum { font-weight: 600; }
.tag { display: inline-block; font-size: 10px; text-transform: uppercase;
       letter-spacing: .06em; padding: 1px 7px; border-radius: 999px;
       border: 1px solid var(--line); color: var(--dim); margin-left: 8px;
       vertical-align: 1px; }
.k-observe { border-color: var(--accent); color: var(--accent); }
.k-action  { border-color: var(--good); color: var(--good); }
.k-report  { border-color: var(--warn); color: var(--warn); }
.meta { color: var(--dim); font-size: 12px; margin-top: 3px; word-break: break-word; }
.fail { color: var(--bad); }
.shots { display: flex; gap: 8px; }
.shots figure { margin: 0; text-align: center; }
.shots img { height: 168px; width: auto; max-width: 150px; border-radius: 4px;
             border: 1px solid var(--line); display: block; background: var(--panel);
             object-fit: contain; }
.shots figcaption { color: var(--dim); font-size: 10px; margin-top: 3px; }
.ref img { opacity: .45; }
details.els { margin-top: 5px; }
details.els summary { color: var(--dim); font-size: 12px; cursor: pointer; }
details.els div { font-size: 12px; color: var(--dim); margin-top: 4px;
                  max-height: 220px; overflow: auto; }
.note { color: var(--dim); font-size: 12px; margin-top: 8px; }
a { color: var(--accent); }
"""

_JS = """
const steps = [...document.querySelectorAll('.step')];
document.querySelectorAll('[data-filter]').forEach(b => b.onclick = () => {
  document.querySelectorAll('[data-filter]').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  const f = b.dataset.filter;
  steps.forEach(s => s.style.display =
    (f === 'all' || s.dataset.kind === f || (f === 'action' && s.dataset.kind === 'command'))
      ? '' : 'none');
});
addEventListener('hashchange', mark); mark();
function mark() {
  steps.forEach(s => s.classList.remove('hit'));
  const el = location.hash && document.querySelector(location.hash);
  if (el && el.classList.contains('step')) el.classList.add('hit');
}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return []
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _facts(episode: dict[str, Any], counts: dict[str, Any]) -> str:
    seconds = episode.get("wall_time_sec")
    bugs = episode.get("active_bugs") or []
    pairs = [
        ("App", episode.get("app") or "—"),
        ("Agent", episode.get("agent") or "—"),
        ("Model", episode.get("model") or "—"),
        ("Condition", episode.get("condition") or "—"),
        ("Device", episode.get("device") or "—"),
        ("Score", "—" if episode.get("score") is None else f'{episode["score"]:.2f}'),
        ("Steps", f'{counts.get("steps", 0)} / {episode.get("step_budget") or "—"}'),
        ("Wall time", f"{seconds / 60:.1f} min" if isinstance(seconds, (int, float)) else "—"),
        ("Screens", f'{counts.get("screenshots", 0)} agent / {counts.get("frames", 0)} captured'),
        ("Seeded bugs", ", ".join(str(b) for b in bugs) if bugs else "—"),
    ]
    cells = "".join(
        f'<div class="fact"><div class="l">{_e(label)}</div>'
        f'<div class="v">{_e(value)}</div></div>'
        for label, value in pairs
    )
    return f'<div class="facts">{cells}</div>'


_KIND_ORDER = ("tap", "type", "press", "swipe", "observe", "launch", "terminate", "other")


def _budget_breakdown(episode: dict[str, Any]) -> str:
    """Where the step budget went, by interaction kind. The total alone makes an
    exhausted budget unreadable — the breakdown shows what actually ate it."""
    data = episode.get("interactions") or {}
    total = data.get("interactions") or 0
    if not total:
        return ""
    rows = [(k, data.get(f"interactions_{k}") or 0) for k in _KIND_ORDER]
    rows = [(k, n) for k, n in rows if n]
    if not rows:
        return ""
    rows.sort(key=lambda kv: -kv[1])
    budget = episode.get("step_budget")
    top_kind, top_n = rows[0]
    share = top_n / total * 100

    bars = "".join(
        f'<tr><td class="k">{_e(kind)}</td>'
        f'<td class="n">{n}</td>'
        f'<td class="bar"><span style="width:{n / total * 100:.1f}%"></span></td>'
        f'<td class="pct">{n / total * 100:.0f}%</td></tr>'
        for kind, n in rows
    )
    note = ""
    if episode.get("truncated") and share >= 50:
        note = (f'<p class="note bad">Budget exhausted: {share:.0f}% of it went on '
                f'<b>{_e(top_kind)}</b> ({top_n} of {total}). '
                f'The episode ran out before it could report.</p>')
    elif share >= 50:
        note = (f'<p class="note">{share:.0f}% of the work was <b>{_e(top_kind)}</b>.</p>')

    cap = f" of a {budget} budget" if budget else ""
    return (f'<h2>Where the budget went</h2>'
            f'<p class="muted">{total} interaction(s){_e(cap)} — one tap, one text '
            f'entry, one screen read each.</p>'
            f'<table class="budget">{bars}</table>{note}')


def _findings_table(findings: dict[str, Any]) -> str:
    areas = findings.get("areas") or []
    if not areas:
        return ""

    rows = []
    for area in areas:
        outcome = str(area.get("outcome") or "")
        claim = area.get("claim_step")
        where = (f'<a href="#s{_e(claim)}">step {_e(claim)}</a>'
                 if isinstance(claim, int) else '<span class="muted">never claimed</span>')
        segment = area.get("segment")
        span = f"{segment[0]}–{segment[1]}" if isinstance(segment, list) and len(segment) == 2 else "—"
        shot = area.get("evidence_screen")
        thumb = (f'<a href="{_e(shot)}"><img src="{_e(shot)}" loading="lazy" alt=""></a>'
                 if shot else "")
        rows.append(
            "<tr>"
            f'<td class="mono">{_e(area.get("feature"))}</td>'
            f'<td>{_e(area.get("truth"))}</td>'
            f'<td>{_e(area.get("verdict") or "—")}</td>'
            f'<td class="{_OUTCOME_CLASS.get(outcome, "muted")}">{_e(outcome.replace("_", " "))}</td>'
            f"<td>{where}</td>"
            f'<td class="mono">{_e(span)}</td>'
            f'<td class="muted">{_e(area.get("attribution"))}</td>'
            f'<td class="thumb">{thumb}</td>'
            "</tr>"
        )

    warning = ""
    if findings.get("agrees_with_score") is False:
        warning = ('<p class="note bad">This index disagrees with the scored result. '
                   "result.json is authoritative; treat the table as indicative.</p>")

    return (
        "<h2>Findings</h2>"
        "<table><thead><tr><th>Area</th><th>Truth</th><th>Verdict</th><th>Outcome</th>"
        "<th>Claimed</th><th>Segment</th><th>Attribution</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f"{warning}"
        '<p class="note">Outcomes are read from the scorer\'s own metrics. Only the '
        "positions — claim step, segment, screenshot — are derived from the transcript. "
        "<em>Attribution</em> says how cleanly a claim can be tied to one stretch of the "
        "episode: <em>banked</em> alone, <em>shared</em> with other areas at the same "
        "point, <em>batched</em> in one closing write-up.</p>"
    )


def _verdict_line(episode: dict[str, Any], findings: dict[str, Any],
                  counts: dict[str, Any]) -> str:
    """One plain sentence answering "how did it do" — a reader who stops here
    should still have the result."""
    areas = findings.get("areas") or []
    seeded = sum(1 for a in areas if a.get("truth") == "broken")
    found = sum(1 for a in areas if a.get("outcome") == "true_positive")
    false = sum(1 for a in areas if a.get("outcome") == "false_positive")
    missed = sum(1 for a in areas if a.get("outcome") in ("miss", "claimed_but_ungated"))
    silent = sum(1 for a in areas if a.get("outcome") == "unreported")

    if not areas:
        headline = "No per-area index for this episode."
    else:
        headline = f"Found <b>{found} of {seeded}</b> seeded bugs"
        tail = []
        if false:
            tail.append(f"<span class='bad'>{false} false report(s)</span>")
        if missed:
            tail.append(f"<span class='bad'>{missed} missed</span>")
        if silent:
            tail.append(f"<span class='muted'>{silent} area(s) never reported</span>")
        headline += (" · " + " · ".join(tail)) if tail else " · no false reports"

    steps = counts.get("steps", 0)
    budget = episode.get("step_budget")
    seconds = episode.get("wall_time_sec")
    pace = f"{steps} steps"
    if budget:
        pace += f" of a {budget} budget"
    if isinstance(seconds, (int, float)):
        pace += f" in {seconds / 60:.1f} min"

    flags = [name for name, on in (("truncated", episode.get("truncated")),
                                   ("timed out", episode.get("timed_out")),
                                   ("left the app", episode.get("off_app"))) if on]
    caveat = (f' · <span class="bad">{", ".join(flags)}</span>' if flags
              else ' · <span class="good">episode valid</span>')

    return f'<p class="verdict">{headline}<br><span class="muted">{pace}{caveat}</span></p>'


def _blocks(findings: dict[str, Any]) -> list[dict[str, Any]]:
    """Areas grouped by the stretch they were claimed from. Areas written up
    together share one segment, so the segment is the honest unit here."""
    grouped: dict[tuple[int, int], dict[str, Any]] = {}
    for area in findings.get("areas") or []:
        segment = area.get("segment")
        if not (isinstance(segment, list) and len(segment) == 2):
            continue
        key = (segment[0], segment[1])
        block = grouped.setdefault(key, {"start": key[0], "end": key[1], "areas": []})
        block["areas"].append(area)
    return sorted(grouped.values(), key=lambda b: (b["start"], b["end"]))


def _worst(areas: list[dict[str, Any]]) -> str:
    for outcome in ("false_positive", "miss", "claimed_but_ungated", "blocked"):
        if any(a.get("outcome") == outcome for a in areas):
            return _OUTCOME_CLASS.get(outcome, "muted")
    return "good"


def _timeline(findings: dict[str, Any], last_step: int) -> str:
    """A proportional strip of the episode: where each area was investigated."""
    blocks = _blocks(findings)
    if not blocks or last_step <= 0:
        return ""

    cells = []
    for block in blocks:
        width = max(1, block["end"] - block["start"] + 1)
        names = ", ".join(str(a.get("feature")) for a in block["areas"])
        verdicts = {str(a.get("verdict")) for a in block["areas"]}
        label = f'{names} → {"/".join(sorted(verdicts))}'
        cells.append(
            f'<a class="seg {_worst(block["areas"])}" style="flex-grow:{width}" '
            f'href="#s{block["end"]}" title="{_e(label)} (steps {block["start"]}–{block["end"]})">'
            f'<span>{_e(names)}</span>'
            f'<small>{block["start"]}–{block["end"]}</small></a>'
        )
    return ('<h2>What happened when</h2>'
            f'<div class="timeline">{"".join(cells)}</div>'
            '<p class="note">Each block is one stretch of the episode and the area(s) '
            "claimed from it — click to jump to the step where the claim was made. "
            "Blocks covering several areas mean those verdicts were written up together, "
            "so the evidence for them overlaps.</p>")


def _integrity(evidence_dir: Path) -> str:
    """The manifest's digests, quoted on the page so they travel with what they cover."""
    manifest = _read_json(evidence_dir / "manifest.json")
    if not manifest:
        return ""
    steps = manifest.get("steps") or {}
    head = str(steps.get("head") or "")
    files = manifest.get("files") or {}
    if not head and not files:
        return ""
    return (
        '<h2>Integrity</h2><p class="note">'
        f'<span class="mono">{len(files)}</span> file(s) covered by '
        '<a href="manifest.json">manifest.json</a> · steps chain head '
        f'<span class="mono">{_e(head[:16])}…</span><br>'
        "Re-hash this bundle with "
        '<span class="mono">evidence_manifest.verify_bundle(&lt;dir&gt;)</span>. '
        "The manifest is written by the run that produced the bundle, so it does not "
        "prove the run happened — it detects edits made after the digests were quoted "
        "somewhere outside this directory."
        "</p>"
    )


def _result_line(result: Any) -> str:
    """First useful line of a tool reply. Replies wrap the text in a JSON envelope
    plus the same coaching tip every step; left whole they triple the page height."""
    if not result:
        return ""
    text = str(result)
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and isinstance(payload.get("result"), str):
            text = payload["result"]
    except ValueError:
        pass
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:240]


def _shots(step: dict[str, Any]) -> str:
    """Images for one step: own screenshot, captured frame, or dimmed references
    labelled with their source step — a stale screen must never read as current."""
    figures = []
    for path in step.get("screens") or []:
        figures.append((path, "agent saw", False))
    frame = step.get("frame")
    if isinstance(frame, dict) and frame.get("path"):
        figures.append((frame["path"], "captured", False))
    if not figures:
        for key, label in (("screen_before", "before"), ("screen_after", "after")):
            ref = step.get(key)
            if isinstance(ref, dict) and ref.get("path"):
                figures.append((ref["path"], f'{label} · step {ref.get("from_step")}', True))

    if not figures:
        return ""
    cells = "".join(
        f'<figure class="{"ref" if faded else ""}"><a href="{_e(path)}">'
        f'<img src="{_e(path)}" loading="lazy" alt=""></a>'
        f"<figcaption>{_e(caption)}</figcaption></figure>"
        for path, caption, faded in figures
    )
    return f'<div class="shots">{cells}</div>'


def _elapsed(step: dict[str, Any], start: datetime | None) -> str:
    """``m:ss`` into the episode, from the frame capture clock. Empty if unknown."""
    frame = step.get("frame")
    if not (start and isinstance(frame, dict) and frame.get("captured_at")):
        return ""
    try:
        seconds = (datetime.fromisoformat(frame["captured_at"]) - start).total_seconds()
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        return ""
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _block_header(block: dict[str, Any]) -> str:
    """The marker that opens an area's stretch of the trajectory."""
    names = ", ".join(str(a.get("feature")) for a in block["areas"])
    outcomes = " · ".join(sorted({str(a.get("outcome") or "").replace("_", " ")
                                  for a in block["areas"]}))
    return (f'<div class="block {_worst(block["areas"])}">'
            f'<b>{_e(names)}</b> · steps {block["start"]}–{block["end"]} · '
            f'{_e(outcomes)}</div>')


def _step_html(step: dict[str, Any], start: datetime | None = None) -> str:
    number = step.get("step")
    kind = str(step.get("kind") or "other")

    meta = [f'<span class="mono">{_e(step.get("tool"))}</span>']
    if not step.get("ok", True):
        meta.append('<span class="fail">failed</span>')
    if step.get("args_redacted"):
        meta.append("args redacted")
    if result := _result_line(step.get("result")):
        meta.append(_e(result))

    elements = step.get("elements")
    element_list = ""
    if isinstance(elements, list) and elements:
        joined = "<br>".join(_e(item) for item in elements[:80])
        element_list = (f'<details class="els"><summary>{len(elements)} element(s)</summary>'
                        f'<div class="mono">{joined}</div></details>')

    at = _elapsed(step, start)
    return (
        f'<div class="step" id="s{_e(number)}" data-kind="{_e(kind)}">'
        f'<div class="n"><a href="#s{_e(number)}">{_e(number)}</a>'
        f'{f"<div class=t>{_e(at)}</div>" if at else ""}</div>'
        f'<div class="body"><div class="sum">{_e(step.get("summary"))}'
        f'<span class="tag {_KIND_CLASS.get(kind, "")}">{_e(kind)}</span></div>'
        f'<div class="meta">{" · ".join(m for m in meta if m)}</div>'
        f"{element_list}</div>"
        f"{_shots(step)}"
        "</div>"
    )


def _backfill_capture_times(evidence_dir: Path, steps: list[dict[str, Any]]) -> None:
    """Recover capture times for bundles written before steps carried them —
    the frame index has always recorded captured_at."""
    if any((step.get("frame") or {}).get("captured_at") for step in steps):
        return
    index = _read_jsonl(evidence_dir / "frames" / "index.jsonl")
    times = {row["hook_count"]: row.get("captured_at") for row in index
             if isinstance(row.get("hook_count"), int)}
    for step in steps:
        frame = step.get("frame")
        if isinstance(frame, dict) and times.get(frame.get("hook_count")):
            frame["captured_at"] = times[frame["hook_count"]]


def _trajectory(steps: list[dict[str, Any]], findings: dict[str, Any],
                episode: dict[str, Any]) -> str:
    """The step list, with a marker wherever a new area's stretch begins."""
    start = None
    if isinstance(episode.get("started_at"), str):
        try:
            start = datetime.fromisoformat(episode["started_at"])
        except ValueError:
            start = None

    headers = {block["start"]: _block_header(block) for block in _blocks(findings)}
    out = []
    for step in steps:
        if header := headers.get(step.get("step")):
            out.append(header)
        out.append(_step_html(step, start))
    return "".join(out)


def render_report(evidence_dir: Path) -> Path | None:
    """Write index.html into an existing bundle. Returns the path, or None.
    Best-effort: a failed render must never disturb a scored episode."""
    try:
        meta = _read_json(evidence_dir / "meta.json")
        steps = _read_jsonl(evidence_dir / "steps.jsonl")
        _backfill_capture_times(evidence_dir, steps)
        findings = _read_json(evidence_dir / "findings.json")
        if not meta and not steps:
            return None

        episode = meta.get("episode") if isinstance(meta.get("episode"), dict) else {}
        counts = meta.get("counts") if isinstance(meta.get("counts"), dict) else {}
        title = f'{episode.get("app") or "Episode"} · {episode.get("model") or ""}'.strip(" ·")

        passed = episode.get("passed")
        status = ("passed" if passed else "failed") if passed is not None else "unscored"
        subtitle = (f'{episode.get("task_id") or ""} · {status} · '
                    f'{episode.get("started_at") or ""}').strip(" ·")

        page = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{_e(title)}</title><style>{_CSS}</style></head><body><div class=\"wrap\">"
            f"<h1>{_e(title)}</h1>"
            f'<div class="sub">{_e(subtitle)}</div>'
            f"{_verdict_line(episode, findings, counts)}"
            f"{_facts(episode, counts)}"
            f"{_budget_breakdown(episode)}"
            f"{_timeline(findings, steps[-1]['step'] if steps else 0)}"
            f"{_findings_table(findings)}"
            f"<h2>Trajectory · {len(steps)} steps</h2>"
            '<div class="bar">'
            '<button data-filter="all" class="on">All</button>'
            '<button data-filter="action">Actions</button>'
            '<button data-filter="observe">Observations</button>'
            '<button data-filter="report">Report</button>'
            "</div>"
            f"{_trajectory(steps, findings, episode)}"
            '<p class="note">Screenshots labelled <em>agent saw</em> come from the model\'s '
            "own tool replies; <em>captured</em> frames were taken independently from the "
            "device during the run. Dimmed images are a neighbouring step's screen, not this "
            "step's.</p>"
            f"{_integrity(evidence_dir)}"
            f"</div><script>{_JS}</script></body></html>"
        )
        out = evidence_dir / "index.html"
        out.write_text(page)
        return out
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("evidence: report not rendered for %s: %s", evidence_dir, exc)
        return None
