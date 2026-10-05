"""`qualgent-bench view` — a static, local site of saved episodes (QUA-2823).

What an operator needs to vet what the agents actually did, for one run or several:
an index with one row per episode, and a page per episode with the recorded-vs-rescored
verdict, the scored reports, the brief, the findings file and the whole transcript —
agent messages, reasoning, every tool call and result, and every image the agent
received, extracted into files beside the page.

Built on the harness's own readers, never a second parser: the transcript goes through
`transcript.timeline` (both CLI formats, both arms), the rescored verdict is
`rescore.rescore(..., dry_run=True)` — exactly what `scripts/rescore_journey.py
--dry-run` prints — and every episode dir is `result.resolve_artifact_dir`. The saved
runs are READ-ONLY input: the only thing written is the output directory.

The output shows EVERYTHING, held-out episodes and the answer key (matched bug ids)
included. Whoever holds the held-out episodes already holds the split, so the page
tells its reader nothing new; the risk is sharing the output, which is why held-out
rows carry a "do not share" badge and held-out pages a banner. It stays INSIDE the runs
root by default (`<runs>/_runs/<run_id>/view/`, or `<runs>/_runs/_view/` for several
runs or all of them): any agent read under the runs root outside its own episode is a
hard `other_episode` contamination hit, so an agent that went looking would void its
own episode. Outside the runs root nothing catches that read, so `--out` there is
refused unless `--allow-outside-runs` says so.

Stdlib only, no server, no CDN: open `index.html` from disk, or serve the directory.
`--portable` also copies each episode's raw transcript, result.json and harness evidence
folder beside its page and drops the links into the runs tree, so the output folder
works on its own (a zip, a static host). A portable view holds exactly as much as a
local one — the answer key and any held-out episodes — so it goes only to people who
may see the held-out split. Every view writes `manifest.json`, a machine-readable
summary for whatever indexes published views.

`--experiment <name>` (QUA-2869) views one CreateBench A/B experiment instead of runs:
every episode its state file names — each cell's creation episode(s) and the five grade
runs of its authored case, which live in many runs (one per creation episode, plus the
driver's) — with the A/B report (`report.html`, `report.json`, from `create/ab.py`), the
experiment's create board (`create.html`) and a per-cell table, all linked from one
index. Default output: `<runs>/_runs/_create/ab/<name>/view/`.

A portable view never carries an episode's `private/` folder (the creation arm's
developer instructions, private text from QualGent-MCP / DevLoop-MCP; `create/arm.py`),
and before its manifest is written every text file in it is checked for runs of that
text (`private_text_hits`, PRIVATE_WINDOW words). A hit fails the build with no manifest,
so the folder cannot be published by mistake. An experiment view is also scanned for
credential markers (`credential_hits`) and refused the same way.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import logging
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import pathname2url

from . import __version__, corpus, journey
from .checkpoint import run_meta_dir
from .failures import exclusion_reason
from .leaderboard import load_results
from .result import RunResult, resolve_artifact_dir
from .transcript import TimelineEntry, timeline

logger = logging.getLogger(__name__)

#: `<runs>/_runs/<run_id>/<VIEW_DIRNAME>/` — one run's view, beside its board.json.
VIEW_DIRNAME = "view"
#: `<runs>/_runs/<MULTI_VIEW_DIRNAME>/` — the view of several runs, or of all of them.
MULTI_VIEW_DIRNAME = "_view"
#: Written into every output dir this module builds. A rebuild only clears a directory
#: that carries it, so `--out` can never wipe a directory the view did not make.
MARKER = ".qualgentbench-view"

HELDOUT_BADGE = "held-out — do not share"
HELDOUT_BANNER = ("Held-out episode — do not share this page, its screenshots or anything "
                  "quoted from it. The held-out split stays with its holders (docs/heldout.md).")
LOCAL_ONLY_NOTE = ("Local only. This view shows the answer key (matched bug ids) and, when "
                   "the split is present, held-out episodes. Do not upload or share it.")
PORTABLE_NOTE = ("This view shows the answer key (matched bug ids) and, when the split is "
                 "present, held-out episodes. Share it only with people who may see the "
                 "held-out split.")
#: `<out>/<MANIFEST>` — what the view holds, for tools that index published views.
MANIFEST = "manifest.json"
#: Format 1. An experiment view (QUA-2869) writes the same keys — `runs` then holds ONE
#: entry for the whole experiment, its `run_id` the experiment name, so an index of
#: published views lists it as one row — plus `kind: "experiment"` and an `experiment`
#: block (`_experiment_manifest`). Additive, so the format is unchanged.
MANIFEST_FORMAT = 1
#: A creation episode's saved case (`create/runner.py`), shown on its page.
AUTHORED_CASE = "authored_case.json"
#: An episode's private folder (`create/arm.PRIVATE_DIR`): never copied into a view.
PRIVATE_DIR = "private"
#: The private-text check's window: this many consecutive words of an episode's private
#: text found in a portable view fails the build. Sampled every PRIVATE_STRIDE words, so
#: any copied run of PRIVATE_WINDOW + PRIVATE_STRIDE - 1 words or more is always caught.
#: Not shorter: the creator template shares phrases with the MCP docs the agents receive
#: as tool results (the test-case guide resource, DevLoop's tool notes), and those runs
#: reach 22 words in QUA-2861's transcripts. A template echoed into a transcript, or a
#: private file copied, is thousands of words.
PRIVATE_WINDOW = 40
PRIVATE_STRIDE = 5

# A tool result longer than this is folded behind a preview; one longer than the cap is
# cut, with the raw transcript one click away.
FOLD_LINES = 12
FOLD_CHARS = 1500
PREVIEW_LINES = 6
RESULT_CAP_CHARS = 60_000
ARGS_FOLD_CHARS = 1200

_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
              "image/webp": "webp", "image/gif": "gif"}

E = html.escape


class ViewError(Exception):
    """The view cannot be built as asked; the message says why and what to do."""


@dataclass
class ViewResult:
    out_dir: Path
    index: Path
    portable: bool = False
    episodes: int = 0
    images: int = 0
    rescored: int = 0
    not_rescored: dict[str, int] = field(default_factory=dict)
    create_board: Path | None = None     # create.html, when the runs hold CreateBench grades
    report: Path | None = None           # report.html, for an experiment view
    missing: list[str] = field(default_factory=list)   # episode dirs named but not on disk


# ── where it goes ──────────────────────────────────────────────────────────────

def default_out(runs_dir: Path, run_ids: list[str] | None) -> Path:
    """`<runs>/_runs/<run_id>/view/` for one run; `<runs>/_runs/_view/` otherwise."""
    if run_ids and len(run_ids) == 1:
        return run_meta_dir(runs_dir, run_ids[0]) / VIEW_DIRNAME
    return run_meta_dir(runs_dir, MULTI_VIEW_DIRNAME)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def out_problem(runs_dir: Path, out: Path, allow_outside_runs: bool = False) -> str | None:
    """Why `out` may not hold a view, or None. Outside the runs root is refused unless
    allowed; so is the runs root itself and any directory the view did not make."""
    runs_dir, out = Path(runs_dir), Path(out)
    if not _inside(out, runs_dir) and not allow_outside_runs:
        return (f"refusing to write the view to {out}: it is outside the runs root "
                f"{runs_dir}.\n"
                "  The view holds the answer key (matched bug ids) and held-out episodes. "
                "Under the runs root an agent that reads it voids its own episode (a hard "
                "`other_episode` contamination hit); outside it, that read is not caught.\n"
                "  Omit --out for the default under <runs>/_runs/, or pass "
                "--allow-outside-runs to write there anyway.")
    if out.exists() and out.resolve() == runs_dir.resolve():
        return f"refusing to write the view into the runs root itself ({out}); pick a subdirectory"
    if out.exists() and not out.is_dir():
        return f"refusing to write the view to {out}: it exists and is not a directory"
    if out.is_dir() and any(out.iterdir()) and not (out / MARKER).exists():
        return (f"refusing to write the view to {out}: the directory is not empty and was "
                f"not made by `qualgent-bench view` (no {MARKER}); a rebuild clears its "
                "output, so it only writes into a directory it owns")
    return None


def _prepare_out(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("written by `qualgent-bench view`; safe to delete\n")
    # Only the view's own output is cleared (out_problem checked the marker).
    ep = out / "ep"
    if ep.is_dir():
        shutil.rmtree(ep)
    ep.mkdir()


# ── one episode ────────────────────────────────────────────────────────────────

@dataclass
class _Episode:
    eid: str
    result: RunResult
    dir: Path | None
    recorded: dict
    rescored: dict | None             # merged metrics a rescore would write, or None
    rescored_reason: str | None
    rescore_status: str
    rescored_result: RunResult | None  # for the rescored board
    held: bool
    arm: str
    images: int = 0
    calls: int = 0
    exp: dict | None = None           # its experiment cell (`ab.experiment_episodes` entry)


def _app_id(r: RunResult) -> str:
    m = r.metrics or {}
    if m.get("app_id"):
        return str(m["app_id"])
    return ""


def _is_heldout(r: RunResult) -> bool:
    m = r.metrics or {}
    if m.get("heldout"):
        return True
    app = _app_id(r)
    return bool(app and corpus.is_heldout(app))


def _arm(r: RunResult) -> str:
    m = r.metrics or {}
    if m.get("version"):
        return str(m["version"])
    if r.task_type == journey.TASK_TYPE:
        return journey.split_task_id(r.task_id)[1]
    return r.task_type


def _rescore_one(ep_dir: Path | None, r: RunResult, tasks_by_id: dict, enabled: bool,
                 off: str = "not rescored (--no-rescore)"
                 ) -> tuple[str, dict | None, str | None, RunResult | None]:
    """(status, merged metrics, failure reason, rescored result) — the dry-run rescore
    `rescore_journey.py --dry-run` performs. Never raises: a view shows what it can.
    `off` is the status when rescoring is disabled."""
    if not enabled:
        return off, None, None, None
    if r.task_type != journey.TASK_TYPE:
        return f"not rescored ({r.task_type} episode)", None, None, None
    if ep_dir is None or not (ep_dir / "result.json").is_file():
        return "not rescored (episode dir missing)", None, None, None
    from . import rescore as _rescore
    try:
        status, _before, _after, v = _rescore.rescore(ep_dir, tasks_by_id, dry_run=True)
    except Exception as exc:  # noqa: BLE001 - one broken episode must not sink the view
        logger.warning("view: rescore of %s failed: %s", ep_dir, exc)
        return f"rescore failed: {type(exc).__name__}: {exc}", None, None, None
    if v is None:
        why = {"no-case": "its case is not in the current corpus (held-out split not "
                          "configured, or the case was pruned)",
               "no-transcript": "no agent/transcript.txt",
               "skip": "not a journey episode"}.get(status, status)
        return f"not rescored: {why}", None, None, None
    return (status, v.metrics, v.failure_reason,
            r.model_copy(update=_rescore.rescored_fields(v)))


def _read(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _href(target: Path, from_dir: Path) -> str:
    return pathname2url(os.path.relpath(target, from_dir))


# ── formatting ─────────────────────────────────────────────────────────────────

def _yn(v: Any) -> str:
    if v is True:
        return '<span class="y">yes</span>'
    if v is False:
        return '<span class="n">no</span>'
    return '<span class="dim">—</span>'


def _bugs(m: dict | None) -> str:
    if m is None:
        return "—"
    return f"{len(m.get('bugs_found') or [])}/{len(m.get('bugs_present') or [])}"


def _ids(v: Any) -> str:
    if not v:
        return ""
    return ", ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)


def _money(v: Any) -> str:
    return f"${v:.2f}" if isinstance(v, (int, float)) else "—"


def _steps(m: dict) -> str:
    steps = m.get("hook_steps") if m.get("hook_steps") is not None else m.get("steps")
    budget = m.get("step_budget")
    s = f"{steps if steps is not None else '—'}/{budget if budget is not None else '—'}"
    return s


def _fold(text: str, *, lines: int = FOLD_LINES, chars: int = FOLD_CHARS,
          cap: int | None = RESULT_CAP_CHARS, raw_href: str = "") -> str:
    """`<pre>` of `text`, folded behind a preview when long, cut at `cap`."""
    note = ""
    if cap is not None and len(text) > cap:
        more = len(text) - cap
        text = text[:cap]
        note = (f'\n<span class="dim">… {more:,} more characters — see the '
                f'<a href="{raw_href}">raw transcript</a></span>' if raw_href
                else f'\n<span class="dim">… {more:,} more characters</span>')
    split = text.split("\n")
    if len(split) <= lines and len(text) <= chars:
        return f"<pre>{E(text)}{note}</pre>"
    preview = "\n".join(split[:PREVIEW_LINES])
    if len(preview) > chars:
        preview = preview[:chars]
    return (f'<details class="fold"><summary><pre>{E(preview)}\n'
            f'<span class="more">… {len(split):,} lines, {len(text):,} characters — '
            f'click to expand</span></pre></summary><pre>{E(text)}{note}</pre></details>')


def _args(inp: Any) -> str:
    if inp is None or inp == {}:
        return ""
    if isinstance(inp, dict) and set(inp) == {"command"} and isinstance(inp["command"], str):
        return f'<pre class="cmd">$ {E(inp["command"])}</pre>'
    text = inp if isinstance(inp, str) else json.dumps(inp, ensure_ascii=False, indent=1)
    return _fold(text, chars=ARGS_FOLD_CHARS, cap=None)


def _write_images(entry: TimelineEntry, img_dir: Path, start: int) -> tuple[list[str], int]:
    """Extract `entry`'s images into `img_dir`; the `<img>` tags and the next index."""
    tags: list[str] = []
    k = start
    for img in entry.images:
        try:
            data = base64.b64decode(img.data, validate=False)
        except (binascii.Error, ValueError):
            tags.append('<span class="dim">[an image that could not be decoded]</span>')
            continue
        k += 1
        ext = _IMAGE_EXT.get(img.media_type.lower(), "bin")
        name = f"{k:03d}.{ext}"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / name).write_bytes(data)
        src = f"{img_dir.name}/{name}"
        tags.append(f'<a href="{src}" target="_blank" rel="noopener">'
                    f'<img loading="lazy" src="{src}" alt="image {k}"></a>')
    return tags, k


def _timeline_html(entries: list[TimelineEntry], img_dir: Path, raw_href: str
                   ) -> tuple[str, int, int]:
    """(html, images written, tool calls) for one transcript."""
    names = {e.id: e.name for e in entries if e.kind == "call" and e.id}
    parts: list[str] = []
    k = calls = 0
    for e in entries:
        if e.kind == "message":
            parts.append(f'<div class="say"><b>agent</b>{_fold(e.text, lines=60, chars=6000)}</div>')
        elif e.kind == "reasoning":
            parts.append(f'<details class="think"><summary>reasoning '
                         f'({len(e.text):,} characters)</summary><pre>{E(e.text)}</pre></details>')
        elif e.kind == "prompt":
            tags, k = _write_images(e, img_dir, k)
            body = _fold(e.text, lines=4, chars=400) if e.text else ""
            parts.append(f'<div class="prompt"><b>sent to the agent</b>{body}{"".join(tags)}</div>')
        elif e.kind == "call":
            calls += 1
            server = f' <span class="dim">{E(e.server)}</span>' if e.server else ""
            parts.append(f'<div class="call"><b>→ {E(e.name or "?")}</b>{server}'
                         f'{_args(e.input)}</div>')
        elif e.kind == "result":
            tags, k = _write_images(e, img_dir, k)
            label = names.get(e.id, "")
            head = (f'<div class="rhead">← {E(label)}{" · refused / failed" if e.is_error else ""}'
                    f'{f" · {len(tags)} image(s)" if tags else ""}</div>')
            body = _fold(e.text, raw_href=raw_href) if e.text else (
                "" if tags else '<pre class="dim">(empty result)</pre>')
            cls = "result err" if e.is_error else "result"
            parts.append(f'<div class="{cls}">{head}{body}'
                         f'{f"<div class=imgs>{chr(10).join(tags)}</div>" if tags else ""}</div>')
        elif e.kind == "error":
            parts.append(f'<div class="cli-err"><b>CLI error</b><pre>{E(e.text)}</pre></div>')
        elif e.kind == "final":
            parts.append(f'<div class="final"><b>end of run{" (error)" if e.is_error else ""}'
                         f'</b>{_fold(e.text)}</div>')
        else:
            parts.append(f'<details class="other"><summary>{E(e.name or e.kind)}</summary>'
                         f'{_args(e.input)}</details>')
    return "\n".join(parts), k, calls


def _reports_html(recorded: dict, rescored: dict | None) -> str:
    rec = recorded.get("reports") or []
    now = (rescored or {}).get("reports")
    reps = now if now is not None else rec
    if not reps:
        return "<p class=dim>No reports.</p>"
    same_shape = now is not None and len(now) == len(rec)
    head = ("<tr><th>#</th><th>screen</th><th>observed</th><th>expected</th><th>description</th>"
            + ("<th>matched (recorded)</th>" if same_shape else "")
            + f"<th>matched{' (rescored)' if now is not None else ''}</th><th>grounded</th></tr>")
    rows = []
    for i, rep in enumerate(reps):
        rep = rep if isinstance(rep, dict) else {"description": str(rep)}
        cells = [str(rep.get("step") if rep.get("step") is not None else i + 1)]
        cells += [str(rep.get(k) or "") for k in ("screen", "observed", "expected", "description")]
        if same_shape:
            old = rec[i] if isinstance(rec[i], dict) else {}
            cells.append(str(old.get("matched") or "—"))
        cells.append(str(rep.get("matched") or "—"))
        cells.append({True: "yes", False: "no"}.get(rep.get("grounded"), "—"))
        rows.append("<tr>" + "".join(f"<td>{E(c)}</td>" for c in cells) + "</tr>")
    return f'<div class="tablewrap"><table class="reps">{head}{"".join(rows)}</table></div>'


def _verdict_table(ep: _Episode) -> str:
    m0, m1 = ep.recorded, ep.rescored
    r = ep.result
    if m1 is None:
        now = f'<td class="dim" rowspan="5">{E(ep.rescore_status)}</td>'
        rows = [
            ("completed", _yn(m0.get("completed")), now),
            ("bugs found / present", f"{_bugs(m0)} {E(_ids(m0.get('bugs_found')))}", ""),
            ("false reports", E(str(m0.get("false_reports", "—"))), ""),
            ("completion reason", E(str(m0.get("completion_reason") or "")), ""),
            ("verdict reason", E(r.failure_reason or ""), ""),
        ]
    else:
        rows = [
            ("completed", _yn(m0.get("completed")), f"<td>{_yn(m1.get('completed'))}</td>"),
            ("bugs found / present", f"{_bugs(m0)} {E(_ids(m0.get('bugs_found')))}",
             f"<td>{_bugs(m1)} {E(_ids(m1.get('bugs_found')))}</td>"),
            ("false reports", E(str(m0.get("false_reports", "—"))),
             f"<td>{E(str(m1.get('false_reports', '—')))}</td>"),
            ("completion reason", E(str(m0.get("completion_reason") or "")),
             f"<td>{E(str(m1.get('completion_reason') or ''))}</td>"),
            ("verdict reason", E(r.failure_reason or ""), f"<td>{E(ep.rescored_reason or '')}</td>"),
        ]
    body = "".join(f"<tr><th>{E(k)}</th><td>{a}</td>{b}</tr>" for k, a, b in rows)
    kinds = journey.integrity_kinds(m1 if m1 is not None else m0)
    hits = (m1 if m1 is not None else m0).get("contamination_hits") or []
    excluded = exclusion_reason(m1 if m1 is not None else m0)
    oracle = m0.get("oracle") if isinstance(m0.get("oracle"), dict) else {}
    witness = m0.get("witness") if isinstance(m0.get("witness"), dict) else {}
    facts = [
        ("bugs present", E(_ids(m0.get("bugs_present"))) or "none (clean arm)"),
        ("fault fired", E(_ids(m0.get("fault_fired"))) or "—"),
        ("reported status", (f"{E(str(m0.get('reported_status') or '—'))} · verdict "
                             f"{E(str(m0.get('reported_verdict') or '—'))} · expected "
                             f"{E(str(m0.get('expected_verdict') or '—'))} · source "
                             f"{E(str(m0.get('report_source') or '—'))}")),
        ("oracle", E(" · ".join(f"{k} {oracle.get(k)}" for k in ("mode", "ok", "why")
                                 if oracle.get(k) not in (None, ""))) or "—"),
        ("witness", E(f"required {witness.get('required')} · missing {witness.get('missing')}")
         if witness.get("required") else "—"),
        ("steps / budget", _steps(m0) + (' · <b class="n">TRUNCATED</b>' if m0.get("truncated")
                                         else "") + (" · timed out" if m0.get("timed_out") else "")),
        ("cost · wall", (f"{_money(m0.get('cost_usd'))} "
                         f"({E(str(m0.get('cost_source') or '—'))}) · "
                         f"{round(r.wall_time_sec or 0)} s")),
        ("contamination", (E(", ".join(m0.get("contamination_reasons") or [])) or
                           ("yes" if m0.get("contaminated") else "none"))
         + ("".join(f'<br><span class="dim">{E(str(h.get("kind")))}: '
                    f'{E(str(h.get("detail"))[:300])}</span>'
                    for h in hits[:10] if isinstance(h, dict)))),
        ("integrity flags", E(", ".join(kinds)) or "none"),
        ("excluded", E(excluded) if excluded else "no"),
        ("rescore", E(ep.rescore_status)),
        ("corpus version", E(f"recorded {m0.get('heldout_version' if ep.held else 'corpus_version') or 'unstamped'}")),
    ]
    facts_html = "".join(f"<tr><th>{E(k)}</th><td colspan=2>{v}</td></tr>" for k, v in facts)
    return (f'<div class="tablewrap"><table class="kv"><tr><th></th><th>recorded (at run time)</th>'
            f'<th>rescored (current scorer, dry run)</th></tr>{body}{facts_html}</table></div>')


def _copy_for_portable(d: Path, dest: Path) -> dict[str, str]:
    """Copy the episode files a portable page links to into `dest` (the page's own
    image dir); their hrefs from the page, by kind. Missing files are skipped."""
    hrefs: dict[str, str] = {}
    for kind, src, name in (("transcript", d / "agent" / "transcript.txt", "transcript.txt"),
                            ("result", d / "result.json", "result.json")):
        if src.is_file():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest / name)
            hrefs[kind] = f"{dest.name}/{name}"
    if (d / "evidence" / "index.html").is_file():
        shutil.copytree(d / "evidence", dest / "evidence", dirs_exist_ok=True)
        hrefs["evidence"] = f"{dest.name}/evidence/index.html"
    return hrefs


def _episode_page(ep: _Episode, page_dir: Path, raw_href: str, timeline_html: str,
                  shots: int, calls: int, portable: dict[str, str] | None = None) -> str:
    """One episode's page. `portable` holds the hrefs of the copies beside the page
    (`_copy_for_portable`); with it, nothing links into the runs tree."""
    r, d = ep.result, ep.dir
    links = ['<a href="../index.html">← all episodes</a>']
    if portable is not None:
        if "evidence" in portable:
            links.append(f'<a href="{portable["evidence"]}">harness evidence viewer</a>')
        if "transcript" in portable:
            links.append(f'<a href="{portable["transcript"]}">raw transcript</a>')
        if "result" in portable:
            links.append(f'<a href="{portable["result"]}">result.json</a>')
    elif d is not None:
        if (d / "evidence" / "index.html").is_file():
            links.append(f'<a href="{_href(d / "evidence" / "index.html", page_dir)}">'
                         f'harness evidence viewer</a>')
        if (d / "agent" / "transcript.txt").is_file():
            links.append(f'<a href="{raw_href}">raw transcript</a>')
        if (d / "result.json").is_file():
            links.append(f'<a href="{_href(d / "result.json", page_dir)}">result.json</a>')
        links.append(f'<a href="{_href(d, page_dir)}/">episode folder</a>')
    brief = _read(d / "instruction_sent.md" if d else None)
    findings = _read(d / "workspace" / journey.FILENAME if d else None)
    authored = _read(d / AUTHORED_CASE if d else None)
    authored_html = (f'<details open><summary><b>Authored test case</b> ({AUTHORED_CASE})'
                     f'</summary><pre>{E(authored)}</pre></details>'
                     if authored is not None else "")
    cell_html = ""
    if ep.exp:
        x = ep.exp
        what = (f"creation episode (attempt {x['attempt']})" if x["stage"] == "author" else
                f"grade run {x['role']} (attempt {x['attempt']})")
        cell_html = (f'<p class="meta">experiment cell <b>{E(x["cell"])}</b> · {E(what)}'
                     + (f' · excluded: {E(x["excluded"])}' if x.get("excluded") else "")
                     + ' · <a href="../index.html#cells">all cells</a></p>')
    held = ep.held
    banner = f'<div class="banner">{E(HELDOUT_BANNER)}</div>' if held else ""
    badge = f' <span class="ho">{E(HELDOUT_BADGE)}</span>' if held else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(r.task_id)} · {E(r.run_id or "no run id")}</title>
<link rel="stylesheet" href="../style.css"></head>
<body class="ep">
{banner}
<p class="nav">{" · ".join(links)}</p>
<h1>{E(r.task_id)}{badge}</h1>
<p class="meta">{E(r.agent)} · {E(r.model)} · {E(r.condition)} arm · run {E(r.run_id or "—")} ·
trial {r.trial} · started {E(r.started_at)}{" · blinded episode dir" if d and "_ep-" in d.name else ""}</p>
{cell_html}
<h2>Verdict</h2>
{_verdict_table(ep)}
<h2>Reports (scored)</h2>
{_reports_html(ep.recorded, ep.rescored)}
<details><summary><b>Brief sent to the agent</b> (instruction_sent.md)</summary>
<pre>{E(brief) if brief is not None else "(no instruction_sent.md)"}</pre></details>
{authored_html}
<details open><summary><b>{E(journey.FILENAME)}</b></summary>
<pre>{E(findings) if findings is not None else "(no findings file)"}</pre></details>
<h2>Transcript · {calls} tool call(s) · {shots} image(s)</h2>
{timeline_html or '<p class="dim">(no transcript)</p>'}
</body></html>
"""


# ── the index ──────────────────────────────────────────────────────────────────

def _board_cells(rows: list[dict]) -> dict[tuple, dict]:
    return {(r["agent"], r["model"], r["condition"], bool(r.get("heldout"))): r for r in rows}


def _frac(row: dict | None, k: str, n: str) -> str:
    if row is None:
        return "—"
    return f"{row.get(k, 0)}/{row.get(n, 0)}"


def _pct(v: Any) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _summary_html(by_run: dict[str, list[_Episode]]) -> str:
    """Per run: the journey board recorded and rescored — catch per seeded defect,
    false alarms per clean case, completion — the numbers `show --run` prints and
    `rescore_journey.py --dry-run` would publish."""
    blocks = []
    for run_id, eps in by_run.items():
        recorded = [e.result for e in eps if e.result.task_type == journey.TASK_TYPE]
        if not recorded:
            continue
        rescored = [e.rescored_result for e in eps if e.rescored_result is not None]
        rec_rows, now_rows = journey.summary(recorded), journey.summary(rescored)
        rec, now = _board_cells(rec_rows), _board_cells(now_rows)
        lines = []
        for key in sorted(set(rec) | set(now), key=lambda k: (k[3], k[0], k[1], k[2])):
            a, b = rec.get(key), now.get(key)
            split = "held-out" if key[3] else "public"
            badge = f' <span class="ho">{E(HELDOUT_BADGE)}</span>' if key[3] else ""
            eps_cell = str((a or b or {}).get("episodes", 0))
            if (a or b or {}).get("excluded_episodes"):
                eps_cell += f" (+{(a or b)['excluded_episodes']} excluded)"
            lines.append(
                f"<tr><td>{E(key[0])} · {E(key[1])} · {E(key[2])}</td><td>{split}{badge}</td>"
                f"<td>{eps_cell}</td>"
                f"<td>{_frac(a, 'catch_k', 'catch_n')} → <b>{_frac(b, 'catch_k', 'catch_n')}</b></td>"
                f"<td>{_frac(a, 'false_alarm_k', 'false_alarm_n')} → "
                f"<b>{_frac(b, 'false_alarm_k', 'false_alarm_n')}</b></td>"
                f"<td>{(a or {}).get('false_reports', '—')} → <b>{(b or {}).get('false_reports', '—')}</b></td>"
                f"<td>{_pct((a or {}).get('completion'))} → <b>{_pct((b or {}).get('completion'))}</b></td>"
                f"</tr>")
        missing = len(recorded) - len(rescored)
        note = (f'<p class="dim">{missing} journey episode(s) of this run were not rescored '
                f"(see their rows); the rescored columns leave them out, as "
                f"<code>rescore_journey.py --dry-run</code> does.</p>" if missing else "")
        blocks.append(
            f"<h3>Run {E(run_id or '(no run id)')}</h3>"
            "<div class=tablewrap><table class=board><tr><th>agent · model · arm</th><th>split</th><th>episodes</th>"
            "<th>catch / seeded defect<br>recorded → rescored</th>"
            "<th>false alarm / clean case<br>recorded → rescored</th>"
            "<th>false reports<br>recorded → rescored</th>"
            "<th>completion<br>recorded → rescored</th></tr>"
            + "".join(lines) + "</table></div>" + note)
    return "\n".join(blocks)


def _row(ep: _Episode, shots: int) -> dict[str, Any]:
    r, m0, m1 = ep.result, ep.recorded, ep.rescored
    now = m1 if m1 is not None else {}
    moved = m1 is not None and (
        m0.get("completed") != m1.get("completed")
        or sorted(m0.get("bugs_found") or []) != sorted(m1.get("bugs_found") or [])
        or (m0.get("false_reports") or 0) != (m1.get("false_reports") or 0))
    return {
        "id": ep.eid, "run": r.run_id or "", "agent": r.agent, "model": r.model,
        "am": f"{r.agent} · {r.model}", "cond": r.condition, "case": r.task_id,
        "arm": ep.arm, "held": ep.held,
        "c0": m0.get("completed"), "c1": now.get("completed") if m1 is not None else "n/a",
        "b0": _bugs(m0), "b1": _bugs(m1) if m1 is not None else "—",
        "missed1": bool((now or m0).get("bugs_missed")),
        "present": len(m0.get("bugs_present") or []),
        "fr0": m0.get("false_reports") or 0,
        "fr1": (now.get("false_reports") or 0) if m1 is not None else None,
        "status": m0.get("reported_status") or "",
        "steps": _steps(m0), "trunc": bool(m0.get("truncated")),
        "cost": m0.get("cost_usd") if isinstance(m0.get("cost_usd"), (int, float)) else None,
        "shots": shots, "moved": moved, "rescored": m1 is not None,
        "excluded": exclusion_reason(now if m1 is not None else m0) or "",
        **({"cell": ep.exp["cell"],
            "stage": "author" if ep.exp["stage"] == "author" else f"grade · {ep.exp['role']}"}
           if ep.exp else {}),
    }


def _index_html(rows: list[dict], summary: str, title: str, any_held: bool,
                not_rescored: dict[str, int], portable: bool = False,
                links: str = "", rescore_note: bool = True) -> str:
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    # An experiment view's rows carry their cell (`_row`): one more column, and the
    # case filter matches it too.
    cells = any("cell" in r for r in rows)
    held_note = (f'<p class="banner">This view contains held-out episodes, marked '
                 f'<span class="ho">{E(HELDOUT_BADGE)}</span>. Do not share it.</p>'
                 if any_held else "")
    nr = "".join(f"<li>{n} · {E(k)}</li>" for k, n in sorted(not_rescored.items()))
    nr_html = (f"<details><summary>{sum(not_rescored.values())} episode(s) not rescored"
               f"</summary><ul>{nr}</ul></details>" if not_rescored else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(title)}</title><link rel="stylesheet" href="style.css"></head>
<body>
<h1>{E(title)}</h1>
<p class="warn"><b>{E(PORTABLE_NOTE if portable else LOCAL_ONLY_NOTE)}</b></p>
{held_note}
{RESCORE_NOTE if rescore_note else NO_RESCORE_NOTE}
{summary}
{links}
{nr_html}
<h2>Episodes</h2>
<div class="filters">
<label>run <select id="f-run"><option value="">all</option></select></label>
<label>agent · model <select id="f-am"><option value="">all</option></select></label>
<label>arm <select id="f-arm"><option value="">all</option></select></label>
<label>split <select id="f-split"><option value="">all</option><option value="pub">public</option><option value="held">held-out</option></select></label>
<label>completed (rescored) <select id="f-comp"><option value="">any</option><option value="true">yes</option><option value="false">no</option><option value="null">unscored</option></select></label>
<label>bugs <select id="f-bugs"><option value="">any</option><option value="missed">missed some</option><option value="all">found all</option><option value="none">none seeded</option></select></label>
<label>reported <select id="f-status"><option value="">any</option></select></label>
<label><input type="checkbox" id="f-fr"> false reports</label>
<label><input type="checkbox" id="f-trunc"> truncated</label>
<label><input type="checkbox" id="f-moved"> changed on rescore</label>
<label><input type="checkbox" id="f-excl"> excluded</label>
<label><input type="checkbox" id="f-shots"> with images</label>
<label>case <input id="f-q" placeholder="filter by case id{" or cell" if cells else ""}" size="22"></label>
<span id="count" class="dim"></span></div>
<div class="tablewrap"><table class="idx"><thead><tr>
<th>run</th>{"<th>cell · stage</th>" if cells else ""}<th>agent · model</th><th>case</th><th>arm</th><th>split</th>
<th>completed<br>rec → now</th><th>bugs found/present<br>rec → now</th><th>false reports<br>rec → now</th>
<th>reported</th><th>steps/budget</th><th>$</th><th>images</th></tr></thead><tbody id="tb"></tbody></table></div>
<script type="application/json" id="rows">{data}</script>
<script>
const ROWS = JSON.parse(document.getElementById('rows').textContent);
const CELLS = {"true" if cells else "false"};
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
function fill(id, key) {{
  [...new Set(ROWS.map(r => r[key]).filter(v => v !== ''))].sort().forEach(v => {{
    const o = document.createElement('option'); o.textContent = v; o.value = v; $(id).appendChild(o); }});
}}
fill('f-run', 'run'); fill('f-am', 'am'); fill('f-arm', 'arm'); fill('f-status', 'status');
const yn = v => v === true ? '<span class="y">yes</span>' : v === false ? '<span class="n">no</span>'
  : v === 'n/a' ? '<span class="dim">n/a</span>' : '<span class="dim">—</span>';
function keep(r) {{
  const q = $('f-q').value.toLowerCase(), comp = $('f-comp').value, bugs = $('f-bugs').value;
  if ($('f-run').value && r.run !== $('f-run').value) return false;
  if ($('f-am').value && r.am !== $('f-am').value) return false;
  if ($('f-arm').value && r.arm !== $('f-arm').value) return false;
  if ($('f-split').value && ($('f-split').value === 'held') !== r.held) return false;
  if (comp && String(r.c1 === undefined ? null : r.c1) !== comp) return false;
  if (bugs === 'missed' && !(r.present && r.missed1)) return false;
  if (bugs === 'all' && !(r.present && !r.missed1)) return false;
  if (bugs === 'none' && r.present) return false;
  if ($('f-status').value && r.status !== $('f-status').value) return false;
  if ($('f-fr').checked && !((r.fr1 ?? r.fr0) > 0)) return false;
  if ($('f-trunc').checked && !r.trunc) return false;
  if ($('f-moved').checked && !r.moved) return false;
  if ($('f-excl').checked && !r.excluded) return false;
  if ($('f-shots').checked && !r.shots) return false;
  if (q && !r.case.toLowerCase().includes(q) && !(CELLS && r.cell.toLowerCase().includes(q))) return false;
  return true;
}}
function draw() {{
  const rs = ROWS.filter(keep);
  $('count').textContent = rs.length + ' of ' + ROWS.length + ' episodes';
  $('tb').innerHTML = rs.map(r => `<tr class="${{r.moved ? 'mv' : ''}}${{r.excluded ? ' ex' : ''}}">`
    + `<td>${{esc(r.run)}}</td>`
    + (CELLS ? `<td>${{esc(r.cell || '')}}<br><span class="dim">${{esc(r.stage || '')}}</span></td>` : '')
    + `<td>${{esc(r.am)}}<br><span class="dim">${{esc(r.cond)}}</span></td>`
    + `<td><a href="ep/${{r.id}}.html">${{esc(r.case)}}</a>`
    + (r.held ? ' <span class="ho">{E(HELDOUT_BADGE)}</span>' : '')
    + (r.excluded ? `<br><span class="dim">excluded: ${{esc(r.excluded)}}</span>` : '') + '</td>'
    + `<td>${{esc(r.arm)}}</td><td>${{r.held ? 'held-out' : 'public'}}</td>`
    + `<td>${{yn(r.c0)}} → ${{yn(r.c1)}}</td><td>${{esc(r.b0)}} → ${{esc(r.b1)}}</td>`
    + `<td>${{r.fr0}} → ${{r.fr1 === null ? '—' : r.fr1}}</td><td>${{esc(r.status)}}</td>`
    + `<td>${{esc(r.steps)}}${{r.trunc ? ' <b class="n" title="truncated">✂</b>' : ''}}</td>`
    + `<td>${{r.cost === null ? '—' : r.cost.toFixed(2)}}</td><td>${{r.shots}}</td></tr>`).join('');
}}
document.querySelectorAll('.filters select, .filters input').forEach(e => e.addEventListener('input', draw));
draw();
</script>
</body></html>
"""


RESCORE_NOTE = """<p class="dim"><b>recorded</b> = the verdict written at run time; <b>rescored</b> = the current
scorer on the same transcript and findings file (<code>scripts/rescore_journey.py --dry-run</code>,
nothing written). Highlighted rows changed on rescore.</p>"""
NO_RESCORE_NOTE = ("""<p class="dim">Episodes show the verdict recorded at run time; a grade run """
                   """is judged by its cell's grade manifest (the cells table and the A/B """
                   """report), not rescored here.</p>""")


CSS = """
:root{--fg:#1d1d1f;--bg:#fff;--dim:#6b6b70;--line:#8884;--th:#f3f3f5;--say:#eef7ee;
 --call:#eef2fb;--res:#f7f7f8;--mv:#fff3c4;--ho:#b35c00;--err:#c62828;--ok:#1a7f37;--link:#0b57d0}
@media (prefers-color-scheme:dark){:root{--fg:#e8e8ea;--bg:#161618;--dim:#9a9aa0;--th:#222226;
 --say:#1f2a20;--call:#1e2533;--res:#1c1c1f;--mv:#3d3410;--link:#8ab4ff;--ok:#5cc27a;--err:#ff6b6b}}
body{font:14px/1.45 -apple-system,system-ui,sans-serif;margin:16px;color:var(--fg);background:var(--bg)}
a{color:var(--link)}code{font-size:12px}
table{border-collapse:collapse;font-size:13px}
th,td{border:1px solid var(--line);padding:3px 6px;vertical-align:top;text-align:left}
th{background:var(--th)}table.idx th{position:sticky;top:0}
.tablewrap{overflow-x:auto;max-width:100%}
@media (max-width:600px){body{margin:16px}.reps td{min-width:120px}}
pre{white-space:pre-wrap;word-break:break-word;margin:4px 0;font-size:12px}
.dim{color:var(--dim)}.y{color:var(--ok);font-weight:600}.n{color:var(--err);font-weight:600}
.say{background:var(--say);padding:6px 8px;margin:6px 0;border-radius:6px}
.prompt{border:1px dashed var(--line);padding:6px 8px;margin:6px 0;border-radius:6px}
.call{background:var(--call);padding:4px 8px;margin:8px 0 0;border-radius:6px 6px 0 0}
.result{background:var(--res);padding:4px 8px;margin:0 0 6px;border-radius:0 0 6px 6px;border-left:3px solid var(--line)}
.result.err{border-left-color:var(--err)}.rhead{font-size:11px;color:var(--dim)}
.cli-err{border-left:3px solid var(--err);padding:4px 8px;margin:6px 0}
.final{border:1px solid var(--line);padding:6px 8px;margin:10px 0;border-radius:6px}
.think summary,.other summary{cursor:pointer;color:var(--dim)}
.fold>summary{list-style:none;cursor:pointer}.fold>summary::-webkit-details-marker{display:none}
.fold[open]>summary{display:none}.more{color:var(--link)}
.imgs img{max-height:420px;max-width:100%;margin:4px 6px 4px 0;border:1px solid var(--line);border-radius:4px}
.prompt img{max-height:420px;max-width:100%}
.ho{background:var(--ho);color:#fff;font-size:12px;padding:1px 6px;border-radius:4px;white-space:nowrap}
.banner{background:var(--ho);color:#fff;padding:8px 12px;border-radius:6px;font-weight:600}
.banner .ho{background:#fff;color:var(--ho)}
.warn{color:var(--ho)}.kv th:first-child{white-space:nowrap}.reps td{max-width:360px}
.filters{display:flex;flex-wrap:wrap;gap:8px 14px;margin:8px 0 12px}.filters label{display:inline-flex;flex-wrap:wrap;gap:4px;align-items:center;max-width:100%}
.filters select{max-width:100%}
tr.mv td{background:var(--mv)}tr.ex td{opacity:.65}
"""


# ── build ──────────────────────────────────────────────────────────────────────

def _manifest(by_run: dict[str, list[_Episode]], title: str, portable: bool) -> dict:
    """`manifest.json`: what the view holds, one entry per run. `completed` counts the
    rescored verdict where there is one, else the recorded one, like the index's
    "now" column."""
    runs = []
    for run_id, eps in sorted(by_run.items()):
        verdicts = [(e.rescored if e.rescored is not None else e.recorded).get("completed")
                    for e in eps]
        runs.append({
            "run_id": run_id,
            "started_at": min((e.result.started_at for e in eps if e.result.started_at),
                              default=None),
            "agents": sorted({f"{e.result.agent} · {e.result.model}" for e in eps}),
            "conditions": sorted({e.result.condition for e in eps if e.result.condition}),
            "arms": sorted({e.arm for e in eps if e.arm}),
            "episodes": len(eps),
            "held_out": sum(e.held for e in eps),
            "completed": sum(v is True for v in verdicts),
            "scored": sum(isinstance(v, bool) for v in verdicts),
        })
    return {"format": MANIFEST_FORMAT, "title": title, "portable": portable,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "qualgentbench_version": __version__,
            "episodes": sum(r["episodes"] for r in runs),
            "held_out": sum(r["held_out"] for r in runs), "runs": runs}


def _load(runs_dir: Path, run_ids: list[str] | None) -> list[RunResult]:
    if not run_ids:
        return load_results(runs_dir)
    out: list[RunResult] = []
    for rid in run_ids:
        found = load_results(runs_dir, run_id=rid)
        if not found:
            raise ViewError(f"no saved episodes for run {rid} under {runs_dir} "
                            f"(check the id against {runs_dir / '_runs'} and --runs-dir)")
        out += found
    return out


def _write_create_board(runs_dir: Path, run_ids: list[str], out_dir: Path,
                        title: str, experiment: str | None = None) -> Path | None:
    """`create.html`: the CreateBench board (`create/board.py`) over these runs' grade
    manifests — or, with `experiment`, that experiment's cells plus the reference
    baseline of its runner, as `show --mode create --experiment` selects them —
    standalone (no link into the runs tree, so `--portable` carries it to the hosted
    viewer). The readiness gate is shown as a banner, not enforced: a view is a reading
    aid; `show --mode create` is where the gate refuses. None when no grades."""
    from .create import board as _cboard
    if not _cboard.load_grades(runs_dir, run_ids or None):
        return None
    b = _cboard.board_for(runs_dir, run_ids=run_ids or None, experiment=experiment,
                          include_smoke=experiment is None,
                          title=f"{title} — CreateBench board")
    path = out_dir / "create.html"
    path.write_text(_cboard.render_html(b))
    return path


def _write_episodes(runs_dir: Path, results: list[RunResult], out_dir: Path, *,
                    portable: bool, rescore: bool, tasks_by_id: dict,
                    rescore_off: str = "not rescored (--no-rescore)",
                    exps: list[dict | None] | None = None,
                    progress: Callable[[str], None] | None = None
                    ) -> tuple[ViewResult, list[dict], dict[str, list[_Episode]], list[_Episode]]:
    """One page per result, in the order given, under `<out_dir>/ep/`; the result
    counts, the index rows, the episodes per run and the episodes in order. `exps[i]`
    is result i's experiment cell, if any."""
    _prepare_out(out_dir)
    ep_root = out_dir / "ep"
    res = ViewResult(out_dir=out_dir, index=out_dir / "index.html", portable=portable)
    rows: list[dict] = []
    by_run: dict[str, list[_Episode]] = {}
    eps: list[_Episode] = []
    for n, r in enumerate(results, 1):
        eid = f"{n:04d}"
        d = resolve_artifact_dir(runs_dir, r)
        if d is not None and not d.is_dir():
            d = None
        status, m1, reason1, rescored_result = _rescore_one(d, r, tasks_by_id, rescore,
                                                            rescore_off)
        if m1 is None:
            res.not_rescored[status] = res.not_rescored.get(status, 0) + 1
        else:
            res.rescored += 1
        ep = _Episode(eid=eid, result=r, dir=d, recorded=dict(r.metrics or {}), rescored=m1,
                      rescored_reason=reason1, rescore_status=status,
                      rescored_result=rescored_result, held=_is_heldout(r), arm=_arm(r),
                      exp=exps[n - 1] if exps else None)
        tr_path = d / "agent" / "transcript.txt" if d else None
        copies: dict[str, str] | None = None
        if portable:
            copies = _copy_for_portable(d, ep_root / eid) if d else {}
            raw_href = copies.get("transcript", "")
        else:
            raw_href = _href(tr_path, ep_root) if tr_path and tr_path.is_file() else ""
        text = _read(tr_path) if raw_href else None
        entries = timeline(text) if text else []
        tl_html, shots, calls = _timeline_html(entries, ep_root / eid, raw_href)
        (ep_root / f"{eid}.html").write_text(
            _episode_page(ep, ep_root, raw_href, tl_html, shots, calls, copies))
        res.images += shots
        rows.append(_row(ep, shots))
        by_run.setdefault(r.run_id or "", []).append(ep)
        eps.append(ep)
        if progress:
            progress(f"{n}/{len(results)} {r.task_id} · {shots} image(s)")
    res.episodes = len(results)
    (out_dir / "style.css").write_text(CSS)
    return res, rows, by_run, eps


def credential_hits(out_dir: Path | str) -> list[dict]:
    """Files under `out_dir` that match a credential marker (`checkpoint.scan_for_secrets`,
    the scanner of the checkpoint export and of QUA-2841's view gate): `{"file",
    "marker"}`, never the matched text. Real images are not read."""
    from .checkpoint import scan_for_secrets
    out_dir = Path(out_dir)
    hits = []
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file() or p.name == MARKER or _is_image(p):
            continue
        found = scan_for_secrets(p.read_bytes())
        if found is not None:
            hits.append({"file": p.relative_to(out_dir).as_posix(), "marker": found[0]})
    return hits


def _finish(out_dir: Path, manifest: dict, eps: list[_Episode], portable: bool,
            credentials: bool = False) -> None:
    """Write `manifest.json` last — after the private-text check of a portable view
    (and, with `credentials`, the credential-marker scan), which raises and leaves no
    manifest on a hit."""
    if portable and credentials:
        found = credential_hits(out_dir)
        if found:
            listed = "\n".join(f"  {h['file']}: credential marker {h['marker']}"
                                for h in found[:20])
            raise ViewError(
                f"refusing to finish the portable view at {out_dir}: {len(found)} file(s) "
                f"match a credential marker:\n{listed}\n  No manifest.json was written, so "
                f"the folder cannot be published. Remove the credential from the episode "
                f"and build the view again.")
    if portable:
        hits = private_text_hits(out_dir, [e.dir for e in eps if e.dir is not None])
        if hits:
            listed = "\n".join(f"  {h['file']}: {h['words']} (from {h['source']})"
                                for h in hits[:20])
            raise ViewError(
                f"refusing to finish the portable view at {out_dir}: {len(hits)} file(s) "
                f"carry text from an episode's private/ folder (the creation arm's private "
                f"developer instructions), which must never leave the runs tree:\n{listed}\n"
                f"  No manifest.json was written, so the folder cannot be published. Delete "
                f"it, and find how that text reached a transcript or a page.")
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=2))


def build_view(runs_dir: Path | str, run_ids: list[str] | None = None,
               out: Path | str | None = None, *, rescore: bool = True,
               allow_outside_runs: bool = False, tasks_by_id: dict | None = None,
               portable: bool = False,
               progress: Callable[[str], None] | None = None) -> ViewResult:
    """Write the view of `run_ids` (every run under `runs_dir` when empty) and return
    where it went. Reads the runs tree only; writes only under `out`. `portable`
    copies what the pages link to beside them (see the module docstring).

    `tasks_by_id` is the corpus the rescore scores against (default: the current one,
    `rescore.journey_tasks_by_id`, held-out included when the split is configured)."""
    runs_dir = Path(runs_dir).expanduser()
    if not runs_dir.is_dir():
        raise ViewError(f"runs dir {runs_dir} does not exist")
    run_ids = [r for r in (run_ids or []) if r]
    out_dir = Path(out).expanduser() if out else default_out(runs_dir, run_ids)
    if problem := out_problem(runs_dir, out_dir, allow_outside_runs):
        raise ViewError(problem)
    results = _load(runs_dir, run_ids)
    if not results:
        raise ViewError(f"no saved episodes under {runs_dir}")
    results.sort(key=lambda r: (r.run_id or "", r.task_id, r.trial, r.started_at))

    if rescore and tasks_by_id is None and any(r.task_type == journey.TASK_TYPE for r in results):
        from .rescore import journey_tasks_by_id
        tasks_by_id = journey_tasks_by_id()
    res, rows, by_run, eps = _write_episodes(runs_dir, results, out_dir, portable=portable,
                                             rescore=rescore, tasks_by_id=tasks_by_id or {},
                                             progress=progress)
    title = ("Run " + run_ids[0] if len(run_ids) == 1
             else "Runs " + ", ".join(run_ids) if run_ids else f"All runs under {runs_dir}")
    res.create_board = _write_create_board(runs_dir, run_ids, out_dir, title)
    links = ('<p><a href="create.html">CreateBench board</a> — the authored-case grades of '
             'these runs (QUA-2858).</p>' if res.create_board else "")
    (out_dir / "index.html").write_text(_index_html(
        rows, _summary_html(by_run), f"{title} — episode view",
        any(e["held"] for e in rows), res.not_rescored, portable, links))
    _finish(out_dir, _manifest(by_run, title, portable), eps, portable)
    return res


# ── an experiment (QUA-2869) ───────────────────────────────────────────────────

def experiment_out(runs_dir: Path, name: str) -> Path:
    """`<runs>/_runs/_create/ab/<name>/view/`: beside the experiment's own state."""
    from .create import ab
    return ab.state_path(runs_dir, name).with_suffix("") / VIEW_DIRNAME


#: Grade runs are not rescored in an experiment view: each is a run of an AUTHORED case
#: (not a corpus case), and the cell's grade manifest is its verdict.
EXPERIMENT_RESCORE_OFF = "not rescored (CreateBench experiment: the grade manifest is the verdict)"


def _axis(v: Any) -> str:
    return {True: '<span class="y">yes</span>', False: '<span class="n">no</span>'}.get(
        v, '<span class="dim">—</span>')


def _cells_html(cells: list[dict], eps: list[_Episode]) -> str:
    """The per-cell table: status, creation outcome, the grade's axes, and a link to
    every episode of the cell (its creation episode(s), then each grade run)."""
    links: dict[str, list[str]] = {}
    for e in eps:
        if not e.exp:
            continue
        x = e.exp
        label = "author" if x["stage"] == "author" else x["role"]
        if x["attempt"] > 1:
            label += f" (attempt {x['attempt']})"
        cls = ' class="dim"' if x.get("excluded") else ""
        tip = f' title="excluded: {E(x["excluded"])}"' if x.get("excluded") else ""
        links.setdefault(x["cell"], []).append(
            f'<a href="ep/{e.eid}.html"{cls}{tip}>{E(label)}</a>')
    axes = ("power", "repeatability", "specificity", "lint", "strong")
    rows = []
    for c in cells:
        status = c["status"] + (f" · {c['grade_status']}" if c["grade_status"]
                                and c["grade_status"] != c["status"] else "")
        made = c["outcome"] + (f" · excluded: {c['excluded']}" if c["excluded"] else "") + (
            f" · flags: {', '.join(c['validity_flags'])}" if c["validity_flags"] else "")
        fault = f'<br><span class="dim">{E(c["fault"])}</span>' if c["fault"] else ""
        rows.append(
            f'<tr><td>{E(c["cell"])}</td><td>{E(c["arm"])}</td><td>{E(c["case_id"])}</td>'
            f'<td>{c["trial"]}</td><td>{E(status)}{fault}</td>'
            f'<td>{E(made) or "—"}</td>'
            + "".join(f"<td>{_axis(c['axes'].get(a))}</td>" for a in axes)
            + f"<td>{_axis(c['uptake'])}</td><td>{_money(c['cost_usd'])}</td>"
            f"<td>{' · '.join(links.get(c['cell'], [])) or '—'}</td></tr>")
    head = "".join(f"<th>{E(a)}</th>" for a in axes)
    return (f'<h2 id="cells">Cells</h2><div class="tablewrap"><table class="idx"><thead><tr>'
            f'<th>cell</th><th>arm</th><th>brief</th><th>trial</th><th>status</th>'
            f'<th>creation</th>{head}<th>uptake</th><th>cost</th><th>episodes</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _report_html(name: str, lines: list[str], board: bool) -> str:
    nav = ['<a href="index.html">← experiment index</a>', '<a href="report.json">report.json</a>']
    if board:
        nav.append('<a href="create.html">create board</a>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(name)} — A/B report</title><link rel="stylesheet" href="style.css"></head>
<body>
<p class="nav">{" · ".join(nav)}</p>
<h1>{E(name)} — A/B report</h1>
<p class="dim">What <code>scripts/run_create_ab.py --report</code> prints for this experiment,
read from its state file and the grade manifests it names.</p>
<pre>{E(chr(10).join(lines))}</pre>
</body></html>
"""


def _experiment_manifest(by_run: dict[str, list[_Episode]], title: str, portable: bool,
                         rep: dict, missing: int) -> dict:
    """`manifest.json` for an experiment: format-1 keys, `runs` = ONE entry for the
    whole experiment (its `run_id` the experiment name), plus `kind` and `experiment`
    (the per-run breakdown, the verdict, the cells, the cost and the linked pages).
    In that entry `completed` / `scored` count CELLS (graded / planned): an episode-level
    `completed` would mix creation and grade runs into a number that means nothing."""
    m = _manifest(by_run, title, portable)
    per_run = m["runs"]
    v = rep["verdict"]
    m["kind"] = "experiment"
    m["experiment"] = {
        "name": rep["experiment"], "run_id": rep["run_id"],
        "registered_at": rep["registered_at"],
        "prediction": v.get("prediction"), "prediction_sha": v.get("prediction_sha"),
        "verdict": v.get("verdict"), "why": v.get("why"), "cells": rep["cells"],
        "spent_usd": rep["spent"]["total"], "missing_episodes": missing,
        "pages": {"index": "index.html", "report": "report.html",
                  "report_json": "report.json", "board": "create.html"},
        "runs": per_run}
    m["runs"] = [{
        "run_id": rep["experiment"],
        "started_at": min((r["started_at"] for r in per_run if r["started_at"]), default=None),
        "agents": sorted({a for r in per_run for a in r["agents"]}),
        "conditions": sorted({c for r in per_run for c in r["conditions"]}),
        "arms": sorted(rep["arms"]),
        "episodes": sum(r["episodes"] for r in per_run),
        "held_out": sum(r["held_out"] for r in per_run),
        "completed": int(rep["cells"].get("graded", 0)),
        "scored": sum(rep["cells"].values())}]
    return m


def build_experiment_view(runs_dir: Path | str, experiment: str,
                          out: Path | str | None = None, *, allow_outside_runs: bool = False,
                          portable: bool = False,
                          progress: Callable[[str], None] | None = None) -> ViewResult:
    """Write the view of one CreateBench A/B experiment (module docstring) and return
    where it went. Reads the runs tree only; writes only under `out` (default
    `experiment_out`)."""
    from .create import ab
    runs_dir = Path(runs_dir).expanduser()
    if not runs_dir.is_dir():
        raise ViewError(f"runs dir {runs_dir} does not exist")
    if ab.load_state(ab.state_path(runs_dir, experiment)) is None:
        known = sorted(p.stem for p in ab.state_path(runs_dir, "x").parent.glob("*.json"))
        raise ViewError(f"no experiment {experiment!r} under {runs_dir} (no state file "
                        f"{ab.state_path(runs_dir, experiment)}); experiments here: "
                        f"{', '.join(known) or 'none'}")
    out_dir = Path(out).expanduser() if out else experiment_out(runs_dir, experiment)
    if problem := out_problem(runs_dir, out_dir, allow_outside_runs):
        raise ViewError(problem)
    results: list[RunResult] = []
    exps: list[dict | None] = []
    missing: list[str] = []
    for x in ab.experiment_episodes(runs_dir, experiment):
        path = resolve_artifact_dir(runs_dir, x["episode_dir"]) / "result.json"
        try:
            results.append(RunResult.model_validate_json(path.read_text()))
        except (OSError, ValueError) as exc:
            logger.warning("view: %s: episode %s unreadable: %s", experiment,
                           x["episode_dir"], exc)
            missing.append(x["episode_dir"])
            continue
        exps.append(x)
    if not results:
        raise ViewError(f"experiment {experiment!r} names no readable episode under {runs_dir}")
    res, rows, by_run, eps = _write_episodes(runs_dir, results, out_dir, portable=portable,
                                             rescore=False, tasks_by_id={},
                                             rescore_off=EXPERIMENT_RESCORE_OFF, exps=exps,
                                             progress=progress)
    res.missing = missing
    title = f"Experiment {experiment}"
    rep = ab.report(runs_dir, experiment)
    res.report = out_dir / "report.html"
    (out_dir / "report.json").write_text(json.dumps(rep, indent=2, default=str))
    run_ids = sorted(by_run)
    res.create_board = _write_create_board(runs_dir, [], out_dir, title, experiment=experiment)
    res.report.write_text(_report_html(experiment, ab.render_report(rep),
                                       res.create_board is not None))
    v = rep["verdict"]
    miss = (f'<p class="warn">{len(missing)} episode(s) the state names are not on disk '
            f'(or have no readable result.json); they are not in this view.</p>'
            if missing else "")
    links = (f'<h2>Experiment</h2><p><b>VERDICT: {E(str(v.get("verdict")))}</b> — '
             f'{E(str(v.get("why") or ""))}</p>'
             f'<p class="dim">prediction {E(str(v.get("prediction")))} · '
             f'{len(rep["briefs"])} brief(s) · arms {E(", ".join(rep["arms"]))} · driver run '
             f'{E(str(rep["run_id"]))} · {len(run_ids)} run(s) · ${rep["spent"]["total"]:.2f}</p>'
             f'<p><a href="report.html">A/B report</a> · <a href="report.json">report.json</a>'
             + (' · <a href="create.html">CreateBench board</a>' if res.create_board else "")
             + f' · <a href="#cells">cells</a></p>{miss}'
             + _cells_html(ab.cell_summaries(runs_dir, experiment), eps))
    (out_dir / "index.html").write_text(_index_html(
        rows, "", f"{title} — episode view", any(e["held"] for e in rows), {}, portable,
        links, rescore_note=False))
    # TODO(QUA-2869 follow-up): `bench_viewer.py publish` uploads only view format 2
    # (QUA-2840/2841, oss epic/qua-2839, unmerged) and only runs with a board.json, then
    # rebuilds the index from ep/*.json — which would drop report.html, create.html and
    # the cells table. Once format 2 lands here: give the experiment view stable episode
    # keys, ep/<key>.json summaries and run.json, swap the refusing credential scan for
    # the per-episode gate, and add `publish --experiment NAME` to the publisher (upload
    # the folder whole, no --index-from). Until then it is published by hand (QUA-2869).
    _finish(out_dir, _experiment_manifest(by_run, title, portable, rep, len(missing)), eps,
            portable, credentials=True)
    return res


# ── the private-text check ─────────────────────────────────────────────────────

_ESCAPE = re.compile(r"\\(?:u[0-9a-fA-F]{4}|.)")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _words(text: str) -> list[str]:
    """Normalised words: HTML entities and JSON/string escapes undone (a transcript is
    JSON, a page is escaped HTML), lower case, every run of non-alphanumerics a break."""
    return _NON_WORD.sub(" ", _ESCAPE.sub(" ", html.unescape(text)).lower()).split()


def _is_image(path: Path) -> bool:
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return False
    with path.open("rb") as fh:
        head = fh.read(12)
    return head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or (
        head[:4] == b"RIFF" and head[8:12] == b"WEBP")


def private_text_hits(out_dir: Path | str, episode_dirs: list[Path]) -> list[dict]:
    """Files under `out_dir` that carry PRIVATE_WINDOW consecutive words of any
    `<episode>/private/` file of `episode_dirs` (`{"file", "source", "words"}`; the
    matched words are cut to a short prefix). Real images are not read. [] when no
    episode has a private folder."""
    windows: dict[str, str] = {}
    seen: set[str] = set()
    for d in episode_dirs:
        priv = Path(d) / PRIVATE_DIR
        if not priv.is_dir():
            continue
        for f in sorted(priv.rglob("*")):
            if not f.is_file():
                continue
            text = f.read_text(errors="replace")
            if text in seen:
                continue
            seen.add(text)
            ws = _words(text)
            for i in range(len(ws) - PRIVATE_WINDOW + 1):
                windows.setdefault(" ".join(ws[i:i + PRIVATE_WINDOW]), f"{PRIVATE_DIR}/{f.name}")
    if not windows:
        return []
    out_dir = Path(out_dir)
    hits = []
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file() or p.name == MARKER or _is_image(p):
            continue
        ws = _words(p.read_bytes().decode("utf-8", errors="replace"))
        for i in range(0, len(ws) - PRIVATE_WINDOW + 1, PRIVATE_STRIDE):
            w = " ".join(ws[i:i + PRIVATE_WINDOW])
            if w in windows:
                hits.append({"file": p.relative_to(out_dir).as_posix(), "source": windows[w],
                             "words": " ".join(w.split()[:4]) + " …"})
                break
    return hits
