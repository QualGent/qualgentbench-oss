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

Composable across machines and segments (QUA-2840). Every episode page is keyed by a
STABLE id (`episode_key`): the episode's own `episode_id` when its marker or result
carries one, else a short hash of its folder name relative to the runs root — the same
on every machine that holds the episode and in every build, whatever else is beside it.
Beside each page, `ep/<key>.json` is the episode's summary (its index row plus the
recorded and rescored results the run board is built from), and `run.json` is the run
state (`plan.json` / `stop.json` distilled at build time). `build_index` rebuilds
`index.html` and `manifest.json` from those files alone, with no runs tree: several
machines' `ep/` merged into one folder index as one run (`view --index-from`).

The credential gate (QUA-2841). A portable view leaves the machine, so every text file
it writes or copies — episode pages (they embed transcript text), the transcript copy,
`result.json`, the evidence html/json/jsonl, the episode summaries, `run.json`, the
index and the manifest — goes through `checkpoint.scan_for_secrets`, the scanner in
front of every checkpoint export, before it is written. A file that matches is not
written (a page is replaced by a stub; a summary by a stub summary); the episode page
says "withheld: credential marker <marker> in <file>" — the marker and the file, never
the matched text; `manifest.json` lists every hit under `withheld`; and the CLI exits
`EXIT_WITHHELD`, so a publisher can refuse. Withheld entries live in the episode's
summary (or stub summary), so they survive a merge and an `--index-from` rebuild, which
also re-scans every file the folder holds. Images are not scanned. Whatever the gate
itself writes names a marker without spelling it (HTML character references, JSON
`\\u` escapes), so a gated folder re-scans clean.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import logging
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.request import pathname2url

from . import __version__, corpus, journey
from .checkpoint import read_episode_marker, run_meta_dir, scan_for_secrets
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
#:
#: Format 2 (QUA-2840) keeps every format-1 key and adds a `state` block to each entry of
#: `runs` — the run's progress, not just what this view holds — and (QUA-2841) the
#: credential gate's `withheld` list::
#:
#:     {"format": 2, "title": str, "portable": bool, "generated_at": ISO 8601 UTC,
#:      "qualgentbench_version": str, "episodes": int, "held_out": int,
#:      "withheld": [{"episode": str|null,   # episode key; null for a run-level file
#:                    "file": str,           # path relative to the view folder
#:                    "marker": str}]        # the credential marker's name, never the text
#:                  | null,                  # null: not gated (a local, non-portable view)
#:      "runs": [{"run_id", "started_at", "agents", "conditions", "arms", "episodes",
#:                "held_out", "completed", "scored",
#:                "state": {"segment": int|null,        # latest sitting (plan.json)
#:                          "units_planned": int|null,  # plan.json's unit list
#:                          "units_done": int|null,     # planned minus owed
#:                          "units_owed": int|null,     # what `run --resume` would run
#:                          "stopped": str|null,        # stop.json's reason, while owed
#:                          "complete": bool|null}}]}   # null: no run state known
MANIFEST = "manifest.json"
MANIFEST_FORMAT = 2
#: `<out>/ep/<key>.json` — one episode's summary: its index row plus the recorded and
#: rescored results the run board needs. Written once per episode, never rewritten by a
#: later segment's build, so it can be merged from anywhere (`build_index`).
SUMMARY_FORMAT = 1
#: `<out>/<RUN_STATE>` — the run state `build_index` reads beside the summaries:
#: `{"format", "title", "portable", "runs": {run_id: <state block, as in the manifest>}}`.
RUN_STATE = "run.json"
RUN_STATE_FORMAT = 1
#: The index's badge on a run that still owes units.
PARTIAL_BADGE = "in progress"
#: `view` / `view --index-from` exit code when the credential gate withheld anything
#: (EX_DATAERR). A contract with the publisher: this code, or a non-empty
#: `manifest.json` `withheld`, means "do not upload this folder".
EXIT_WITHHELD = 65
#: The index's badge on an episode with a withheld file.
WITHHELD_BADGE = "withheld"
WITHHELD_BANNER = ("The credential gate withheld {n} file(s) from this view: each matched a "
                   "credential marker and was not written. Do not publish this view; remove "
                   "the credential from the run and build it again.")
#: Not scanned by the gate: images are out of scope (docs/checkpointing.md).
UNSCANNED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})

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
    #: The credential gate's hits, `{"episode", "file", "marker"}` as in the manifest;
    #: None when the view was not gated (not portable).
    withheld: list[dict] | None = None


# ── the credential gate (QUA-2841) ─────────────────────────────────────────────

def _html_marker(marker: str) -> str:
    """`marker` as HTML character references: it renders as the marker, and the page's
    bytes do not match it, so the notice never trips the scan it reports."""
    return "".join(f"&#{ord(c)};" for c in marker)


def _dumps(doc: Any, markers: Any = (), **kw: Any) -> str:
    """`json.dumps`, with every string value equal to one of `markers` written as
    `\\u` escapes — the same string once parsed, and no match for the scanner."""
    text = json.dumps(doc, **kw)
    for m in sorted(set(markers), key=len, reverse=True):
        esc = '"' + "".join(f"\\u{ord(c):04x}" for c in m) + '"'
        text = text.replace(json.dumps(m, ensure_ascii=kw.get("ensure_ascii", True)), esc)
    return text


def _markers(hits: Any) -> set[str]:
    return {h["marker"] for h in hits or ()}


@dataclass
class _Gate:
    """`scan_for_secrets` in front of every text file a portable view writes. Disabled
    (a plain writer) for a local view. `hits` collects `{"episode", "file", "marker"}`."""
    out_dir: Path
    enabled: bool
    hits: list[dict] = field(default_factory=list)

    def rel(self, path: Path) -> str:
        return path.relative_to(self.out_dir).as_posix()

    def check(self, data: bytes, path: Path, episode: str | None) -> dict | None:
        """Record and return the hit for `data` bound for `path`, or None if clean."""
        if not self.enabled:
            return None
        found = scan_for_secrets(data)
        if found is None:
            return None
        hit = {"episode": episode, "file": self.rel(path), "marker": found[0]}
        self.hits.append(hit)
        logger.warning("view: withheld %s (credential marker %r)", hit["file"], hit["marker"])
        return hit

    def write(self, path: Path, data: str | bytes, episode: str | None) -> dict | None:
        """Write `data` to `path` unless it carries a credential marker; the hit or None.
        A withheld file is not written, and an older copy at `path` is removed."""
        b = data.encode("utf-8") if isinstance(data, str) else data
        hit = self.check(b, path, episode)
        if hit is not None:
            path.unlink(missing_ok=True)
            return hit
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b)
        return None

    def copy(self, src: Path, dst: Path, episode: str | None) -> dict | None:
        """Copy `src` to `dst` through the gate; images are copied unscanned."""
        if dst.suffix.lower() in UNSCANNED_SUFFIXES:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            return None
        return self.write(dst, src.read_bytes(), episode)

    def replace(self, path: Path, text: str) -> None:
        """Write a replacement (a stub) the gate built itself. Scanned all the same; one
        that would not pass (a marker in a case id?) is left unwritten."""
        b = text.encode("utf-8")
        if self.enabled and scan_for_secrets(b) is not None:
            logger.warning("view: stub for %s not written: it matches a marker", path)
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b)


def scan_view(view_dir: Path | str) -> list[dict]:
    """Every file under `view_dir` that matches a credential marker, as withheld entries
    (`--index-from` on a merged folder: whatever it holds is re-checked). Images are not
    scanned; neither are the three files an index rebuild rewrites."""
    view_dir = Path(view_dir)
    regenerated = {"index.html", MANIFEST, "style.css", MARKER}
    hits = []
    for p in sorted(view_dir.rglob("*")):
        rel = p.relative_to(view_dir)
        if (not p.is_file() or p.suffix.lower() in UNSCANNED_SUFFIXES
                or (len(rel.parts) == 1 and rel.name in regenerated)):
            continue
        found = scan_for_secrets(p.read_bytes())
        if found is not None:
            hits.append({"episode": _episode_of(rel), "file": rel.as_posix(),
                         "marker": found[0]})
    return hits


def _episode_of(rel: PurePosixPath | Path) -> str | None:
    """The episode key a view-relative path belongs to (`ep/<key>.html`, `ep/<key>/…`)."""
    parts = PurePosixPath(Path(rel).as_posix()).parts
    if len(parts) < 2 or parts[0] != "ep":
        return None
    if len(parts) > 2:
        return parts[1]
    name = parts[1]
    for suffix in (".html", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _notice_html(hits: list[dict], nl: bool = False) -> str:
    """The episode page's notice: one line per withheld file ("" when there is none,
    so a clean page is byte for byte what it was before the gate)."""
    if not hits:
        return ""
    lines = "".join(f"<li>withheld: credential marker <code>{_html_marker(h['marker'])}</code> "
                    f"in <code>{E(h['file'])}</code></li>" for h in hits)
    return (f'<div class="banner wh-notice"><p>The credential gate withheld '
            f'{len(hits)} file(s) of this episode (not written; never the matched text):</p>'
            f"<ul>{lines}</ul></div>" + ("\n" if nl else ""))


def _stub_page(key: str, case: str, hits: list[dict]) -> str:
    """What stands in for a withheld episode page."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(case)} · withheld</title>
<link rel="stylesheet" href="../style.css"></head>
<body class="ep">
<p class="nav"><a href="../index.html">← all episodes</a></p>
<h1>{E(case)} <span class="ho">{E(WITHHELD_BADGE)}</span></h1>
<p class="meta">episode {E(key)}</p>
{_notice_html(hits)}
<p>This page matched a credential marker, so the view did not write it. Remove the
credential from the run and build the view again.</p>
</body></html>
"""


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


def _rescore_one(ep_dir: Path | None, r: RunResult, tasks_by_id: dict, enabled: bool
                 ) -> tuple[str, dict | None, str | None, RunResult | None]:
    """(status, merged metrics, failure reason, rescored result) — the dry-run rescore
    `rescore_journey.py --dry-run` performs. Never raises: a view shows what it can."""
    if not enabled:
        return "not rescored (--no-rescore)", None, None, None
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


# A key names a file and a folder under `ep/`, so only a plain, short token is taken
# verbatim; anything else is hashed.
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def episode_key(r: RunResult, ep_dir: Path | None) -> str:
    """The episode's stable page key: `ep/<key>.html`, assets in `ep/<key>/`.

    The same on every machine that holds the episode and in every build, whatever else
    the build holds — so views built on several machines, or before and after a later
    segment, merge by file name. The episode's `episode_id` (QUA-2806: the marker in
    its dir, else the result's provenance) when it has one; otherwise `h-` + 12 hex of
    sha256 over the folder name relative to the runs root (`<task>/<episode>`, the last
    two parts of `artifact_dir`, relative or legacy absolute alike); with no artifact
    dir at all, over the result's identity (run, task, agent, model, arm, trial, start)."""
    marker = read_episode_marker(ep_dir) if ep_dir is not None else None
    for cand in ((marker or {}).get("episode_id"), (r.provenance or {}).get("episode_id")):
        if isinstance(cand, str) and _SAFE_KEY.match(cand):
            return cand
    if r.artifact_dir:
        basis = "/".join(PurePosixPath(str(r.artifact_dir).replace("\\", "/")).parts[-2:])
    else:
        basis = "\x1f".join(str(x) for x in (r.run_id, r.task_id, r.agent, r.model,
                                             r.condition, r.trial, r.started_at))
    return "h-" + hashlib.sha256(basis.encode()).hexdigest()[:12]


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


def _copy_for_portable(d: Path, dest: Path, gate: _Gate, key: str) -> dict[str, str]:
    """Copy the episode files a portable page links to into `dest` (the page's own
    image dir), each through the credential gate; their hrefs from the page, by kind.
    Missing files are skipped, and so is a link to a copy the gate withheld."""
    hrefs: dict[str, str] = {}
    for kind, src, name in (("transcript", d / "agent" / "transcript.txt", "transcript.txt"),
                            ("result", d / "result.json", "result.json")):
        if src.is_file() and gate.copy(src, dest / name, key) is None:
            hrefs[kind] = f"{dest.name}/{name}"
    ev = d / "evidence"
    if (ev / "index.html").is_file():
        for src in sorted(ev.rglob("*")):
            if src.is_file():
                gate.copy(src, dest / "evidence" / src.relative_to(ev), key)
        if (dest / "evidence" / "index.html").is_file():
            hrefs["evidence"] = f"{dest.name}/evidence/index.html"
    return hrefs


def _episode_page(ep: _Episode, page_dir: Path, raw_href: str, timeline_html: str,
                  shots: int, calls: int, portable: dict[str, str] | None = None,
                  withheld: list[dict] | None = None) -> str:
    """One episode's page. `portable` holds the hrefs of the copies beside the page
    (`_copy_for_portable`); with it, nothing links into the runs tree. `withheld` is
    the gate's hits for this episode, shown as a notice under the title."""
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
{_notice_html(withheld or [], nl=True)}<p class="meta">{E(r.agent)} · {E(r.model)} · {E(r.condition)} arm · run {E(r.run_id or "—")} ·
trial {r.trial} · started {E(r.started_at)}{" · blinded episode dir" if d and "_ep-" in d.name else ""}</p>
<h2>Verdict</h2>
{_verdict_table(ep)}
<h2>Reports (scored)</h2>
{_reports_html(ep.recorded, ep.rescored)}
<details><summary><b>Brief sent to the agent</b> (instruction_sent.md)</summary>
<pre>{E(brief) if brief is not None else "(no instruction_sent.md)"}</pre></details>
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


def _summary_html(by_run: dict[str, list[_Summary]]) -> str:
    """Per run: the journey board recorded and rescored — catch per seeded defect,
    false alarms per clean case, completion — the numbers `show --run` prints and
    `rescore_journey.py --dry-run` would publish."""
    blocks = []
    for run_id, eps in by_run.items():
        recorded = [e.result for e in eps if e.result.task_type == journey.TASK_TYPE]
        if not recorded:
            continue
        rescored = [e.rescored_result for e in eps if e.rescored_result is not None
                    and e.result.task_type == journey.TASK_TYPE]
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
    }


def _units(st: dict) -> str:
    if st.get("units_planned") is None:
        return ""
    out = f" · {st.get('units_done')}/{st['units_planned']} units done"
    if st.get("units_owed"):
        out += f", {st['units_owed']} owed"
    if st.get("segment") is not None:
        out += f" · segment {st['segment']}"
    return out


def _state_html(states: dict[str, dict]) -> str:
    """One line per run whose state is known: a partial run gets the badge
    ("in progress · stopped: <reason>") and its counts; a complete one a dim line."""
    lines = []
    for run_id, st in sorted(states.items()):
        if not st or st.get("complete") is None:
            continue
        name = f"Run {E(run_id or '(no run id)')}"
        if st["complete"]:
            lines.append(f'<p class="dim">{name}: complete{E(_units(st))}</p>')
            continue
        badge = PARTIAL_BADGE + (f" · stopped: {st['stopped']}" if st.get("stopped") else "")
        lines.append(f'<p class="partial"><span class="ip">{E(badge)}</span> '
                     f'{name}{E(_units(st))}</p>')
    return "\n".join(lines)


def _index_html(rows: list[dict], summary: str, title: str, any_held: bool,
                not_rescored: dict[str, int], portable: bool = False,
                state_html: str = "", withheld_html: str = "") -> str:
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
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
{state_html}
{held_note}{withheld_html}
<p class="dim"><b>recorded</b> = the verdict written at run time; <b>rescored</b> = the current
scorer on the same transcript and findings file (<code>scripts/rescore_journey.py --dry-run</code>,
nothing written). Highlighted rows changed on rescore.</p>
{summary}
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
<label>case <input id="f-q" placeholder="filter by case id" size="22"></label>
<span id="count" class="dim"></span></div>
<div class="tablewrap"><table class="idx"><thead><tr>
<th>run</th><th>agent · model</th><th>case</th><th>arm</th><th>split</th>
<th>completed<br>rec → now</th><th>bugs found/present<br>rec → now</th><th>false reports<br>rec → now</th>
<th>reported</th><th>steps/budget</th><th>$</th><th>images</th></tr></thead><tbody id="tb"></tbody></table></div>
<script type="application/json" id="rows">{data}</script>
<script>
const ROWS = JSON.parse(document.getElementById('rows').textContent);
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
  if (q && !r.case.toLowerCase().includes(q)) return false;
  return true;
}}
function draw() {{
  const rs = ROWS.filter(keep);
  $('count').textContent = rs.length + ' of ' + ROWS.length + ' episodes';
  $('tb').innerHTML = rs.map(r => `<tr class="${{r.moved ? 'mv' : ''}}${{r.excluded ? ' ex' : ''}}">`
    + `<td>${{esc(r.run)}}</td><td>${{esc(r.am)}}<br><span class="dim">${{esc(r.cond)}}</span></td>`
    + `<td><a href="ep/${{r.id}}.html">${{esc(r.case)}}</a>`
    + (r.held ? ' <span class="ho">{E(HELDOUT_BADGE)}</span>' : '')
    + (r.wh ? ' <span class="ho">{E(WITHHELD_BADGE)}</span>' : '')
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
.ip{background:var(--link);color:var(--bg);font-size:12px;padding:1px 6px;border-radius:4px;white-space:nowrap}
"""


# ── summaries, run state, the index ────────────────────────────────────────────

@dataclass
class _Summary:
    """One episode as the index needs it — what `ep/<key>.json` holds, read back."""
    key: str
    run_id: str
    row: dict
    held: bool
    arm: str
    rescore_status: str
    result: RunResult
    rescored_result: RunResult | None
    #: The gate's hits for this episode's other files, `{"file", "marker"}`.
    withheld: list[dict] = field(default_factory=list)

    @property
    def verdict(self) -> Any:
        """The "now" completion: rescored where there is one, else recorded."""
        return ((self.rescored_result or self.result).metrics or {}).get("completed")

    def as_json(self) -> dict:
        doc = {"format": SUMMARY_FORMAT, "key": self.key, "run_id": self.run_id,
               "held": self.held, "arm": self.arm, "rescore_status": self.rescore_status,
               "row": self.row, "result": self.result.model_dump(mode="json"),
               "rescored_result": (self.rescored_result.model_dump(mode="json")
                                   if self.rescored_result is not None else None)}
        if self.withheld:
            doc["withheld"] = self.withheld
        return doc

    def dumps(self) -> str:
        return _dumps(self.as_json(), _markers(self.withheld), indent=1, ensure_ascii=False)

    @classmethod
    def from_json(cls, doc: Any, where: Path) -> _Summary:
        try:
            if not isinstance(doc, dict):
                raise TypeError("not a JSON object")
            if int(doc.get("format") or 0) > SUMMARY_FORMAT:
                raise ValueError(f"summary format {doc.get('format')} is newer than this "
                                 f"harness reads ({SUMMARY_FORMAT}); upgrade qualgentbench")
            rescored = doc.get("rescored_result")
            return cls(key=str(doc["key"]), run_id=str(doc.get("run_id") or ""),
                       row=dict(doc["row"]), held=bool(doc.get("held")),
                       arm=str(doc.get("arm") or ""),
                       rescore_status=str(doc.get("rescore_status") or ""),
                       result=RunResult.model_validate(doc["result"]),
                       rescored_result=(RunResult.model_validate(rescored)
                                        if rescored is not None else None),
                       withheld=_hit_list(doc.get("withheld")))
        except (KeyError, TypeError, ValueError) as exc:
            raise ViewError(f"unreadable episode summary {where}: {exc}") from exc


def _hit_list(v: Any) -> list[dict]:
    """A summary's `withheld` list, read back: `[{"file", "marker"}]`."""
    if v is None:
        return []
    if not isinstance(v, list):
        raise TypeError("withheld is not a list")
    return [{"file": str(h["file"]), "marker": str(h["marker"])} for h in v]


@dataclass
class _Stub:
    """An episode whose summary the gate withheld: `ep/<key>.json` is this stub
    (`"stub": true`), so the withheld entries survive a merge and `--index-from`. It is
    listed as withheld and left out of the rows, the board and the manifest counts."""
    key: str
    run_id: str
    case: str
    withheld: list[dict]

    def dumps(self) -> str:
        return _dumps({"format": SUMMARY_FORMAT, "key": self.key, "run_id": self.run_id,
                       "case": self.case, "stub": True, "withheld": self.withheld},
                      _markers(self.withheld), indent=1, ensure_ascii=False)


def _read_summary(doc: Any, where: Path) -> _Summary | _Stub:
    if isinstance(doc, dict) and doc.get("stub"):
        try:
            return _Stub(key=str(doc["key"]), run_id=str(doc.get("run_id") or ""),
                         case=str(doc.get("case") or ""), withheld=_hit_list(doc["withheld"]))
        except (KeyError, TypeError) as exc:
            raise ViewError(f"unreadable episode summary {where}: {exc}") from exc
    return _Summary.from_json(doc, where)


def _order(s: _Summary) -> tuple:
    r = s.result
    return (r.run_id or "", r.task_id, r.trial, r.started_at, s.key)


_UNKNOWN_STATE = {"segment": None, "units_planned": None, "units_done": None,
                  "units_owed": None, "stopped": None, "complete": None}


def run_state(runs_dir: Path | str, run_id: str) -> dict:
    """The run's progress, off disk: `plan.json` (segment, units planned) against the
    finished episodes (`checkpoint.run_summary`, the predicate `run --resume` subtracts
    with), plus `stop.json`'s reason. `stopped` is reported only while units are owed:
    stop.json outlives the resume that finished the run. With no plan.json (a run from
    before plans, or none at all) the counts are null and the run is complete unless a
    stop.json says otherwise."""
    if not run_id:
        return dict(_UNKNOWN_STATE)
    from . import checkpoint, credit
    stop = credit.read_stop(runs_dir, run_id)
    reason = str(stop.get("reason") or "unknown") if stop is not None else None
    try:
        s = checkpoint.run_summary(runs_dir, run_id)
    except checkpoint.CheckpointError:
        s = None
    if s is None:
        return {**_UNKNOWN_STATE, "stopped": reason, "complete": reason is None}
    planned = int(s["counts"]["planned"])
    owed = int(s["counts"]["remaining"])
    return {"segment": s.get("segment"), "units_planned": planned,
            "units_done": planned - owed, "units_owed": owed,
            "stopped": reason if owed else None, "complete": owed == 0}


def _manifest(by_run: dict[str, list[_Summary]], title: str, portable: bool,
              states: dict[str, dict], withheld: list[dict] | None = None) -> dict:
    """`manifest.json`: what the view holds, one entry per run, each with its run
    state (format 2). `completed` counts the rescored verdict where there is one, else
    the recorded one, like the index's "now" column."""
    runs = []
    for run_id, eps in sorted(by_run.items()):
        verdicts = [e.verdict for e in eps]
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
            "state": {**_UNKNOWN_STATE, **(states.get(run_id) or {})},
        })
    return {"format": MANIFEST_FORMAT, "title": title, "portable": portable,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "qualgentbench_version": __version__,
            "episodes": sum(r["episodes"] for r in runs),
            "held_out": sum(r["held_out"] for r in runs), "withheld": withheld, "runs": runs}


def _dedupe(hits: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out = []
    for h in hits:
        k = (h["episode"], h["file"], h["marker"])
        if k not in seen:
            seen.add(k)
            out.append(h)
    return out


def _withheld_html(hits: list[dict], cases: dict[str, str]) -> str:
    if not hits:
        return ""
    items = []
    for h in hits:
        ep = h["episode"]
        where = (f'<a href="ep/{E(ep)}.html">{E(cases.get(ep) or ep)}</a> · ' if ep else "")
        items.append(f"<li>{where}<code>{E(h['file'])}</code> · credential marker "
                     f"<code>{_html_marker(h['marker'])}</code></li>")
    return (f'<p class="banner">{E(WITHHELD_BANNER.format(n=len(hits)))}</p>'
            f"<details open><summary>{len(hits)} withheld file(s)</summary>"
            f"<ul>{''.join(items)}</ul></details>")


def _write_index(out_dir: Path, summaries: list[_Summary], title: str, portable: bool,
                 states: dict[str, dict], *, gate: _Gate, stubs: list[_Stub] = (),
                 extra: list[dict] = ()) -> tuple[dict[str, int], list[dict] | None]:
    """`index.html`, `manifest.json` and `style.css` from the summaries alone — the one
    renderer behind `build_view` and `build_index`, so the two cannot differ. Returns
    the not-rescored counts by reason and the withheld list (None: not gated and
    nothing recorded): every summary's and stub's entries plus `extra` (run-level
    files), then the index's and the manifest's own if either matched."""
    summaries = sorted(summaries, key=_order)
    by_run: dict[str, list[_Summary]] = {}
    not_rescored: dict[str, int] = {}
    for s in summaries:
        by_run.setdefault(s.run_id, []).append(s)
        if s.rescored_result is None:
            not_rescored[s.rescore_status] = not_rescored.get(s.rescore_status, 0) + 1
    hits = [{"episode": s.key, **h} for s in summaries for h in s.withheld]
    hits += [{"episode": s.key, **h} for s in sorted(stubs, key=lambda s: s.key)
             for h in s.withheld]
    hits = _dedupe(hits + list(extra))
    cases = {s.key: s.row.get("case") or "" for s in summaries}
    cases.update({s.key: s.case for s in stubs})
    rows = [{**s.row, "wh": len(s.withheld)} if s.withheld else s.row for s in summaries]
    (out_dir / "style.css").write_text(CSS)
    index = out_dir / "index.html"
    if gate.write(index, _index_html(
            rows, _summary_html(by_run), f"{title} — episode view",
            any(s.held for s in summaries), not_rescored, portable,
            _state_html({r: states.get(r) or {} for r in by_run}),
            _withheld_html(hits, cases)), None) is not None:
        hits = _dedupe(hits + gate.hits[-1:])
        gate.replace(index, _stub_index(hits))
    withheld = hits if (gate.enabled or hits) else None
    manifest = out_dir / MANIFEST
    doc = _manifest(by_run, title, portable, states, withheld)
    if gate.write(manifest, _dumps(doc, _markers(withheld), indent=2), None) is not None:
        # Only the view's own fields are left: the run entries and title matched.
        withheld = _dedupe((withheld or []) + gate.hits[-1:])
        gate.replace(manifest, _dumps(
            {**_manifest({}, "", portable, {}, withheld), "episodes": None, "held_out": None},
            _markers(withheld), indent=2))
    return not_rescored, withheld


def _stub_index(hits: list[dict]) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Episode view · withheld</title><link rel="stylesheet" href="style.css"></head>
<body>
<h1>Episode view · withheld</h1>
<p>The index matched a credential marker, so the view did not write it.</p>
{_withheld_html(hits, {})}
</body></html>
"""


def _title(run_ids: list[str]) -> str:
    return "Run " + run_ids[0] if len(run_ids) == 1 else "Runs " + ", ".join(run_ids)


# ── build ──────────────────────────────────────────────────────────────────────

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


def build_view(runs_dir: Path | str, run_ids: list[str] | None = None,
               out: Path | str | None = None, *, rescore: bool = True,
               allow_outside_runs: bool = False, tasks_by_id: dict | None = None,
               portable: bool = False,
               progress: Callable[[str], None] | None = None) -> ViewResult:
    """Write the view of `run_ids` (every run under `runs_dir` when empty) and return
    where it went. Reads the runs tree only; writes only under `out`. `portable`
    copies what the pages link to beside them (see the module docstring).

    Each episode's page, assets and summary are keyed by `episode_key`, so a later
    build — after another segment, or on another machine — writes the same files for
    the same episode. `run.json` records the run state the index shows.

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
    tasks_by_id = tasks_by_id or {}

    _prepare_out(out_dir)
    ep_root = out_dir / "ep"
    res = ViewResult(out_dir=out_dir, index=out_dir / "index.html", portable=portable)
    gate = _Gate(out_dir, enabled=portable)
    summaries: list[_Summary] = []
    stubs: list[_Stub] = []
    used: set[str] = set()
    for n, r in enumerate(results, 1):
        d = resolve_artifact_dir(runs_dir, r)
        if d is not None and not d.is_dir():
            d = None
        key = episode_key(r, d)
        if key in used:  # two results naming one episode dir: keep both pages
            logger.warning("view: episode key %s is shared by two results (%s)", key, r.task_id)
            key = next(f"{key}-{i}" for i in range(2, len(results) + 2)
                       if f"{key}-{i}" not in used)
        used.add(key)
        status, m1, reason1, rescored_result = _rescore_one(d, r, tasks_by_id, rescore)
        if m1 is not None:
            res.rescored += 1
        ep = _Episode(eid=key, result=r, dir=d, recorded=dict(r.metrics or {}), rescored=m1,
                      rescored_reason=reason1, rescore_status=status,
                      rescored_result=rescored_result, held=_is_heldout(r), arm=_arm(r))
        tr_path = d / "agent" / "transcript.txt" if d else None
        has_transcript = bool(tr_path and tr_path.is_file())
        first_hit = len(gate.hits)
        copies: dict[str, str] | None = None
        if portable:
            copies = _copy_for_portable(d, ep_root / key, gate, key) if d else {}
            raw_href = copies.get("transcript", "")
        else:
            raw_href = _href(tr_path, ep_root) if has_transcript else ""
        # Rendered even when the gate withheld the transcript copy: the page is scanned
        # in its own right, and a page that shows no marker is still worth having.
        text = _read(tr_path) if has_transcript else None
        entries = timeline(text) if text else []
        tl_html, shots, calls = _timeline_html(entries, ep_root / key, raw_href)
        page_path, summary_path = ep_root / f"{key}.html", ep_root / f"{key}.json"
        page = _episode_page(ep, ep_root, raw_href, tl_html, shots, calls, copies)
        page_hit = gate.check(page.encode("utf-8"), page_path, key)
        summary = _Summary(key=key, run_id=r.run_id or "", row=_row(ep, shots), held=ep.held,
                           arm=ep.arm, rescore_status=status, result=r,
                           rescored_result=rescored_result,
                           withheld=[{"file": h["file"], "marker": h["marker"]}
                                     for h in gate.hits[first_hit:]])
        if gate.write(summary_path, summary.dumps(), key) is None:
            summaries.append(summary)
        else:
            stub = _Stub(key=key, run_id=r.run_id or "", case=r.task_id,
                         withheld=[{"file": h["file"], "marker": h["marker"]}
                                   for h in gate.hits[first_hit:]])
            gate.replace(summary_path, stub.dumps())
            stubs.append(stub)
        ep_hits = gate.hits[first_hit:]
        if page_hit is not None:
            gate.replace(page_path, _stub_page(key, r.task_id, ep_hits))
        else:
            page_path.write_text(_episode_page(ep, ep_root, raw_href, tl_html, shots, calls,
                                               copies, ep_hits) if ep_hits else page)
        res.images += shots
        if progress:
            progress(f"{n}/{len(results)} {r.task_id} · {shots} image(s)")
    res.episodes = len(results)

    title = (_title(run_ids) if run_ids else f"All runs under {runs_dir}")
    states = {rid: run_state(runs_dir, rid)
              for rid in sorted({s.run_id for s in [*summaries, *stubs]})}
    run_hit = gate.write(out_dir / RUN_STATE, json.dumps(
        {"format": RUN_STATE_FORMAT, "title": title, "portable": portable, "runs": states},
        indent=2), None)
    res.not_rescored, res.withheld = _write_index(
        out_dir, summaries, title, portable, states, gate=gate, stubs=stubs,
        extra=[run_hit] if run_hit else [])
    return res


def build_index(view_dir: Path | str) -> ViewResult:
    """Rebuild `index.html`, `manifest.json` and `style.css` in `view_dir` from its
    `ep/*.json` summaries and `run.json` alone — no runs tree (`view --index-from`).

    For a folder merged from several views of one run (machines, segments): pages and
    summaries are keyed by `episode_key`, so a merge is a plain copy of every `ep/`, and
    the index lists every episode once. The run state is `run.json`'s as it stands
    (the publisher keeps the latest segment's); without one, the title is derived from
    the run ids and the state is unknown. Writes only those three files and the view
    marker; nothing under `ep/` is touched.

    Withheld entries come back from the summaries and stub summaries, and a portable
    folder is re-scanned (`scan_view`): a merged-in file that matches a marker is
    reported as withheld (its summary, if it is one, is not read), so the rebuilt
    `manifest.json` and the CLI's `EXIT_WITHHELD` cover everything the folder holds."""
    view_dir = Path(view_dir).expanduser()
    files = sorted((view_dir / "ep").glob("*.json")) if (view_dir / "ep").is_dir() else []
    if not files:
        raise ViewError(f"no episode summaries under {view_dir / 'ep'} (expected ep/<key>.json "
                        "files, written by `qualgent-bench view`)")
    state_doc = _read_run_state(view_dir)
    portable = bool(state_doc.get("portable"))
    # A portable folder may have been merged from anywhere (and built before the gate):
    # everything it holds is re-scanned, and a summary that matches is not read.
    found = scan_view(view_dir) if portable else []
    matched = {h["file"] for h in found}
    summaries: list[_Summary] = []
    stubs: list[_Stub] = []
    for f in files:
        rel = f.relative_to(view_dir).as_posix()
        if rel in matched:
            stubs.append(_Stub(key=f.stem, run_id="", case="", withheld=[]))
            continue
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError) as exc:
            raise ViewError(f"unreadable episode summary {f}: {exc}") from exc
        item = _read_summary(doc, f)
        (stubs if isinstance(item, _Stub) else summaries).append(item)
    run_ids = sorted({s.run_id for s in summaries})
    title = str(state_doc.get("title") or _title(run_ids))
    states = {k: v for k, v in (state_doc.get("runs") or {}).items() if isinstance(v, dict)}
    (view_dir / MARKER).write_text("written by `qualgent-bench view`; safe to delete\n")
    res = ViewResult(out_dir=view_dir, index=view_dir / "index.html", portable=portable,
                     episodes=len(summaries),
                     images=sum(int(s.row.get("shots") or 0) for s in summaries),
                     rescored=sum(s.rescored_result is not None for s in summaries))
    res.not_rescored, res.withheld = _write_index(
        view_dir, summaries, title, portable, states,
        gate=_Gate(view_dir, enabled=portable), stubs=stubs, extra=found)
    return res


def _read_run_state(view_dir: Path) -> dict:
    state_doc: dict = {}
    if (view_dir / RUN_STATE).is_file():
        try:
            state_doc = json.loads((view_dir / RUN_STATE).read_text())
        except (OSError, ValueError) as exc:
            raise ViewError(f"unreadable run state {view_dir / RUN_STATE}: {exc}") from exc
        if not isinstance(state_doc, dict):
            raise ViewError(f"unreadable run state {view_dir / RUN_STATE}: not a JSON object")
    return state_doc
