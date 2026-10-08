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
also re-scans every file the folder holds. Images are not scanned — but only a file
that has an image suffix AND starts with a PNG, JPEG, GIF or WEBP signature counts as
one (QUA-2847): a transcript image of any other media type (`NNN.bin`) and a text file
named `*.png` are scanned like any other file. A portable build ends by re-scanning its
own folder (`scan_view`) as a backstop. Whatever the gate itself writes names a marker
without spelling it (HTML character references, JSON `\\u` escapes), so a gated folder
re-scans clean.

Copies keep their source's mtime (QUA-2847): a transcript image gets its transcript's,
a copied file its original's. A publisher that syncs by size and mtime (`aws s3 sync`)
then skips an unchanged episode's images and copies on a rebuild; the pages, summaries
and index are regenerated and carry the build's time.

The same gate keeps an episode's PRIVATE text in (QUA-2869). A view never copies an
episode's `private/` folder (the creation arm's developer instructions, private text from
QualGent-MCP / DevLoop-MCP; `create/arm.py`), and in a portable view the gate also checks
every text file for a run of PRIVATE_WINDOW consecutive words of any episode's private
text. Such a file is withheld like a credential hit (`marker` names the private file,
never the text), and the build then FAILS: `ViewError`, no `manifest.json`, so the
folder cannot be published by mistake — a private-text hit means private text reached a
transcript or a page, which is a harness bug to find, not a file to leave out.

`--experiment <name>` (QUA-2869) views one CreateBench A/B experiment instead of runs:
every episode its state file names — each cell's creation episode(s) and the five grade
runs of its authored case, which live in many runs (one per creation episode, plus the
driver's) — with the A/B report (`report.html`, `report.json`, from `create/ab.py`), the
experiment's create board (`create.html`, and `create.json`, the same board as data) and a
per-cell table, all linked from one index. Default output:
`<runs>/_runs/_create/ab/<name>/view/`. It is a format-2 view like any other (stable keys,
`ep/<key>.json`, `run.json`, the same gate): its `run.json` also carries the experiment
(`experiment`: the verdict, the cells, the report's numbers), so `--index-from` rebuilds
its index and manifest from the folder alone. The index charts the report (QUA-2922): X1
per-brief power, arm A against arm B, by detection group with the pooled group row and its
registered tests; X2 the uptake check; X3 the pre-registered preconditions and
expectations as a checklist. Their inputs (`brief_power`, `by_group`, `uptake`,
`expectations`, `preconditions`, `arm_order`) are in run.json's `experiment` block.

A run's index (QUA-2918) states what the run measured and how its rescore relates to it
(`_versions_html`: mode, corpus, held-out split, brief, arm, DevLoop server, set key, a
MIXED badge, the rescore sentence and its counts), prints the journey board `show --run`
prints (`_summary_html`: ranking order, public then held-out, `journey.rates_cells` /
`cost_cells`, the run-time numbers where they differ; no blocker recall, which reads the
building checkout's corpus) and draws charts R2-R4 with `viz` (`_charts_html`: rates by
row and app, one strip cell per episode, rescore drift only when something moved), each
with a table twin. All of it reads the manifest's own blocks (`_measurement`), so it is
as pure a function of the summaries as the manifest is.
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

from . import corpus, journey, viz
from .checkpoint import read_episode_marker, run_meta_dir, scan_for_secrets
from .failures import exclusion_reason, is_excluded
from .leaderboard import clean_model_name, load_results
from .rates import fmt_pct_ci
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
#:                    "marker": str}]        # the credential marker's name, or
#:                                           # "private text (private/<file>)": never the text
#:                  | null,                  # null: not gated (a local, non-portable view)
#:      "runs": [{"run_id", "started_at", "agents", "conditions", "arms", "episodes",
#:                "held_out", "completed", "scored",
#:                "state": {"segment": int|null,        # latest sitting (plan.json)
#:                          "units_planned": int|null,  # plan.json's unit list
#:                          "units_done": int|null,     # planned minus owed
#:                          "units_owed": int|null,     # what `run --resume` would run
#:                          "stopped": str|null,        # stop.json's reason, while owed
#:                          "complete": bool|null}}]}   # null: no run state known
#:
#: An experiment view (QUA-2869) writes the same format-2 document with ONE `runs` entry
#: for the whole experiment — its `run_id` the experiment name, `completed` / `scored`
#: counting CELLS (graded / planned), its `state` the cells done / owed — plus
#: `"kind": "experiment"` and an `experiment` block (`_experiment_manifest`).
#:
#: Still format 2, QUA-2917 adds keys (never renames or removes one): what each run
#: MEASURED, so pages that compare runs compose from manifests alone. `qualgentbench_version`
#: is now `checkpoint.package_version()`, and the top level gains::
#:
#:     "notes": {"rates_legend", "blocker_off_note", "ranking_note", "mixed_corpus_note",
#:               "mixed_brief_note"}
#:              # BOARD_RATES_LEGEND (journey.RATES_LEGEND minus blocker recall, which
#:              # `board` drops) / BLOCKER_OFF_NOTE / journey.RANKING_NOTE /
#:              # MIXED_CORPUS_NOTE / MIXED_BRIEF_NOTE
#:
#: and every `runs[]` entry gains::
#:
#:     "versions": {"mode": "journey"|"create"|<task type>|"mixed"|null,
#:                  "modes": [task_type, ...],
#:                  "corpus": str|null, "corpus_versions": [str], "corpus_unstamped": int,
#:                  "heldout": str|null, "heldout_versions": [str], "heldout_unstamped": int,
#:                  "brief": str|null, "brief_versions": [str], "brief_unstamped": int,
#:                  "condition": str|null, "conditions": [str],
#:                  "devloop": str|null, "devloops": [str],  # "<tools8>/<instr8>"|"bare"|"unstamped"
#:                  "mixed": bool, "set_key": str|null},
#:     "set_key": "j-<corpus>-<heldout|none>-b<brief|none>" | "c-<corpus>-g<grader>-cb<brief>"
#:                | null,                    # null: mixed, unstamped corpus, or not journey
#:     "rescored_with": {"corpus", "heldout", "scorer": str|null,
#:                       "corpus_versions", "heldout_versions", "scorer_versions": [str],
#:                       "stamped", "unstamped", "not_rescored": int},
#:     "moved": int,            # episodes whose completion / bugs found / false reports moved
#:     "present_changed": int,  # episodes whose seeded defects (bugs_present) moved
#:     "public" | "heldout": {"episodes", "excluded", "cases", "apps", "completed",
#:                            "scored": int},
#:     "models": [{"agent", "model", "model_raw", "provider": str|null, "condition"}],
#:     "board": {"now": [row], "recorded": [row],            # journey.summary rows, ranked,
#:               "by_app_now": [row], "by_app_recorded": [row]},  # minus _BOARD_DROP,
#:                                                       # plus "not_rescored"; by_app rows
#:                                                       # keep _BY_APP_KEYS only
#:     "cases": [{"key", "case_id", "app_id", "arm", "held", "agent", "model", "condition",
#:                "trial", "started_at", "completed_rec", "completed_now", "present",
#:                "found_rec", "found_now", "fired", "reports_now",
#:                "unmatched_grounded_now", "fr_rec", "fr_now", "truncated", "steps",
#:                "step_budget", "excluded", "cost_usd", "cost_source", "moved",
#:                "rescored"}]               # one per journey episode (`_cases`)
#:
#: Every one of them is a pure function of the summaries and `run.json` (no clock, host,
#: package version or current-corpus fact beyond the top-level `generated_at` and
#: `qualgentbench_version`), so `build_index` reproduces them byte for byte. Blocker recall
#: is absent from `board` for that reason (`_BOARD_DROP`). An experiment's entry has an
#: empty `board` and `cases`; its `experiment` block also gains `environment` and
#: `arm_pins`. The gate-stub manifest (a withheld manifest) has no run entries.
MANIFEST = "manifest.json"
MANIFEST_FORMAT = 2
#: `<out>/ep/<key>.json` — one episode's summary: its index row plus the recorded and
#: rescored results the run board needs. Written once per episode, never rewritten by a
#: later segment's build, so it can be merged from anywhere (`build_index`). One optional
#: key (QUA-2917): `rescored_with` = `{"corpus_version", "heldout_version"}`, present only
#: when `rescored_result` is and the build rescored against the default corpus.
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
#: Not scanned by the gate: images are out of scope (docs/checkpointing.md). The suffix
#: alone is not trusted: a file is left unscanned only when its bytes also start with an
#: image signature (`_is_image`), so a text file named `shot.png` is scanned (QUA-2847).
UNSCANNED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
#: The signatures `_is_image` accepts. No BMP: its two-byte `BM` is not a signature
#: worth trusting, so a `.bmp` is scanned.
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
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
#: A withheld entry's `marker` for a private-text hit: this prefix, then the private
#: file it came from (`private text (private/developer_instructions.md)`), never the text.
PRIVATE_MARKER = "private text"

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


def _is_image(name: str, head: bytes) -> bool:
    """Whether the gate may leave a file unscanned: an image suffix and bytes that
    start with a PNG, JPEG, GIF or WEBP signature. `head` needs its first 12 bytes."""
    if PurePosixPath(name).suffix.lower() not in UNSCANNED_SUFFIXES:
        return False
    return head.startswith(_IMAGE_MAGIC) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")


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
    create_board: Path | None = None     # create.html, when the runs hold CreateBench grades
    create_board_json: Path | None = None   # create.json: the same board as data (QUA-2922)
    report: Path | None = None           # report.html, for an experiment view
    missing: list[str] = field(default_factory=list)   # episode dirs named but not on disk


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


# The private-text check's normalisation: HTML entities and JSON/string escapes undone (a
# transcript is JSON, a page is escaped HTML), lower case, every run of non-alphanumerics
# a word break.
_ESCAPE = re.compile(r"\\(?:u[0-9a-fA-F]{4}|.)")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _words(text: str) -> list[str]:
    return _NON_WORD.sub(" ", _ESCAPE.sub(" ", html.unescape(text)).lower()).split()


class _PrivateText:
    """Every PRIVATE_WINDOW-word run of the `private/` files of `episode_dirs`, to look
    for in a portable view's files (QUA-2869). Empty (falsy) when no episode has one."""

    def __init__(self, episode_dirs: Any = ()) -> None:
        self.windows: dict[str, str] = {}
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
                    self.windows.setdefault(" ".join(ws[i:i + PRIVATE_WINDOW]),
                                            f"{PRIVATE_DIR}/{f.relative_to(priv).as_posix()}")

    def __bool__(self) -> bool:
        return bool(self.windows)

    def find(self, data: bytes) -> tuple[str, str] | None:
        """(the private file, the window's first words) of the first private run in
        `data`, sampled every PRIVATE_STRIDE words; None when there is none."""
        if not self.windows:
            return None
        ws = _words(data.decode("utf-8", errors="replace"))
        for i in range(0, len(ws) - PRIVATE_WINDOW + 1, PRIVATE_STRIDE):
            w = " ".join(ws[i:i + PRIVATE_WINDOW])
            if w in self.windows:
                return self.windows[w], " ".join(ws[i:i + 4]) + " …"
        return None


def _private_marker(source: str) -> str:
    return f"{PRIVATE_MARKER} ({source})"


def is_private_hit(hit: dict) -> bool:
    """Whether a withheld entry is a private-text hit (not a credential marker)."""
    return str(hit.get("marker") or "").startswith(PRIVATE_MARKER)


def _scan(data: bytes, private: _PrivateText | None) -> str | None:
    """The gate's one question: the marker `data` carries — a credential marker's name
    (`scan_for_secrets`), else a private-text marker — or None when it is clean."""
    found = scan_for_secrets(data)
    if found is not None:
        return found[0]
    hit = private.find(data) if private else None
    return _private_marker(hit[0]) if hit else None


@dataclass
class _Gate:
    """`scan_for_secrets` and the private-text check in front of every text file a
    portable view writes. Disabled (a plain writer) for a local view. `hits` collects
    `{"episode", "file", "marker"}`."""
    out_dir: Path
    enabled: bool
    hits: list[dict] = field(default_factory=list)
    #: The episodes' private text (QUA-2869); None or empty: nothing private to look for.
    private: _PrivateText | None = None

    def rel(self, path: Path) -> str:
        return path.relative_to(self.out_dir).as_posix()

    def check(self, data: bytes, path: Path, episode: str | None) -> dict | None:
        """Record and return the hit for `data` bound for `path`, or None if clean."""
        if not self.enabled:
            return None
        marker = _scan(data, self.private)
        if marker is None:
            return None
        hit = {"episode": episode, "file": self.rel(path), "marker": marker}
        self.hits.append(hit)
        logger.warning("view: withheld %s (%s %r)", hit["file"],
                       "private text" if is_private_hit(hit) else "credential marker",
                       hit["marker"])
        return hit

    def write(self, path: Path, data: str | bytes, episode: str | None,
              mtime: float | None = None) -> dict | None:
        """Write `data` to `path` unless it carries a credential marker; the hit or None.
        A withheld file is not written, and an older copy at `path` is removed. A real
        image (`_is_image`: suffix and signature) is written unscanned. `mtime`, when
        given, is set on the written file (a copy keeps its source's time, so a sync by
        mtime skips it when it has not changed)."""
        b = data.encode("utf-8") if isinstance(data, str) else data
        hit = None if _is_image(path.name, b[:12]) else self.check(b, path, episode)
        if hit is not None:
            path.unlink(missing_ok=True)
            return hit
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return None

    def copy(self, src: Path, dst: Path, episode: str | None) -> dict | None:
        """Copy `src` to `dst` through the gate, keeping `src`'s mtime. Only a real
        image goes unscanned: a text file with an image suffix is scanned."""
        return self.write(dst, src.read_bytes(), episode, mtime=src.stat().st_mtime)

    def replace(self, path: Path, text: str) -> None:
        """Write a replacement (a stub) the gate built itself. Scanned all the same; one
        that would not pass (a marker in a case id?) is left unwritten."""
        b = text.encode("utf-8")
        if self.enabled and _scan(b, self.private) is not None:
            logger.warning("view: stub for %s not written: it matches a marker", path)
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b)


def scan_view(view_dir: Path | str, private: _PrivateText | None = None) -> list[dict]:
    """Every file under `view_dir` that matches a credential marker — or, with
    `private`, carries a run of an episode's private text — as withheld entries
    (`--index-from` on a merged folder: whatever it holds is re-checked; a portable
    build's backstop). Real images (`_is_image`) are not scanned; neither are the files
    an index rebuild rewrites."""
    view_dir = Path(view_dir)
    regenerated = {"index.html", MANIFEST, "style.css", MARKER}
    hits = []
    for p in sorted(view_dir.rglob("*")):
        rel = p.relative_to(view_dir)
        if not p.is_file() or (len(rel.parts) == 1 and rel.name in regenerated):
            continue
        if p.suffix.lower() in UNSCANNED_SUFFIXES:
            with p.open("rb") as fh:
                if _is_image(p.name, fh.read(12)):
                    continue
        marker = _scan(p.read_bytes(), private)
        if marker is not None:
            hits.append({"episode": _episode_of(rel), "file": rel.as_posix(),
                         "marker": marker})
    return hits


def private_text_hits(out_dir: Path | str, episode_dirs: list[Path]) -> list[dict]:
    """Files under `out_dir` that carry PRIVATE_WINDOW consecutive words of any
    `<episode>/private/` file of `episode_dirs` (`{"file", "source", "words"}`; the
    matched words are cut to a short prefix). The gate's own check (`_PrivateText`),
    run over a finished folder. Real images are not read. [] when no episode has a
    private folder."""
    private = _PrivateText(episode_dirs)
    if not private:
        return []
    out_dir = Path(out_dir)
    hits = []
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file() or p.name == MARKER:
            continue
        data = p.read_bytes()
        if _is_image(p.name, data[:12]):
            continue
        found = private.find(data)
        if found is not None:
            hits.append({"file": p.relative_to(out_dir).as_posix(), "source": found[0],
                         "words": found[1]})
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


def _what(hit: dict) -> str:
    """How a withheld entry names what matched: the credential marker (as character
    references) or the private file the text came from."""
    if is_private_hit(hit):
        return f"<code>{E(hit['marker'])}</code>"
    return f"credential marker <code>{_html_marker(hit['marker'])}</code>"


def _notice_html(hits: list[dict], nl: bool = False) -> str:
    """The episode page's notice: one line per withheld file ("" when there is none,
    so a clean page is byte for byte what it was before the gate)."""
    if not hits:
        return ""
    lines = "".join(f"<li>withheld: {_what(h)} in <code>{E(h['file'])}</code></li>"
                    for h in hits)
    return (f'<div class="banner wh-notice"><p>The credential gate withheld '
            f'{len(hits)} file(s) of this episode (not written; never the matched text):</p>'
            f"<ul>{lines}</ul></div>" + ("\n" if nl else ""))


def _stub_page(key: str, case: str, hits: list[dict]) -> str:
    """What stands in for a withheld episode page."""
    what = ("an episode's private text" if any(map(is_private_hit, hits))
            else "a credential marker")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(case)} · withheld</title>
<link rel="stylesheet" href="../style.css"></head>
<body class="ep">
<p class="nav"><a href="../index.html">← all episodes</a></p>
<h1>{E(case)} <span class="ho">{E(WITHHELD_BADGE)}</span></h1>
<p class="meta">episode {E(key)}</p>
{_notice_html(hits)}
<p>This page matched {what}, so the view did not write it. Remove the
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
    exp: dict | None = None           # its experiment cell (`_exp_of` of an `ab.experiment_episodes` entry)


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


def _write_images(entry: TimelineEntry, img_dir: Path, start: int, gate: _Gate | None = None,
                  episode: str | None = None, mtime: float | None = None
                  ) -> tuple[list[str], int]:
    """Extract `entry`'s images into `img_dir` through `gate` (every one that is not a
    real image is scanned, a `.bin` always), each with `mtime` (its transcript's); the
    `<img>` tags and the next index."""
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
        if gate is None:
            img_dir.mkdir(parents=True, exist_ok=True)
            (img_dir / name).write_bytes(data)
            if mtime is not None:
                os.utime(img_dir / name, (mtime, mtime))
        elif gate.write(img_dir / name, data, episode, mtime=mtime) is not None:
            tags.append(f'<span class="dim">[image {k}: withheld by the credential gate]</span>')
            continue
        src = f"{img_dir.name}/{name}"
        tags.append(f'<a href="{src}" target="_blank" rel="noopener">'
                    f'<img loading="lazy" src="{src}" alt="image {k}"></a>')
    return tags, k


def _timeline_html(entries: list[TimelineEntry], img_dir: Path, raw_href: str,
                   gate: _Gate | None = None, episode: str | None = None,
                   mtime: float | None = None) -> tuple[str, int, int]:
    """(html, images written, tool calls) for one transcript; images go through
    `gate` with `mtime` (see `_write_images`)."""
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
            tags, k = _write_images(e, img_dir, k, gate, episode, mtime)
            body = _fold(e.text, lines=4, chars=400) if e.text else ""
            parts.append(f'<div class="prompt"><b>sent to the agent</b>{body}{"".join(tags)}</div>')
        elif e.kind == "call":
            calls += 1
            server = f' <span class="dim">{E(e.server)}</span>' if e.server else ""
            parts.append(f'<div class="call"><b>→ {E(e.name or "?")}</b>{server}'
                         f'{_args(e.input)}</div>')
        elif e.kind == "result":
            tags, k = _write_images(e, img_dir, k, gate, episode, mtime)
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
    authored = _read(d / AUTHORED_CASE if d else None)
    # Both blocks are "" — no line at all — on a page that has neither, so a run
    # view's page is what it was before QUA-2869.
    authored_html = (f'<details open><summary><b>Authored test case</b> ({AUTHORED_CASE})'
                     f'</summary><pre>{E(authored)}</pre></details>\n'
                     if authored is not None else "")
    cell_html = ""
    if ep.exp:
        x = ep.exp
        what = (f"creation episode (attempt {x['attempt']})" if x["stage"] == "author" else
                f"grade run {x['role']} (attempt {x['attempt']})")
        cell_html = (f'<p class="meta">experiment cell <b>{E(x["cell"])}</b> · {E(what)}'
                     + (f' · excluded: {E(x["excluded"])}' if x.get("excluded") else "")
                     + ' · <a href="../index.html#cells">all cells</a></p>\n')
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
{cell_html}<h2>Verdict</h2>
{_verdict_table(ep)}
<h2>Reports (scored)</h2>
{_reports_html(ep.recorded, ep.rescored)}
<details><summary><b>Brief sent to the agent</b> (instruction_sent.md)</summary>
<pre>{E(brief) if brief is not None else "(no instruction_sent.md)"}</pre></details>
{authored_html}<details open><summary><b>{E(journey.FILENAME)}</b></summary>
<pre>{E(findings) if findings is not None else "(no findings file)"}</pre></details>
<h2>Transcript · {calls} tool call(s) · {shots} image(s)</h2>
{timeline_html or '<p class="dim">(no transcript)</p>'}
</body></html>
"""


# ── the index ──────────────────────────────────────────────────────────────────

# ── the run page: versions, the board, charts R2–R4 (QUA-2918) ─────────────────
# Everything below reads the manifest's own blocks (`_measurement`), so the page and
# `manifest.json` cannot disagree, and like them it is a pure function of the summaries
# and `run.json`: `build_index` reproduces it byte for byte.

def _pct(v: Any) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _version_text(single: Any, values: list | None, unstamped: int = 0,
                  fmt: Callable[[Any], str] = str) -> str:
    """A version as the page names it: the single value, else the distinct values and
    the unstamped count, else `unstamped`."""
    if single is not None:
        return fmt(single)
    parts = [fmt(x) for x in values or []]
    if unstamped:
        parts.append(f"{unstamped} unstamped" if parts else "unstamped")
    return ", ".join(parts) or "—"


def _brief(b: Any) -> str:
    return f"v{b}"


def _heldout_text(v: dict, held_episodes: int) -> str:
    if v.get("heldout") is None and not v.get("heldout_versions") and not held_episodes:
        return "none"
    return _version_text(v.get("heldout"), v.get("heldout_versions"),
                         v.get("heldout_unstamped") or 0)


def _mixed_parts(v: dict) -> list[str]:
    """What a `mixed` run mixes, one `name: values` entry per disagreeing version."""
    out = []
    if v.get("mode") == "mixed":
        out.append("modes: " + ", ".join(v.get("modes") or []))
    for name, key, fmt in (("corpus", "corpus", str), ("held-out", "heldout", str),
                           ("brief", "brief", _brief)):
        vals, un = v.get(f"{key}_versions") or [], v.get(f"{key}_unstamped") or 0
        if corpus.is_mixed(vals, un):
            out.append(f"{name}: {_version_text(None, vals, un, fmt)}")
    return out


def _rescore_sentence(v: dict, rw: dict, held: int = 0, held_not_rescored: int = 0) -> str:
    """How the run's rescored verdicts relate to what it recorded (`rescored_with`).
    `held` is the run's held-out episode count and `held_not_rescored` how many of them
    have no rescored verdict. Every held-out version is named through `_heldout_text`,
    so the sentence and the version line above it say the same thing. A rescore with
    no held-out stamp on a run that recorded one was built without the held-out split:
    it is compared on the corpus alone and says its held-out episodes were not
    rescored, rather than calling the public rescore a different measurement."""
    if not rw["stamped"] and not rw["unstamped"]:
        return "not rescored: the board shows the verdicts recorded at run time"
    if not rw["stamped"]:
        return ("rescore corpus unstamped (summaries built before the stamp existed, or "
                "rescored against a corpus other than the default)")
    now_c = _version_text(rw["corpus"], rw["corpus_versions"], rw["unstamped"])
    # The build's own split: "none" when it had none, whatever the run recorded.
    now_h = _heldout_text(rw, 0)
    if rw["unstamped"] or rw["corpus"] is None or (rw["heldout"] is None
                                                   and rw["heldout_versions"]):
        return f"rescored against mixed corpora: corpus {now_c} · held-out {now_h}"
    rec_c = _version_text(v.get("corpus"), v.get("corpus_versions"),
                          v.get("corpus_unstamped") or 0)
    rec_h = _heldout_text(v, held)
    if rw["heldout"] is None and (v.get("heldout") or v.get("heldout_versions")):
        skipped = (f"no held-out split at build time: "
                   f"{_plural(held_not_rescored, 'held-out episode')} not rescored")
        if v.get("corpus") == rw["corpus"]:
            return f"rescored with the recorded corpus; {skipped}"
        return (f"recorded under corpus {rec_c}, rescored with corpus {now_c}: a different "
                f"measurement, not a correction (docs/heldout.md); {skipped}")
    if (v.get("corpus"), v.get("heldout")) == (rw["corpus"], rw["heldout"]):
        return "rescored with the recorded corpus"
    return (f"recorded under corpus {rec_c} · held-out {rec_h}, rescored with corpus "
            f"{now_c} · held-out {now_h}: a different measurement, not a correction "
            f"(docs/heldout.md)")


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _versions_html(by_run: dict[str, list[_Summary]], measures: dict[str, dict]) -> str:
    """Per run, under the state line: what it measured — mode, corpus, held-out split,
    brief, arm, DevLoop server, comparable set (`_versions`) — a MIXED badge naming what
    disagrees, and for a journey run how its rescore relates to that (`rescored_with`)
    with the moved / denominator-changed / not-rescored counts."""
    out = []
    for run_id in sorted(by_run):
        meas = measures[run_id]
        v, rw = meas["versions"], meas["rescored_with"]
        held = meas["heldout"]["episodes"]
        parts = [f"benchmark: {v.get('mode') or '—'}",
                 "corpus " + _version_text(v.get("corpus"), v.get("corpus_versions"),
                                           v.get("corpus_unstamped") or 0),
                 f"held-out split {_heldout_text(v, held)} ({_plural(held, 'episode')})",
                 "brief " + _version_text(v.get("brief"), v.get("brief_versions"),
                                          v.get("brief_unstamped") or 0, _brief),
                 "arm " + _version_text(v.get("condition"), v.get("conditions")),
                 "DevLoop " + _version_text(v.get("devloop"), v.get("devloops")),
                 f"set {v.get('set_key') or '—'}"]
        name = f"<b>Run {E(run_id or '(no run id)')}</b> · " if len(by_run) > 1 else ""
        badge = ""
        if v.get("mixed"):
            badge = (f'<span class="mixed">MIXED</span> <span class="warn">'
                     f'{E("; ".join(_mixed_parts(v)))} — not one measurement</span><br>')
        out.append(f'<p class="versions">{name}{badge}{E(" · ".join(parts))}</p>')
        if any(e.result.task_type == journey.TASK_TYPE for e in by_run[run_id]):
            counts = (f"{_plural(meas['moved'], 'episode')} moved on rescore · "
                      f"{_plural(meas['present_changed'], 'denominator')} changed · "
                      f"{rw['not_rescored']} not rescored")
            held_skipped = sum(1 for e in by_run[run_id] if e.held and e.rescored_result is None
                               and e.result.task_type == journey.TASK_TYPE)
            sentence = _rescore_sentence(v, rw, held, held_skipped)
            out.append(f'<p class="dim">{E(sentence)} · {E(counts)}</p>')
    return "\n".join(out)


def _board_rows(now: list[dict], recorded: list[dict]) -> list[tuple[dict, dict | None, bool]]:
    """`(shown row, recorded row, rescored)` in ranking order: the rescored row where the
    run has one, else the recorded one (a row none of whose episodes was rescored)."""
    rec = {viz.row_id(r): r for r in recorded}
    have = {viz.row_id(r) for r in now}
    rows = ([(r, rec.get(viz.row_id(r)), True) for r in now]
            + [(r, r, False) for r in recorded if viz.row_id(r) not in have])
    return sorted(rows, key=lambda t: journey.ranking_key(t[0]))


def _board_numbers(row: dict) -> list[tuple[str, str]]:
    c = journey.rates_cells(row)
    return [("false alarm", c["false_alarm"]), ("catch", c["catch"]),
            ("false reports", str(row.get("false_reports", 0))),
            ("completion", _pct(row.get("completion")))]


def _recorded_cell(row: dict, rec: dict | None, rescored: bool) -> str:
    """The dim recorded column: only the numbers the run-time board had differently."""
    if not rescored:
        return "not rescored: recorded shown"
    if rec is None:
        return "—"
    was = dict(_board_numbers(rec))
    return " · ".join(f"{k} {was[k]}" for k, now in _board_numbers(row) if was[k] != now)


#: The board legend: `journey.RATES_LEGEND` without its blocker-recall clause, which
#: the page does not show (`_BOARD_DROP`), and a line saying where to find it.
BOARD_RATES_LEGEND = " · ".join(p for p in journey.RATES_LEGEND.split(" · ")
                                if not p.startswith("blocker recall"))
BLOCKER_OFF_NOTE = ("blocker recall is not shown here: it needs the current corpus at "
                    "build time (see `show --run`)")
BOARD_COLUMNS_NOTE = ("episodes = scored episodes (+N excluded from every number; N "
                      "truncated = step budget exhausted: not completed, all seeded bugs "
                      "missed) · (+N unscored) = completion not scored · $/ep = mean over "
                      "priced episodes · min/ep = median agent wall-clock · recorded = the "
                      "run-time board, only where it differs")


def _board_tr(i: int, row: dict, rec: dict | None, rescored: bool) -> str:
    held = bool(row.get("heldout"))
    c, money = journey.rates_cells(row), journey.cost_cells(row)
    star = "*" if row.get("mixed_corpus") or row.get("mixed_brief") else ""
    eps = [str(row.get("episodes", 0))]
    if row.get("excluded_episodes"):
        eps.append(f"+{row['excluded_episodes']} excluded")
    if row.get("truncated"):
        eps.append(f"{row['truncated']} truncated")
    if rescored and row.get("not_rescored"):
        eps.append(f"+{row['not_rescored']} not rescored")
    completion = _pct(row.get("completion"))
    if row.get("completion_unscored"):
        completion += f" (+{row['completion_unscored']} unscored)"
    split = (f'held-out <span class="ho">{E(HELDOUT_BADGE)}</span>' if held else "public")
    return (f'<tr><td>{"H" if held else ""}{i}</td>'
            f"<td>{E(row.get('agent'))} · {E(row.get('model'))} · {E(row.get('condition'))}"
            f"{star}</td><td>{split}</td><td>{E(' · '.join(eps))}</td>"
            f"<td class=num>{E(c['false_alarm'])}</td><td class=num>{E(c['catch'])}</td>"
            f"<td class=num>{E(c['integrity'])}</td>"
            f"<td class=num>{E(str(row.get('false_reports', 0)))}</td>"
            f"<td class=num>{E(completion)}</td><td class=num>{E(money['cost'])}</td>"
            f"<td class=num>{E(money['minutes'])}</td>"
            f'<td class="dim">{E(_recorded_cell(row, rec, rescored))}</td></tr>')


def _summary_html(by_run: dict[str, list[_Summary]], measures: dict[str, dict]) -> str:
    """Per run: the journey board `show --run` prints — ranking order (`journey.
    ranking_key`), public block then held-out block, false alarm and catch as `k/n p%
    [lo–hi]`, integrity @200, false reports, completion, $/ep, min/ep, through the CLI's
    own cell formatters — over the rescored verdicts (`board.now`), with the run-time
    numbers in a dim column where they differ. A row none of whose episodes was rescored
    shows its recorded numbers and says so. Blocker recall is not on the page: it reads
    the corpus of the checkout that builds the view (`_BOARD_DROP`)."""
    blocks = []
    for run_id in by_run:
        board = measures[run_id]["board"]
        rows = _board_rows(board["now"], board["recorded"])
        if not rows:
            continue
        lines, n = [], {False: 0, True: 0}
        for row, rec, rescored in rows:
            held = bool(row.get("heldout"))
            n[held] += 1
            lines.append(_board_tr(n[held], row, rec, rescored))
        shown = [r for r, _, _ in rows]
        notes = [f'<p class="dim">{E(BOARD_RATES_LEGEND)} · {E(BLOCKER_OFF_NOTE)}</p>',
                 f'<p class="dim">{E(BOARD_COLUMNS_NOTE)}</p>',
                 f'<p class="dim">{E(journey.RANKING_NOTE)}</p>']
        if not n[True]:
            notes.append(f'<p class="warn">{E(journey.NO_HELDOUT_NOTE)}</p>')
        if any(r.get("mixed_corpus") for r in shown):
            notes.append(f'<p class="warn">{E(journey.MIXED_CORPUS_NOTE)}</p>')
        if any(r.get("mixed_brief") for r in shown):
            notes.append(f'<p class="warn">* {E(journey.MIXED_BRIEF_NOTE)}</p>')
        excluded = sum(r.get("excluded_episodes") or 0 for r in shown)
        if excluded:
            notes.append(f'<p class="dim">{_plural(excluded, "episode")} excluded from '
                         f"every number above (env/infra failure, contamination, unclean "
                         f"MCP session or rate limit)</p>")
        if note := journey.integrity_note(shown):
            notes.append(f'<p class="warn">{E(note)}</p>')
        missing = measures[run_id]["rescored_with"]["not_rescored"]
        if missing:
            notes.append(f'<p class="dim">{_plural(missing, "journey episode")} of this run '
                         f"were not rescored (see their rows); the rescored numbers leave "
                         f"them out, as <code>rescore_journey.py --dry-run</code> does.</p>")
        blocks.append(
            f"<h3>Run {E(run_id or '(no run id)')}</h3>"
            "<div class=tablewrap><table class=board><tr><th>#</th><th>agent · model · arm</th>"
            "<th>split</th><th>episodes</th><th>false alarm / clean case</th>"
            "<th>catch / seeded defect</th>"
            f"<th>integrity @{journey.INTEGRITY_N}</th><th>false reports</th>"
            "<th>completion</th><th>$/ep</th><th>min/ep</th><th>recorded</th></tr>"
            + "".join(lines) + "</table></div>" + "".join(notes))
    return "\n".join(blocks)


# ── charts R2–R4 (`viz`), each with a table twin ───────────────────────────────

RATE_PANELS = (("false_alarm", "false alarm / clean case"),
               ("catch", "catch / seeded defect"))


def _tr(cells: list[str], cls: str = "") -> str:
    """One table row of already-escaped cells."""
    attr = f' class="{cls}"' if cls else ""
    return f"<tr{attr}>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _twin(head: list[str], rows: list[str], what: str) -> str:
    """A chart's table twin: the same numbers as text, folded under the chart. The one
    twin on every page (run charts R2–R4, experiment charts X1–X2): `head` is plain
    text, `rows` rendered rows (`_tr`)."""
    th = "".join(f"<th>{E(h)}</th>" for h in head)
    return (f'<details><summary class="dim">{E(what)} as a table</summary>'
            f'<div class="tablewrap"><table class="idx"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></details>')


def _figure(chart: str, caption: str, fid: str = "") -> str:
    """A chart and its caption (already-escaped HTML): the one caption style on every
    page."""
    attr = f' id="{fid}"' if fid else ""
    return (f'<figure class="fig"{attr}>{chart}<figcaption class="dim">{caption}'
            f"</figcaption></figure>")


def _rates_chart(board: dict) -> str:
    """R2: false alarm and catch with 95% intervals — the board rows, then each app
    (`by_app`); public before held-out, held-out marks hollow."""
    rows = [r for r, _, _ in _board_rows(board["now"], board["recorded"])]
    apps = [r for r, _, _ in _board_rows(board["by_app_now"], board["by_app_recorded"])]
    if not rows:
        return ""

    def label(r: dict) -> str:
        return viz.row_label(r) if r.get("app") is None else "↳ " + str(r["app"])

    # Each board row, then its apps under it, so the eye reads one lane at a time. A
    # lane with one app in the split has no app rows: they would repeat its numbers.
    ordered: list[dict] = []
    for r in rows:
        mine = [a for a in apps if viz.row_id(a)[:4] == viz.row_id(r)[:4]]
        ordered += [r] + (mine if len(mine) > 1 else [])
    chart = viz.dots_ci(ordered, RATE_PANELS, label=label)
    twin = _twin(["row", "split", "false alarm / clean case", "catch / seeded defect"],
                 [_tr([E(viz.row_label(r)), "held-out" if r.get("heldout") else "public",
                       E(journey.rates_cells(r)["false_alarm"]),
                       E(journey.rates_cells(r)["catch"])]) for r in ordered], "rates")
    return _figure(chart, "Dots are the rate, whiskers its 95% Wilson interval; hollow "
                          "marks are held-out. Rescored numbers where the run has them.") + twin


def _strip_status(e: _Summary) -> tuple[str, str]:
    """An episode's R3 status from its "now" verdict (rescored where there is one) and
    a sentence for its title. Seeded: nothing present (no seeded defect under that
    verdict, so out of the catch denominator: shown as excluded, never as missed),
    else caught, else truncated, else never reached (the seeded site's canary did not
    fire), else missed with an unmatched grounded report (an artifact of the scorer?)
    or missed silently. Clean: a false report or none."""
    m0 = e.result.metrics or {}
    m = (e.rescored_result.metrics or {}) if e.rescored_result is not None else m0
    if is_excluded(m):
        return "excluded", f"excluded: {exclusion_reason(m)}"
    reports = [x for x in m.get("reports") or [] if isinstance(x, dict)]
    if e.arm != "seeded":
        fr = m.get("false_reports") or 0
        return (("false_report", _plural(fr, "false report")) if fr
                else ("clean", "no false report"))
    present = {str(x) for x in m.get("bugs_present") or []}
    if not present:
        return "excluded", "nothing present: no seeded defect to catch, not in the catch rate"
    found = {str(x) for x in m.get("bugs_found") or []}
    if found:
        return "caught", f"caught {len(found)}/{len(present)}"
    if m0.get("truncated"):
        return "truncated", "truncated: step budget exhausted, seeded defect missed"
    fired = m0.get("fault_fired")
    if isinstance(fired, list) and not present & {str(x) for x in fired}:
        return "unreached", "never reached: the seeded site's canary did not fire"
    grounded = sum(1 for x in reports if not x.get("matched") and x.get("grounded"))
    if grounded:
        return "artifact", (f"missed · {_plural(grounded, 'unmatched grounded report')}: "
                            f"artifact?")
    return "silent", f"missed · {_plural(len(reports), 'report')}, none grounded"


def _strip_chart(eps: list[_Summary], uid: str) -> str:
    """R3: one cell per journey episode, seeded and clean lanes, grouped by app (public
    apps first), each linking to its page."""
    js = [e for e in eps if e.result.task_type == journey.TASK_TYPE]
    if not js:
        return ""
    keyed = sorted(js, key=lambda e: (e.held, _app_id(e.result), _case_of(e.result), e.arm,
                                      e.result.trial, e.key))
    cells, twin = [], []
    for e in keyed:
        r = e.result
        status, what = _strip_status(e)
        app = _app_id(r) or "—"
        who = f"{_case_of(r)} · trial {r.trial} · {r.agent} · {clean_model_name(r.model)} · {r.condition}"
        cells.append({"lane": "seeded" if e.arm == "seeded" else "clean",
                      "group": f"H·{app}" if e.held else app, "status": status,
                      "title": f"{who}{' · held-out' if e.held else ''}: {what}", "key": e.key})
        twin.append(_tr([
            E(app) + (f' <span class="ho">{E(HELDOUT_BADGE)}</span>' if e.held else ""),
            f'<a href="ep/{E(e.key)}.html">{E(_case_of(r))}</a>', E(e.arm),
            E(str(r.trial)), E(f"{r.agent} · {clean_model_name(r.model)} · {r.condition}"),
            f"{viz.STATUS[status][1]} {E(what)}"]))
    chart = viz.strip(cells, ("seeded", "clean"), lambda c: f"ep/{c['key']}.html", uid)
    return (_figure(chart, "One cell per episode, grouped by app, public apps first (H· = "
                           "a held-out app); the verdict is the rescored one where there is "
                           "one. Each cell links to its episode.")
            + _twin(["app", "case", "arm", "trial", "agent · model · arm", "status"], twin,
                    "episodes"))


def _drift_chart(meas: dict) -> str:
    """R4: recorded (hollow) → rescored (filled) per board row — only when the rescore
    moved an episode."""
    if not meas["moved"]:
        return ""
    board = meas["board"]
    chart = viz.drift(board["recorded"], board["now"], RATE_PANELS)
    now = {viz.row_id(r): r for r in board["now"]}
    twin = []
    for rec in board["recorded"]:
        cur = now.get(viz.row_id(rec))
        for prefix, title in RATE_PANELS:
            twin.append(_tr([E(viz.row_label(rec)), E(title),
                             E(journey.rates_cells(rec)[prefix]),
                             E(journey.rates_cells(cur)[prefix] if cur else "not rescored")]))
    counts = (f"{_plural(meas['moved'], 'episode')} moved · "
              f"{_plural(meas['present_changed'], 'denominator')} changed")
    return (_figure(chart, f"{E(counts)}. Hollow = recorded at run time, filled = "
                           f"rescored; rows not rescored keep only their recorded mark.")
            + _twin(["row", "metric", "recorded", "rescored"], twin, "drift"))


def _charts_html(by_run: dict[str, list[_Summary]], measures: dict[str, dict]) -> str:
    """Charts R2–R4 per journey run, between the board and the episode table."""
    out = []
    for i, run_id in enumerate(by_run):
        meas = measures[run_id]
        rates = _rates_chart(meas["board"])
        if not rates:
            continue
        name = f"Run {run_id or '(no run id)'} · " if len(by_run) > 1 else ""
        out.append(f"<h3>{E(name)}Rates by row and app</h3>{rates}")
        out.append(f"<h3>{E(name)}Episodes by outcome</h3>"
                   + _strip_chart(by_run[run_id], f"strip{i}"))
        drift = _drift_chart(meas)
        if drift:
            out.append(f"<h3>{E(name)}Rescore drift</h3>{drift}")
    return "\n".join(out)


def _row(ep: _Episode, shots: int) -> dict[str, Any]:
    r, m0, m1 = ep.result, ep.recorded, ep.rescored
    now = m1 if m1 is not None else {}
    moved = _moved(m0, m1)
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
                state_html: str = "", withheld_html: str = "", links: str = "",
                rescore_note: bool = True, charts: str = "") -> str:
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
{state_html}
{held_note}{withheld_html}
{RESCORE_NOTE if rescore_note else NO_RESCORE_NOTE}
{summary}
{charts + chr(10) if charts else ""}{links + chr(10) if links else ""}{nr_html}
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
  if (q && !r.case.toLowerCase().includes(q) && !(CELLS && (r.cell || '').toLowerCase().includes(q))) return false;
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
.ip{background:var(--link);color:var(--bg);font-size:12px;padding:1px 6px;border-radius:4px;white-space:nowrap}
.mixed{background:var(--st-warn);color:#1d1d1f;font-size:12px;font-weight:600;padding:1px 6px;border-radius:4px}
.versions{margin:4px 0}table.board td.num,table.board td:nth-child(2),table.board td:nth-child(3){white-space:nowrap}table.board td.dim{font-size:12px}
:root{--s1:#2a78d6;--s2:#eb6834;--seq1:#cde2fb;--seq2:#9ec5f4;--seq3:#6da7ec;--seq4:#3987e5;
 --seq5:#256abf;--seq6:#184f95;--seq7:#0d366b;--st-good:#0ca30c;--st-warn:#fab219;
 --st-serious:#ec835a;--st-crit:#d03b3b}
@media (prefers-color-scheme:dark){:root{--s1:#3987e5;--s2:#d95926;--seq1:#0d366b;--seq2:#184f95;
 --seq3:#256abf;--seq4:#3987e5;--seq5:#6da7ec;--seq6:#9ec5f4;--seq7:#cde2fb}}
svg.chart{display:block;max-width:100%;height:auto;margin:8px 0;font:11px -apple-system,system-ui,sans-serif}
.chart text{fill:var(--fg)}.chart text.dim,.chart .tick,.chart .legend text{fill:var(--dim)}
.chart .ptitle{font-weight:600}.chart .val{font-weight:600;paint-order:stroke;stroke:var(--bg);stroke-width:3px}
.chart .grid{stroke:var(--line);stroke-width:1}.chart .axis{stroke:var(--dim);stroke-width:1}
.chart .whisker,.chart .conn{stroke:var(--s1);stroke-width:2;stroke-linecap:round}.chart .conn{opacity:.5}
.chart .hit{fill:transparent}.chart .pt .dot{fill:var(--s1);stroke:var(--bg);stroke-width:2}
.chart .pt.ho .dot,.chart .pt.rec .dot{fill:var(--bg);stroke:var(--s1)}
.chart .pt.s2 .dot{fill:var(--s2)}.chart .pt.s2 .whisker{stroke:var(--s2)}
.chart .pt:hover .dot{stroke:var(--fg)}.chart .lown{opacity:.6}
.chart .legend .key{fill:var(--s1)}.chart .legend .key.ho,.chart .legend .key.rec{fill:var(--bg);stroke:var(--s1);stroke-width:2}
.chart .legend .key.s2{fill:var(--s2)}
.chart .cell text{font-size:8px;pointer-events:none}.chart .cell:hover rect{stroke:var(--fg);stroke-width:1}
.chart .cell.st-good rect,.chart .key.st-good{fill:var(--st-good)}.chart .cell.st-warn rect,.chart .key.st-warn{fill:var(--st-warn)}
.chart .cell.st-serious rect,.chart .key.st-serious{fill:var(--st-serious)}.chart .cell.st-crit rect,.chart .key.st-crit{fill:var(--st-crit)}
.chart .cell.st-good text,.chart .cell.st-crit text{fill:#fff}.chart .cell.st-warn text,.chart .cell.st-serious text{fill:#1d1d1f}
.chart .cell.st-ex rect{stroke:var(--dim);stroke-width:1}.chart .key.st-ex{fill:none;stroke:var(--dim)}
.chart .hatch{stroke:var(--dim);stroke-width:1.5}
.chart .bar{fill:var(--s1)}.chart .bm:hover .bar{stroke:var(--fg);stroke-width:1}
figure.fig{margin:12px 0}figure.fig figcaption{font-size:12px;max-width:720px}
.heat td.hm{text-align:center;white-space:nowrap;min-width:56px}.heat .gl{font-size:10px;font-weight:600}
.hm0{background:var(--seq3);color:#1d1d1f}.hm1{background:var(--seq4);color:#1d1d1f}
.hm2{background:var(--seq5);color:#fff}.hm3{background:var(--seq6);color:#fff}.hm4{background:var(--seq7);color:#fff}
@media (prefers-color-scheme:dark){.hm0{color:#fff}.hm1,.hm2,.hm3,.hm4{color:#1d1d1f}}
.heatkey .hm{padding:1px 6px;margin-right:2px;border-radius:3px;font-size:11px;white-space:nowrap;display:inline-block}
.chk{font-weight:600;white-space:nowrap}.chk .g{font-size:14px}.chk.good .g{color:var(--st-good)}
.chk.crit .g{color:var(--st-crit)}.chk.warn .g{color:var(--st-warn)}
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
    #: Its experiment cell, for an experiment view (`_exp_of`); None otherwise.
    exp: dict | None = None
    #: Which corpus the rescore scored against (QUA-2917): `corpus.stamp()` —
    #: `{"corpus_version", "heldout_version"}` — when the build rescored against the
    #: DEFAULT corpus, None otherwise (an explicit corpus, no rescore, or a summary
    #: written before the stamp existed). Set only beside a `rescored_result`.
    rescored_with: dict | None = None

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
        if self.exp:
            doc["exp"] = self.exp
        if self.rescored_with:
            doc["rescored_with"] = self.rescored_with
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
                       withheld=_hit_list(doc.get("withheld")),
                       exp=_exp_of(doc["exp"]) if doc.get("exp") is not None else None,
                       rescored_with=_stamp_of(doc.get("rescored_with")))
        except (KeyError, TypeError, ValueError) as exc:
            raise ViewError(f"unreadable episode summary {where}: {exc}") from exc


#: What an episode's summary keeps of its experiment cell (`ab.experiment_episodes`):
#: everything but the episode dir, which is the runs tree's business.
_EXP_KEYS = ("cell", "arm", "case_id", "trial", "stage", "role", "attempt", "excluded")


def _exp_of(x: Any) -> dict:
    if not isinstance(x, dict):
        raise TypeError("exp is not a JSON object")
    return {"cell": str(x["cell"]), "arm": str(x.get("arm") or ""),
            "case_id": str(x.get("case_id") or ""), "trial": x.get("trial"),
            "stage": str(x["stage"]), "role": str(x.get("role") or ""),
            "attempt": int(x.get("attempt") or 1), "excluded": str(x.get("excluded") or "")}


def _stamp_of(v: Any) -> dict | None:
    """A summary's `rescored_with`, read back: a JSON object of version strings, or None."""
    if v is None:
        return None
    if not isinstance(v, dict):
        raise TypeError("rescored_with is not a JSON object")
    return {str(k): (str(x) if x is not None else None) for k, x in v.items()}


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


# ── what a run measured: the manifest's additive block (QUA-2917) ──────────────
#
# Every helper here is a pure function of the summaries (`ep/<key>.json`) — no clock, no
# host, no package version, no fact read from the current corpus — so `build_index` over
# merged summaries writes the same block a full build does, byte for byte.

def _moved(m0: dict, m1: dict | None) -> bool:
    """Whether the rescore moved an episode: completion, the sorted bugs found or the
    false-report count differ between recorded (`m0`) and rescored (`m1`) metrics. One
    definition for the index's highlighted rows and the manifest's `moved`."""
    return m1 is not None and (
        m0.get("completed") != m1.get("completed")
        or sorted(m0.get("bugs_found") or []) != sorted(m1.get("bugs_found") or [])
        or (m0.get("false_reports") or 0) != (m1.get("false_reports") or 0))


def _mode_of(task_type: str) -> str:
    """The benchmark mode a task type belongs to: `journey`, `create` (a CreateBench
    creation episode or a grade run of its authored case), else the task type itself."""
    if task_type == journey.TASK_TYPE:
        return journey.MODE
    from .create import grader, runner
    return "create" if task_type in (runner.TASK_TYPE, grader.TASK_TYPE) else task_type


def _devloop(r: RunResult) -> str:
    """The MCP server an episode was handed, as a lane label: `<tools_sha256[:8]>/
    <instructions_sha256[:8]>`; `bare` for the no-server arm (`mcp_server: null`);
    `unstamped` for a result from before the stamp, or a server whose identity could not
    be read."""
    prov = r.provenance or {}
    if "mcp_server" not in prov:
        return "unstamped"
    srv = prov["mcp_server"]
    if srv is None:
        return "bare"
    if isinstance(srv, dict) and srv.get("tools_sha256") and srv.get("instructions_sha256"):
        return f"{str(srv['tools_sha256'])[:8]}/{str(srv['instructions_sha256'])[:8]}"
    return "unstamped"


def _versions(eps: list[_Summary]) -> dict:
    """What the run measured: its mode and the corpus, held-out split, brief, arm and
    MCP server its episodes carry — over the journey episodes when there are any (the
    held-out version is stamped run-wide on every one of them), else over all. A single
    value is set only when every episode agrees; `mixed` when the corpus, held-out or
    brief versions disagree (or mix stamped and unstamped episodes), or the run mixes
    modes. Arm and server are lanes within one measurement, never `mixed`."""
    kinds = sorted({_mode_of(e.result.task_type) for e in eps})
    mode = (kinds[0] if len(kinds) == 1 else "mixed") if kinds else None
    scope = [e for e in eps if e.result.task_type == journey.TASK_TYPE] or list(eps)
    ms = [e.result.metrics or {} for e in scope]
    c, cs, cu = corpus.distinct_versions(ms, "corpus_version")
    h, hs, hu = corpus.distinct_versions(ms, "heldout_version")
    b, bs, bu = corpus.distinct_versions(
        [{"brief_version": (e.result.provenance or {}).get("brief_version")} for e in scope],
        "brief_version")
    conditions = sorted({e.result.condition for e in scope if e.result.condition})
    devloops = sorted({_devloop(e.result) for e in scope})
    mixed = (mode == "mixed" or corpus.is_mixed(cs, cu) or corpus.is_mixed(hs, hu)
             or corpus.is_mixed(bs, bu))
    out = {"mode": mode, "modes": sorted({e.result.task_type for e in eps}),
           "corpus": c, "corpus_versions": cs, "corpus_unstamped": cu,
           "heldout": h, "heldout_versions": hs, "heldout_unstamped": hu,
           "brief": b, "brief_versions": bs, "brief_unstamped": bu,
           "condition": conditions[0] if len(conditions) == 1 else None,
           "conditions": conditions,
           "devloop": devloops[0] if len(devloops) == 1 else None, "devloops": devloops,
           "mixed": mixed}
    out["set_key"] = _set_key(out)
    return out


def _set_key(v: dict) -> str | None:
    """`j-<corpus>-<held-out version | none>-b<brief | none>`: the comparable set a
    journey run belongs to. None for a mixed run, a non-journey run, or a run whose
    corpus is unstamped — none of those is one measurement of a known benchmark."""
    if v.get("mode") != journey.MODE or v.get("mixed") or not v.get("corpus"):
        return None
    return f"j-{v['corpus']}-{v.get('heldout') or 'none'}-b{v.get('brief') or 'none'}"


def _experiment_set_key(env: dict | None) -> str | None:
    """`c-<corpus>-g<grader version>-cb<create-brief version>` from an A/B experiment's
    registered environment (`ab.environment`); None when the state predates it."""
    env = env or {}
    c, g = env.get("corpus_version"), (env.get("runner") or {}).get("grader_version")
    cb = env.get("create_brief_version")
    if c is None or g is None or cb is None:
        return None
    return f"c-{c}-g{g}-cb{cb}"


def _rescored_with_block(eps: list[_Summary]) -> dict:
    """Which corpus the run's rescored verdicts were scored against, distinct over the
    journey summaries' `rescored_with` stamps: a single value only when every rescored
    episode carries the same stamp. `stamped` / `unstamped` count rescored episodes with
    and without a stamp; `not_rescored` counts journey episodes with no rescored verdict.
    `scorer` reads a `scorer_version` stamp key, which no build writes yet."""
    js = [e for e in eps if e.result.task_type == journey.TASK_TYPE]
    done = [e for e in js if e.rescored_result is not None]
    stamps = [e.rescored_with or {} for e in done]
    c, cs, _ = corpus.distinct_versions(stamps, "corpus_version")
    h, hs, _ = corpus.distinct_versions(stamps, "heldout_version")
    sc, scs, _ = corpus.distinct_versions(stamps, "scorer_version")
    stamped = sum(1 for e in done if e.rescored_with)
    return {"corpus": c, "corpus_versions": cs, "heldout": h, "heldout_versions": hs,
            "scorer": sc, "scorer_versions": scs, "stamped": stamped,
            "unstamped": len(done) - stamped, "not_rescored": len(js) - len(done)}


def _case_of(r: RunResult) -> str:
    m = r.metrics or {}
    if m.get("case_id"):
        return str(m["case_id"])
    if r.task_type == journey.TASK_TYPE:
        return journey.split_task_id(r.task_id)[0]
    return r.task_id


def _split_counts(eps: list[_Summary], held: bool) -> dict:
    """One split's size: episodes, excluded episodes (`failures.is_excluded` on the
    "now" verdict, rescored where there is one), distinct cases and apps, and the "now"
    completion counts the run's `completed` / `scored` blend."""
    part = [e for e in eps if e.held == held]
    now = [(e.rescored_result or e.result).metrics or {} for e in part]
    verdicts = [e.verdict for e in part]
    return {"episodes": len(part), "excluded": sum(1 for m in now if is_excluded(m)),
            "cases": len({_case_of(e.result) for e in part}),
            "apps": len({a for e in part if (a := _app_id(e.result))}),
            "completed": sum(v is True for v in verdicts),
            "scored": sum(isinstance(v, bool) for v in verdicts)}


def _provider(model: str) -> str | None:
    return "fireworks" if (model or "").startswith("accounts/fireworks/") else None


def _models(eps: list[_Summary]) -> list[dict]:
    """The distinct (agent, model, arm) lanes: `model` as the board names it
    (`leaderboard.clean_model_name`), `model_raw` as the run recorded it."""
    seen = sorted({(e.result.agent, clean_model_name(e.result.model), e.result.model or "",
                    e.result.condition or "") for e in eps})
    return [{"agent": a, "model": m, "model_raw": raw, "provider": _provider(raw),
             "condition": c} for a, m, raw, c in seen]


#: Board-row fields the manifest leaves out. Blocker recall resolves each defect's kind
#: and tier from the corpus of the checkout that BUILDS the view (`journey._defect_lookup`),
#: not from the episode summaries, so it is not a pure function of them: an
#: `--index-from` rebuild on another checkout would publish different numbers.
_BOARD_DROP = ("blocker_recall", "blocker_recall_ci", "blocker_found", "blocker_n")
#: What a per-app board row keeps: the chartable numbers and the row's identity.
_BY_APP_KEYS = ("agent", "model", "condition", "app", "heldout", "episodes",
                "excluded_episodes", "truncated", "completion", "completion_unscored",
                "false_alarm_rate", "false_alarm_ci", "false_alarm_k", "false_alarm_n",
                "catch_rate", "catch_ci", "catch_k", "catch_n", "cost_per_episode",
                "minutes_per_episode", "not_rescored")


def _board_block(eps: list[_Summary]) -> dict:
    """The run's journey board as data, in ranking order (`journey.ranking_key`): `now`
    over the rescored verdicts only (what the index's rescored column shows), `recorded`
    over the verdicts written at run time, and both per app (trimmed to `_BY_APP_KEYS`).
    Every `journey.summary` field except `_BOARD_DROP`, plus `not_rescored`: the row's
    journey episodes with no rescored verdict."""
    js = [e for e in eps if e.result.task_type == journey.TASK_TYPE]
    recorded = [e.result for e in js]
    rescored = [e.rescored_result for e in js if e.rescored_result is not None]
    out: dict[str, list[dict]] = {}
    for name, results, by_app in (("now", rescored, False), ("recorded", recorded, False),
                                  ("by_app_now", rescored, True),
                                  ("by_app_recorded", recorded, True)):
        missing: dict[tuple, int] = {}
        for e in js:
            if e.rescored_result is None:
                k = journey.row_key(e.result, by_app)
                missing[k] = missing.get(k, 0) + 1
        rows = []
        for row in journey.summary(results, by_app=by_app):
            key = viz.row_id(row)
            row = {k: v for k, v in row.items() if k not in _BOARD_DROP}
            row["not_rescored"] = missing.get(key, 0)
            rows.append({k: row.get(k) for k in _BY_APP_KEYS} if by_app else row)
        out[name] = rows
    return out


def _sorted_ids(v: Any) -> list[str] | None:
    return sorted(str(x) for x in v) if isinstance(v, (list, tuple)) else None


def _cases(eps: list[_Summary]) -> list[dict]:
    """One row per journey episode, sorted by (held, app, case, arm, trial, key). `*_rec`
    is the verdict recorded at run time, `*_now` the rescored one (None when the episode
    was not rescored: `rescored` false). `present` is the seeded defects under the "now"
    verdict; `fired` the seeded sites whose markers the device showed after the agent
    exited (None: not read). `excluded` is the exclusion reason ("" when kept)."""
    rows = []
    for e in eps:
        r = e.result
        if r.task_type != journey.TASK_TYPE:
            continue
        m0 = r.metrics or {}
        m1 = (e.rescored_result.metrics or {}) if e.rescored_result is not None else None
        now = m1 if m1 is not None else m0
        reports = now.get("reports") if m1 is not None else None
        steps = m0.get("hook_steps") if m0.get("hook_steps") is not None else m0.get("steps")
        rows.append({
            "key": e.key, "case_id": _case_of(r), "app_id": _app_id(r), "arm": e.arm,
            "held": e.held, "agent": r.agent, "model": clean_model_name(r.model),
            "condition": r.condition, "trial": r.trial, "started_at": r.started_at,
            "completed_rec": m0.get("completed"),
            "completed_now": m1.get("completed") if m1 is not None else None,
            "present": sorted(str(x) for x in now.get("bugs_present") or []),
            "found_rec": sorted(str(x) for x in m0.get("bugs_found") or []),
            "found_now": _sorted_ids(m1.get("bugs_found") or []) if m1 is not None else None,
            "fired": _sorted_ids(m0.get("fault_fired")),
            "reports_now": len(reports or []) if m1 is not None else None,
            "unmatched_grounded_now": (
                sum(1 for x in reports or [] if isinstance(x, dict)
                    and not x.get("matched") and x.get("grounded"))
                if m1 is not None else None),
            "fr_rec": m0.get("false_reports") or 0,
            "fr_now": (m1.get("false_reports") or 0) if m1 is not None else None,
            "truncated": bool(m0.get("truncated")), "steps": steps,
            "step_budget": m0.get("step_budget"),
            "excluded": exclusion_reason(now) or "",
            "cost_usd": m0.get("cost_usd") if isinstance(m0.get("cost_usd"), (int, float))
            else None,
            "cost_source": m0.get("cost_source"),
            "moved": _moved(m0, m1), "rescored": m1 is not None})
    rows.sort(key=lambda c: (c["held"], c["app_id"], c["case_id"], c["arm"], c["trial"],
                             c["key"]))
    return rows


def _measurement(eps: list[_Summary]) -> dict:
    """The additive keys of one `runs[]` entry (`MANIFEST` docstring)."""
    js = [e for e in eps if e.result.task_type == journey.TASK_TYPE]
    v = _versions(eps)
    return {
        "versions": v, "set_key": v["set_key"],
        "rescored_with": _rescored_with_block(eps),
        "moved": sum(1 for e in js if _moved(e.result.metrics or {},
                                             (e.rescored_result.metrics or {})
                                             if e.rescored_result is not None else None)),
        "present_changed": sum(
            1 for e in js if e.rescored_result is not None
            and sorted(map(str, (e.result.metrics or {}).get("bugs_present") or []))
            != sorted(map(str, (e.rescored_result.metrics or {}).get("bugs_present") or []))),
        "public": _split_counts(eps, False), "heldout": _split_counts(eps, True),
        "models": _models(eps), "board": _board_block(eps), "cases": _cases(eps)}


#: The manifest's `notes`: the journey board's standing captions, so a page built from
#: the manifest prints the harness's own words and cannot drift from them. The legend is
#: the run page's (`BOARD_RATES_LEGEND`): `board` carries no blocker recall
#: (`_BOARD_DROP`), so a legend defining it would describe a number nobody can show.
MANIFEST_NOTES = {"rates_legend": BOARD_RATES_LEGEND, "blocker_off_note": BLOCKER_OFF_NOTE,
                  "ranking_note": journey.RANKING_NOTE,
                  "mixed_corpus_note": journey.MIXED_CORPUS_NOTE,
                  "mixed_brief_note": journey.MIXED_BRIEF_NOTE}
#: The keys of a `runs[]` entry before QUA-2917 — what an experiment's per-run
#: breakdown (`experiment.runs`) keeps.
_RUN_KEYS = ("run_id", "started_at", "agents", "conditions", "arms", "episodes", "held_out",
             "completed", "scored", "state")


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
            **_measurement(eps),
        })
    from .checkpoint import package_version
    return {"format": MANIFEST_FORMAT, "title": title, "portable": portable,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "qualgentbench_version": package_version(),
            "episodes": sum(r["episodes"] for r in runs),
            "held_out": sum(r["held_out"] for r in runs), "withheld": withheld, "runs": runs,
            "notes": dict(MANIFEST_NOTES)}


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
        items.append(f"<li>{where}<code>{E(h['file'])}</code> · {_what(h)}</li>")
    return (f'<p class="banner">{E(WITHHELD_BANNER.format(n=len(hits)))}</p>'
            f"<details open><summary>{len(hits)} withheld file(s)</summary>"
            f"<ul>{''.join(items)}</ul></details>")


def _order_key(experiment: dict | None) -> Callable[[_Summary], tuple]:
    """How the index orders its rows: by run, case and trial — or, in an experiment
    view, in plan order (the cells as the state lists them; per cell its creation
    episode(s), then the grade runs in the grader's run order), from the summaries'
    own cells, so `build_index` orders them exactly as the build did."""
    if not experiment:
        return _order
    from .create import grader
    cells = {c.get("cell"): i for i, c in enumerate(experiment.get("cell_rows") or [])}
    roles = {f"{r}-{i}": n for n, (r, i) in enumerate(grader.PLAN_ORDER)}

    def key(s: _Summary) -> tuple:
        x = s.exp or {}
        return (cells.get(x.get("cell"), len(cells)), str(x.get("cell") or ""),
                0 if x.get("stage") == "author" else 1,
                roles.get(x.get("role"), len(roles)), str(x.get("role") or ""),
                int(x.get("attempt") or 0), *_order(s))
    return key


def _pages_links(pages: dict | None) -> str:
    """The index's links to the run view's extra pages (`run.json` `pages`)."""
    if not (pages or {}).get("create_board"):
        return ""
    data = (f' · <a href="{E(pages["create_board_json"])}">create.json</a>'
            if pages.get("create_board_json") else "")
    return (f'<p><a href="{E(pages["create_board"])}">CreateBench board</a>{data} — the '
            f'authored-case grades of these runs (QUA-2858).</p>')


def _write_index(out_dir: Path, summaries: list[_Summary], title: str, portable: bool,
                 states: dict[str, dict], *, gate: _Gate, stubs: list[_Stub] = (),
                 extra: list[dict] = (), pages: dict | None = None,
                 experiment: dict | None = None) -> tuple[dict[str, int], list[dict] | None]:
    """`index.html`, `manifest.json` and `style.css` from the summaries alone — the one
    renderer behind `build_view`, `build_experiment_view` and `build_index`, so they
    cannot differ. `pages` and `experiment` are `run.json`'s (the run view's extra
    pages; an experiment view's experiment). Returns the not-rescored counts by reason
    and the withheld list (None: not gated and nothing recorded): every summary's and
    stub's entries plus `extra` (run-level files), then the index's and the manifest's
    own if either matched."""
    summaries = sorted(summaries, key=_order_key(experiment))
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
    if experiment:
        # The board, the run state and the rescore note are a run's: an experiment shows
        # its verdict, its links and the per-cell table instead (QUA-2869).
        page = _index_html(rows, "", f"{title} — episode view",
                           any(s.held for s in summaries), {}, portable, "",
                           _withheld_html(hits, cases),
                           _experiment_links(experiment, summaries, len(by_run)),
                           rescore_note=False)
    else:
        measures = {r: _measurement(eps) for r, eps in by_run.items()}
        page = _index_html(rows, _summary_html(by_run, measures), f"{title} — episode view",
                           any(s.held for s in summaries), not_rescored, portable,
                           _state_html({r: states.get(r) or {} for r in by_run})
                           + "\n" + _versions_html(by_run, measures),
                           _withheld_html(hits, cases), _pages_links(pages),
                           charts=_charts_html(by_run, measures))
    index = out_dir / "index.html"
    if gate.write(index, page, None) is not None:
        hits = _dedupe(hits + gate.hits[-1:])
        gate.replace(index, _stub_index(hits))
    withheld = hits if (gate.enabled or hits) else None
    manifest = out_dir / MANIFEST
    doc = (_experiment_manifest(by_run, title, portable, states, withheld, experiment)
           if experiment else _manifest(by_run, title, portable, states, withheld))
    if gate.write(manifest, _dumps(doc, _markers(withheld), indent=2), None) is not None:
        # Only the view's own fields are left: the run entries and title matched.
        withheld = _dedupe((withheld or []) + gate.hits[-1:])
        gate.replace(manifest, _dumps(
            {**_manifest({}, "", portable, {}, withheld), "episodes": None, "held_out": None,
             **({"kind": "experiment"} if experiment else {})},
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


def _episode_dir(runs_dir: Path, r: RunResult) -> Path | None:
    d = resolve_artifact_dir(runs_dir, r)
    return d if d is not None and d.is_dir() else None


def _write_episodes(runs_dir: Path, results: list[RunResult], out_dir: Path, gate: _Gate, *,
                    rescore: bool, tasks_by_id: dict,
                    rescore_off: str = "not rescored (--no-rescore)",
                    exps: list[dict | None] | None = None,
                    rescored_with: dict | None = None,
                    progress: Callable[[str], None] | None = None
                    ) -> tuple[ViewResult, list[_Summary], list[_Stub]]:
    """One page, assets dir and summary per result, in the order given, under
    `<out_dir>/ep/`, each keyed by `episode_key` and written through `gate`; the result
    counts, the summaries and the stub summaries. `exps[i]` is result i's experiment
    cell, if any; `rescored_with` the corpus stamp every rescored summary carries (None:
    the rescore did not use the default corpus). The one episode writer behind
    `build_view` and `build_experiment_view`."""
    _prepare_out(out_dir)
    ep_root = out_dir / "ep"
    portable = gate.enabled
    res = ViewResult(out_dir=out_dir, index=out_dir / "index.html", portable=portable)
    summaries: list[_Summary] = []
    stubs: list[_Stub] = []
    used: set[str] = set()
    for n, r in enumerate(results, 1):
        d = _episode_dir(runs_dir, r)
        key = episode_key(r, d)
        if key in used:  # two results naming one episode dir: keep both pages
            logger.warning("view: episode key %s is shared by two results (%s)", key, r.task_id)
            key = next(f"{key}-{i}" for i in range(2, len(results) + 2)
                       if f"{key}-{i}" not in used)
        used.add(key)
        status, m1, reason1, rescored_result = _rescore_one(d, r, tasks_by_id, rescore,
                                                            rescore_off)
        if m1 is not None:
            res.rescored += 1
        exp = _exp_of(exps[n - 1]) if exps and exps[n - 1] else None
        ep = _Episode(eid=key, result=r, dir=d, recorded=dict(r.metrics or {}), rescored=m1,
                      rescored_reason=reason1, rescore_status=status,
                      rescored_result=rescored_result, held=_is_heldout(r), arm=_arm(r),
                      exp=exp)
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
        # Images carry the transcript's mtime: unchanged, they are not re-uploaded.
        tl_html, shots, calls = _timeline_html(
            entries, ep_root / key, raw_href, gate, key,
            tr_path.stat().st_mtime if has_transcript else None)
        page_path, summary_path = ep_root / f"{key}.html", ep_root / f"{key}.json"
        page = _episode_page(ep, ep_root, raw_href, tl_html, shots, calls, copies)
        page_hit = gate.check(page.encode("utf-8"), page_path, key)
        summary = _Summary(key=key, run_id=r.run_id or "", row=_row(ep, shots), held=ep.held,
                           arm=ep.arm, rescore_status=status, result=r,
                           rescored_result=rescored_result,
                           withheld=[{"file": h["file"], "marker": h["marker"]}
                                     for h in gate.hits[first_hit:]], exp=exp,
                           rescored_with=(dict(rescored_with) if rescored_with
                                          and rescored_result is not None else None))
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
    return res, summaries, stubs


def _write_run_state(out_dir: Path, gate: _Gate, title: str, portable: bool,
                     states: dict[str, dict], **more: Any) -> list[dict]:
    """`run.json` through the gate (`more`: the optional `pages` / `experiment` keys,
    left out when empty, so a plain run's run.json is what it always was); [] or the
    run-level hit."""
    doc = {"format": RUN_STATE_FORMAT, "title": title, "portable": portable, "runs": states,
           **{k: v for k, v in more.items() if v}}
    hit = gate.write(out_dir / RUN_STATE, json.dumps(doc, indent=2), None)
    return [hit] if hit else []


def _refuse_private(out_dir: Path, hits: list[dict]) -> None:
    """Fail the build — no `manifest.json` — when the gate found an episode's private
    text anywhere (QUA-2869). The files were withheld; the folder still must not be
    published, because private text reaching a transcript or a page is a bug to find."""
    private = [h for h in hits if is_private_hit(h)]
    if not private:
        return
    (out_dir / MANIFEST).unlink(missing_ok=True)
    listed = "\n".join(f"  {h['file']}: {h['marker']}" for h in private[:20])
    raise ViewError(
        f"refusing to finish the portable view at {out_dir}: {len(private)} file(s) carry "
        f"text from an episode's {PRIVATE_DIR}/ folder (the creation arm's private developer "
        f"instructions), which must never leave the runs tree:\n{listed}\n  They were "
        f"withheld, and no manifest.json was written, so the folder cannot be published. "
        f"Delete it, and find how that text reached a transcript or a page.")


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
    the same episode. `run.json` records the run state the index shows. When the runs
    hold CreateBench grades, `create.html` is their board and `create.json` its data
    (`run.json` `pages`).

    `tasks_by_id` is the corpus the rescore scores against (default: the current one,
    `rescore.journey_tasks_by_id`, held-out included when the split is configured). Only
    the default stamps each rescored summary's `rescored_with` (`corpus.stamp()`)."""
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

    journey_run = any(r.task_type == journey.TASK_TYPE for r in results)
    # The rescore's corpus stamp, only when it scores against the DEFAULT corpus (the
    # checkout's own, held-out included when configured): a caller-supplied corpus has
    # no version to name. A pure function of the checkout — no clock, no package
    # version — so every build from one checkout writes the same summaries (QUA-2917).
    stamp = corpus.stamp() if (rescore and tasks_by_id is None and journey_run) else None
    if rescore and tasks_by_id is None and journey_run:
        from .rescore import journey_tasks_by_id
        tasks_by_id = journey_tasks_by_id()
    gate = _Gate(out_dir, enabled=portable,
                 private=_PrivateText(filter(None, (_episode_dir(runs_dir, r)
                                                    for r in results))) if portable else None)
    res, summaries, stubs = _write_episodes(runs_dir, results, out_dir, gate, rescore=rescore,
                                            tasks_by_id=tasks_by_id or {}, progress=progress,
                                            rescored_with=stamp)

    title = (_title(run_ids) if run_ids else f"All runs under {runs_dir}")
    res.create_board, res.create_board_json = _write_create_board(runs_dir, run_ids, out_dir,
                                                                   title, gate)
    pages = ({"create_board": res.create_board.name,
              **({"create_board_json": res.create_board_json.name}
                 if res.create_board_json else {})} if res.create_board else None)
    states = {rid: run_state(runs_dir, rid)
              for rid in sorted({s.run_id for s in [*summaries, *stubs]})}
    extra = _write_run_state(out_dir, gate, title, portable, states, pages=pages)
    if portable:
        extra += _backstop(out_dir, gate)
    _refuse_private(out_dir, gate.hits + extra)
    res.not_rescored, res.withheld = _write_index(
        out_dir, summaries, title, portable, states, gate=gate, stubs=stubs, extra=extra,
        pages=pages)
    _refuse_private(out_dir, res.withheld or [])
    return res


def _write_create_board(runs_dir: Path, run_ids: list[str], out_dir: Path, title: str,
                        gate: _Gate, experiment: str | None = None
                        ) -> tuple[Path | None, Path | None]:
    """`create.html` and `create.json`: the CreateBench board (`create/board.py`) over
    these runs' grade manifests — or, with `experiment`, that experiment's cells plus
    the reference baseline of its runner, as `show --mode create --experiment` selects
    them — standalone (no link into the runs tree, so `--portable` carries it to the
    hosted viewer), written through the gate. `create.json` is `board.build_board`'s
    dict, the page's charts' and tables' data (QUA-2922). The readiness gate is shown as
    a banner, not enforced: a view is a reading aid; `show --mode create` is where the
    gate refuses. Each is None when there are no grades, or the gate withheld it."""
    from .create import board as _cboard
    if not _cboard.load_grades(runs_dir, run_ids or None):
        return None, None
    b = _cboard.board_for(runs_dir, run_ids=run_ids or None, experiment=experiment,
                          include_smoke=experiment is None,
                          title=f"{title} — CreateBench board")
    page, data = out_dir / "create.html", out_dir / "create.json"
    page_out = None if gate.write(page, _cboard.render_html(b), None) is not None else page
    data_out = (None if gate.write(data, json.dumps(b, indent=2, default=str), None)
                is not None else data)
    return page_out, data_out


def _backstop(out_dir: Path, gate: _Gate) -> list[dict]:
    """Re-scan a portable folder once it is written (QUA-2847): a file that reached it
    without passing the gate — a path this module forgot to route through it — is
    removed and reported as withheld like any other hit. Empty on a correct build."""
    late = [h for h in scan_view(out_dir, gate.private) if h not in gate.hits]
    for h in late:
        (out_dir / h["file"]).unlink(missing_ok=True)
        logger.warning("view: backstop withheld %s (marker %r) — it was written "
                       "past the gate", h["file"], h["marker"])
    return late


# ── an experiment (QUA-2869) ───────────────────────────────────────────────────

def experiment_out(runs_dir: Path, name: str) -> Path:
    """`<runs>/_runs/_create/ab/<name>/view/`: beside the experiment's own state."""
    from .create import ab
    return ab.state_path(runs_dir, name).with_suffix("") / VIEW_DIRNAME


#: Grade runs are not rescored in an experiment view: each is a run of an AUTHORED case
#: (not a corpus case), and the cell's grade manifest is its verdict.
EXPERIMENT_RESCORE_OFF = "not rescored (CreateBench experiment: the grade manifest is the verdict)"
#: The `experiment` block's keys in `manifest.json` (plus `runs`, the per-run breakdown).
#: `environment` is the A/B state's registered environment (`ab.environment`: corpus
#: version, the frozen runner's fingerprint, the creation-brief version) and `arm_pins`
#: each arm's `{qualgent_mcp, devloop, template_sha256}` (QUA-2917).
_EXPERIMENT_MANIFEST_KEYS = ("name", "run_id", "registered_at", "prediction", "prediction_sha",
                             "verdict", "why", "cells", "spent_usd", "missing_episodes",
                             "pages", "environment", "arm_pins")


def _axis(v: Any) -> str:
    return {True: '<span class="y">yes</span>', False: '<span class="n">no</span>'}.get(
        v, '<span class="dim">—</span>')


def _cells_html(cells: list[dict], summaries: list[_Summary]) -> str:
    """The per-cell table: status, creation outcome, the grade's axes, and a link to
    every episode of the cell (its creation episode(s), then each grade run)."""
    links: dict[str, list[str]] = {}
    for s in summaries:
        if not s.exp:
            continue
        x = s.exp
        label = "author" if x["stage"] == "author" else x["role"]
        if x["attempt"] > 1:
            label += f" (attempt {x['attempt']})"
        cls = ' class="dim"' if x.get("excluded") else ""
        tip = f' title="excluded: {E(x["excluded"])}"' if x.get("excluded") else ""
        links.setdefault(x["cell"], []).append(
            f'<a href="ep/{E(s.key)}.html"{cls}{tip}>{E(label)}</a>')
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


#: The keys of an A/B report's expectation and precondition that `run.json` keeps for
#: the experiment index's checklist (X3); the per-brief rows are X1's.
_EXPECTATION_KEYS = ("expectation", "axis", "direction", "scope", "stratum", "test", "alpha",
                     "outcome", "why", "a", "b", "p_value", "sign")
_PRECONDITION_KEYS = ("precondition", "kind", "axis", "stratum", "met", "why", "a", "b")


def _report_fields(rep: dict) -> dict:
    """What the experiment index charts (X1-X3, QUA-2922), copied from the A/B report
    (`ab.report`) into `run.json`'s experiment block so `--index-from` redraws them:
    the arms in A, B order, the per-brief and per-group power, the uptake check, and
    the pre-registered expectations and preconditions with their outcomes."""
    v = rep.get("verdict") or {}
    return {"arm_order": list(rep.get("arms") or {}),
            "brief_power": v.get("brief_power") or {},
            "by_group": v.get("by_group") or {},
            "uptake": v.get("uptake"),
            "expectations": [{k: e.get(k) for k in _EXPECTATION_KEYS}
                             for e in v.get("expectations") or []],
            "preconditions": [{k: pc.get(k) for k in _PRECONDITION_KEYS}
                              for pc in v.get("preconditions") or []]}


def _ab_fields(prefix: str, d: dict | None) -> dict:
    """An A/B report rate (`{k, n, p, ci}`) as the `{prefix}_rate/_ci/_k/_n` fields
    `viz` reads; None stays None (drawn "n/a", never 0%)."""
    d = d or {}
    return {f"{prefix}_rate": d.get("p"), f"{prefix}_ci": d.get("ci"),
            f"{prefix}_k": d.get("k") or 0, f"{prefix}_n": d.get("n") or 0}


def _ab_rate(d: dict | None) -> str:
    return "—" if not d else fmt_pct_ci(d.get("p"), d.get("ci"), d.get("k"), d.get("n"))


def _arm_label(name: str, pins: dict | None) -> str:
    """An arm as the X1 legend names it: its name and its pinned SHAs (short)."""
    pins = pins or {}
    parts = [f"{k} {str(pins[k])[:7]}" for k in ("qualgent_mcp", "devloop") if pins.get(k)]
    return f"arm {name}" + (f" ({', '.join(parts)})" if parts else "")


def _pooled_note(x: dict, group: str) -> str:
    """The pooled row's tests for a detection group: the registered one-sided Fisher
    exact test and brief-level sign test on its power when the prediction has them, and
    the brief tally (B below A on k of the judged briefs) always."""
    out = []
    for e in x.get("expectations") or []:
        if e.get("axis") != "power" or e.get("stratum") != group or e.get("p_value") is None:
            continue
        if e.get("test") == "fisher":
            out.append(f"Fisher one-sided p = {e['p_value']:.3g} ({e.get('outcome')})")
        elif e.get("test") == "sign":
            sign = e.get("sign") or {}
            out.append(f"sign test p = {e['p_value']:.3g} on {sign.get('for', 0)}/"
                       f"{sign.get('judged', 0)} judged brief(s) ({e.get('outcome')})")
    bp = (x.get("brief_power") or {}).get(group) or {}
    out.append(f"B below A on {bp.get('b_below_a', 0)}/{bp.get('judged', 0)} brief(s)")
    return " · ".join(out)


def _x1_html(x: dict) -> str:
    """X1: per-brief power, arm A against arm B, by detection group, with the pooled
    group row; and its table twin."""
    bp, by_group = x.get("brief_power") or {}, x.get("by_group") or {}
    if not bp:
        return ""
    order = x.get("arm_order") or sorted(x.get("arm_pins") or {})
    pins = x.get("arm_pins") or {}
    names = {f"s{i + 1}": _arm_label(a, pins.get(a)) for i, a in enumerate(order[:2])}
    groups, rows = [], []
    for g, d in bp.items():
        pooled = (by_group.get(g) or {}).get("power") or {}
        groups.append({"title": f"{g} briefs",
                       "rows": [{"label": r["brief"], "flag": r.get("b_below_a") is True,
                                 **_ab_fields("a", r.get("a")), **_ab_fields("b", r.get("b"))}
                                for r in d.get("briefs") or []],
                       "pooled": {"label": f"pooled · {g}", **_ab_fields("a", pooled.get("a")),
                                  **_ab_fields("b", pooled.get("b"))},
                       "note": _pooled_note(x, g)})
        for r in d.get("briefs") or []:
            below = {True: "yes", False: "no"}.get(r.get("b_below_a"), "—")
            rows.append(_tr([E(g), E(r["brief"]), E(_ab_rate(r.get("a"))),
                             E(_ab_rate(r.get("b"))), below]))
        rows.append(_tr([E(g), "<b>pooled</b>", E(_ab_rate(pooled.get("a"))),
                         E(_ab_rate(pooled.get("b"))), E(_pooled_note(x, g))], "mv"))
    chart = viz.forest(groups, names, title="power (target-only run failed on the target)")
    a, b = (order + ["A", "B"])[:2]
    return (_figure(chart, f"X1. Power per brief, arm {E(a)} against arm {E(b)}, Wilson 95% "
                           f"intervals, by detection group, with the pooled group row and its "
                           f"registered tests. {viz.FLAG} marks a brief where B fell below A. "
                           f"Walk-group power mostly measures reaching the feature and is "
                           f"never pooled into the headline.", "x1")
            + _twin(["group", "brief", f"arm {a}", f"arm {b}", "B below A"], rows, "X1"))


def _x2_html(x: dict) -> str:
    """X2: the uptake check, one bar per arm and detection group; and its table twin."""
    up = x.get("uptake") or {}
    if not up.get("arms"):
        return ""
    order = [a for a in (x.get("arm_order") or []) if a in up["arms"]] + sorted(
        a for a in up["arms"] if a not in (x.get("arm_order") or []))
    bars, rows = [], []
    for arm in order:
        for g, d in (up["arms"][arm] or {}).items():
            bars.append({"label": f"arm {arm} · {g}", "group": arm, **_ab_fields("up", d)})
            rows.append(_tr([E(arm), E(g), E(_ab_rate(d))]))
    chart = viz.bars(bars, "up", f"uptake of {up.get('rule')}")
    registered = any(pc.get("kind") == "uptake" for pc in x.get("preconditions") or [])
    return (_figure(chart, f"X2. Authored cases that took {E(str(up.get('rule')))}, k/n per "
                           f"arm and detection group"
                           + ("" if registered
                              else " (a diagnostic: no uptake precondition is registered)")
                           + ".", "x2")
            + _twin(["arm", "group", "uptake"], rows, "X2"))


#: X3's status marks: a glyph and a word, so the status never rests on colour alone.
_CHECK = {"MET": ("good", "✓"), "NOT MET": ("crit", "✗")}


def _check(status: str) -> str:
    cls, glyph = _CHECK.get(status, ("warn", "○"))
    return f'<span class="chk {cls}"><span class="g">{glyph}</span> {E(status)}</span>'


def _x3_html(x: dict) -> str:
    """X3: the pre-registered preconditions and expectations as a checklist (a table:
    status, what, why, A, B, p)."""
    pcs, exps = x.get("preconditions") or [], x.get("expectations") or []
    if not pcs and not exps:
        return ""
    rows = []
    for pc in pcs:
        rows.append(f"<tr><td>{_check('MET' if pc.get('met') else 'NOT MET')}</td>"
                    f"<td>precondition: {E(str(pc.get('precondition')))}</td>"
                    f"<td>{E(str(pc.get('why') or ''))}</td><td>{E(_ab_rate(pc.get('a')))}</td>"
                    f"<td>{E(_ab_rate(pc.get('b')))}</td><td>—</td></tr>")
    for e in exps:
        p = e.get("p_value")
        rows.append(f"<tr><td>{_check(str(e.get('outcome')))}</td>"
                    f"<td>{E(str(e.get('expectation')))}</td><td>{E(str(e.get('why') or ''))}</td>"
                    f"<td>{E(_ab_rate(e.get('a')))}</td><td>{E(_ab_rate(e.get('b')))}</td>"
                    f"<td>{'—' if p is None else f'{p:.3g}'}</td></tr>")
    return (f'<div class="tablewrap" id="x3"><table class="idx"><thead><tr><th>status</th>'
            f'<th>pre-registered</th><th>why</th><th>arm A</th><th>arm B</th><th>p</th></tr>'
            f'</thead><tbody>{"".join(rows)}</tbody></table></div>'
            f'<p class="dim">X3. Every precondition and expectation the prediction registered, '
            f'with its outcome. A per-brief expectation\'s rows are in X1.</p>')


def _report_charts(x: dict) -> str:
    """X1-X3 under one heading; empty for a run.json written before QUA-2922."""
    body = _x1_html(x) + _x2_html(x) + _x3_html(x)
    return f'<h2 id="charts">Report charts</h2>{body}' if body else ""

def _experiment_links(x: dict, summaries: list[_Summary], n_runs: int) -> str:
    """The experiment index's head: the verdict, the report's numbers, the links to the
    report and the board, and the per-cell table — from `run.json`'s `experiment`."""
    board = (x.get("pages") or {}).get("board")
    board_json = (x.get("pages") or {}).get("board_json")
    miss = int(x.get("missing_episodes") or 0)
    miss_html = (f'<p class="warn">{miss} episode(s) the state names are not on disk '
                 f'(or have no readable result.json); they are not in this view.</p>'
                 if miss else "")
    return (f'<h2>Experiment</h2><p><b>VERDICT: {E(str(x.get("verdict")))}</b> — '
            f'{E(str(x.get("why") or ""))}</p>'
            f'<p class="dim">prediction {E(str(x.get("prediction")))} · '
            f'{x.get("briefs")} brief(s) · arms {E(", ".join(x.get("arms") or []))} · driver '
            f'run {E(str(x.get("run_id")))} · {n_runs} run(s) · {_money(x.get("spent_usd"))}</p>'
            f'<p><a href="report.html">A/B report</a> · <a href="report.json">report.json</a>'
            + (f' · <a href="{E(board)}">CreateBench board</a>' if board else "")
            + (f' · <a href="{E(board_json)}">create.json</a>' if board_json else "")
            + f' · <a href="#cells">cells</a></p>{miss_html}'
            + _report_charts(x)
            + _cells_html(x.get("cell_rows") or [], summaries))


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


def _experiment_state(cells: dict[str, int]) -> dict:
    """The experiment's progress as a run-state block: its cells are its units, done
    once graded, skipped or faulted (`ab.DONE`); no segment, no stop."""
    from .create import ab
    planned = sum(int(n) for n in cells.values())
    done = sum(int(n) for st, n in cells.items() if st in ab.DONE)
    return {"segment": None, "units_planned": planned, "units_done": done,
            "units_owed": planned - done, "stopped": None, "complete": planned == done}


def _experiment_manifest(by_run: dict[str, list[_Summary]], title: str, portable: bool,
                         states: dict[str, dict], withheld: list[dict] | None,
                         x: dict) -> dict:
    """`manifest.json` for an experiment, format 2: `runs` = ONE entry for the whole
    experiment (its `run_id` the experiment name, its `state` the cells' progress),
    plus `kind` and `experiment` (the per-run breakdown, the verdict, the cells, the
    cost and the linked pages). In that entry `completed` / `scored` count CELLS
    (graded / planned): an episode-level `completed` would mix creation and grade runs
    into a number that means nothing.

    The entry also carries the additive keys of a run entry (QUA-2917): `versions` and
    `set_key` (`c-<corpus>-g<grader>-cb<create brief>`, from the registered
    environment), `rescored_with`, `moved`, `present_changed`, `public`, `heldout` and
    `models` over every episode, and an empty `board` and `cases` (an experiment's
    verdict is its cells, not a journey board). The per-run breakdown keeps the
    pre-QUA-2917 keys only."""
    m = _manifest(by_run, title, portable, {}, withheld)
    per_run = [{k: r[k] for k in _RUN_KEYS} for r in m["runs"]]
    cells = x.get("cells") or {}
    everything = [e for eps in by_run.values() for e in eps]
    extra = _measurement(everything)
    extra["set_key"] = extra["versions"]["set_key"] = _experiment_set_key(x.get("environment"))
    extra["board"] = {"now": [], "recorded": [], "by_app_now": [], "by_app_recorded": []}
    extra["cases"] = []
    m["kind"] = "experiment"
    m["experiment"] = {**{k: x.get(k) for k in _EXPERIMENT_MANIFEST_KEYS}, "runs": per_run}
    m["runs"] = [{
        "run_id": x["name"],
        "started_at": min((r["started_at"] for r in per_run if r["started_at"]), default=None),
        "agents": sorted({a for r in per_run for a in r["agents"]}),
        "conditions": sorted({c for r in per_run for c in r["conditions"]}),
        "arms": sorted(x.get("arms") or []),
        "episodes": sum(r["episodes"] for r in per_run),
        "held_out": sum(r["held_out"] for r in per_run),
        "completed": int(cells.get("graded", 0)),
        "scored": sum(int(n) for n in cells.values()),
        "state": {**_UNKNOWN_STATE, **(states.get(x["name"]) or {})},
        **extra}]
    return m


def build_experiment_view(runs_dir: Path | str, experiment: str,
                          out: Path | str | None = None, *, allow_outside_runs: bool = False,
                          portable: bool = False,
                          progress: Callable[[str], None] | None = None) -> ViewResult:
    """Write the view of one CreateBench A/B experiment (module docstring) and return
    where it went. Reads the runs tree only; writes only under `out` (default
    `experiment_out`). A format-2 view: the episodes go through the same writer and
    gate as `build_view`, and `run.json` carries the experiment, so `build_index`
    rebuilds the same index and manifest."""
    from .create import ab
    runs_dir = Path(runs_dir).expanduser()
    if not runs_dir.is_dir():
        raise ViewError(f"runs dir {runs_dir} does not exist")
    ab_state = ab.load_state(ab.state_path(runs_dir, experiment))
    if ab_state is None:
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
    gate = _Gate(out_dir, enabled=portable,
                 private=_PrivateText(filter(None, (_episode_dir(runs_dir, r)
                                                    for r in results))) if portable else None)
    res, summaries, stubs = _write_episodes(
        runs_dir, results, out_dir, gate, rescore=False, tasks_by_id={},
        rescore_off=EXPERIMENT_RESCORE_OFF, exps=exps, progress=progress)
    res.missing = missing
    title = f"Experiment {experiment}"
    rep = ab.report(runs_dir, experiment)
    extra: list[dict] = []
    if hit := gate.write(out_dir / "report.json", json.dumps(rep, indent=2, default=str), None):
        extra.append(hit)
    res.create_board, res.create_board_json = _write_create_board(
        runs_dir, [], out_dir, title, gate, experiment=experiment)
    res.report = out_dir / "report.html"
    if hit := gate.write(res.report, _report_html(experiment, ab.render_report(rep),
                                                  res.create_board is not None), None):
        extra.append(hit)
    v = rep["verdict"]
    x = {"name": experiment, "run_id": rep["run_id"], "registered_at": rep["registered_at"],
         "prediction": v.get("prediction"), "prediction_sha": v.get("prediction_sha"),
         "verdict": v.get("verdict"), "why": v.get("why"), "cells": rep["cells"],
         "spent_usd": rep["spent"]["total"], "missing_episodes": len(missing),
         "pages": {"index": "index.html", "report": "report.html",
                   "report_json": "report.json",
                   "board": res.create_board.name if res.create_board else None,
                   "board_json": (res.create_board_json.name if res.create_board_json
                                  else None)},
         "arms": sorted(rep["arms"]), "briefs": len(rep["briefs"]),
         "cell_rows": ab.cell_summaries(runs_dir, experiment),
         "environment": ab_state.get("environment"),
         "arm_pins": {a: {k: (p or {}).get(k)
                          for k in ("qualgent_mcp", "devloop", "template_sha256")}
                      for a, p in sorted(rep["arms"].items())},
         **_report_fields(rep)}
    states = {experiment: _experiment_state(rep["cells"])}
    extra += _write_run_state(out_dir, gate, title, portable, states, experiment=x)
    if portable:
        extra += _backstop(out_dir, gate)
    _refuse_private(out_dir, gate.hits + extra)
    res.not_rescored, res.withheld = _write_index(
        out_dir, summaries, title, portable, states, gate=gate, stubs=stubs, extra=extra,
        experiment=x)
    _refuse_private(out_dir, res.withheld or [])
    return res


# ── rebuilding the index ───────────────────────────────────────────────────────

def build_index(view_dir: Path | str) -> ViewResult:
    """Rebuild `index.html`, `manifest.json` and `style.css` in `view_dir` from its
    `ep/*.json` summaries and `run.json` alone — no runs tree (`view --index-from`).

    For a folder merged from several views of one run (machines, segments): pages and
    summaries are keyed by `episode_key`, so a merge is a plain copy of every `ep/`, and
    the index lists every episode once. The run state is `run.json`'s as it stands
    (the publisher keeps the latest segment's); without one, the title is derived from
    the run ids and the state is unknown. `run.json`'s `pages` and `experiment` (an
    experiment view, QUA-2869) are rendered as the build rendered them. Writes only
    those three files and the view marker; nothing under `ep/` is touched.

    Withheld entries come back from the summaries and stub summaries, and a portable
    folder is re-scanned (`scan_view`): a merged-in file that matches a marker is
    reported as withheld (its summary, if it is one, is not read), so the rebuilt
    `manifest.json` and the CLI's `EXIT_WITHHELD` cover everything the folder holds.
    The private-text check needs the episodes' `private/` folders, which a view never
    holds, so it is not repeated here: a build that found private text wrote no
    manifest, and its summaries list those files as withheld."""
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
    pages = state_doc.get("pages") if isinstance(state_doc.get("pages"), dict) else None
    experiment = (state_doc.get("experiment")
                  if isinstance(state_doc.get("experiment"), dict) else None)
    if experiment is not None and not experiment.get("name"):
        raise ViewError(f"unreadable run state {view_dir / RUN_STATE}: its experiment has "
                        f"no name")
    (view_dir / MARKER).write_text("written by `qualgent-bench view`; safe to delete\n")
    res = ViewResult(out_dir=view_dir, index=view_dir / "index.html", portable=portable,
                     episodes=len(summaries),
                     images=sum(int(s.row.get("shots") or 0) for s in summaries),
                     rescored=sum(s.rescored_result is not None for s in summaries))
    res.not_rescored, res.withheld = _write_index(
        view_dir, summaries, title, portable, states,
        gate=_Gate(view_dir, enabled=portable), stubs=stubs, extra=found, pages=pages,
        experiment=experiment)
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
