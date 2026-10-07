"""`qualgent-bench view --experiment` — one CreateBench A/B experiment as one site (QUA-2869).

A small experiment driven offline through the REAL driver and grader (`test_create_ab`'s
simulated author and runner), with episode dirs on disk the way the live stages leave
them: a creation episode per cell (transcript, result.json, authored case and the arm's
`private/developer_instructions.md`) in its own run, and five grade runs per cell in
the driver's run. Pinned:

* the index links the A/B report, the create board and every episode, with each row's
  cell and stage;
* a portable export stands alone, never carries an episode's private folder, and fails
  (no manifest) when private text reached a page; a credential marker goes through the
  same gate as a run view's (QUA-2841): the file is withheld, listed in the manifest, and
  the CLI exits 65;
* the view is format 2 (QUA-2840): stable episode keys, `ep/<key>.json` summaries and a
  `run.json` that carries the experiment, so `--index-from` rebuilds the same index and
  manifest; the manifest lists the experiment as ONE run for the bench viewer's front
  page and passes the publisher's format-2 checks;
* the runs tree is never written.

App and case names come from the packaged corpus; the private text is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_create_ab import BRIEFS, SimAuthor, SimRunner, _drive, _spec

from qualgentbench import cli, view
from qualgentbench.create import ab, grader
from qualgentbench.create import runner as create_runner
from qualgentbench.result import RunResult

NAME = "pc-view"
#: A private sentence (synthetic): what the arm's developer instructions hold.
PRIVATE = ("Before authoring anything the creator walks the whole feature twice and writes down "
           "every stable anchor it saw in the order the screens appeared to it so that the "
           "case can be replayed by a different agent without any memory of this session at all "
           "and the reviewer can trace each step back to the screen that justified it")
#: A short synthetic phrase the private text shares with what agents legitimately see.
SHARED = "every stable anchor it saw in the order the screens appeared"


def _codex(text: str) -> str:
    return "\n".join(json.dumps(x) for x in [
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}])


def _result(runs: Path, ep: Path, *, task_id: str, task_type: str, run_id: str,
            metrics: dict) -> None:
    r = {"task_id": task_id, "task_version": "v", "task_type": task_type,
         "agent": "codex-cli", "model": "gpt-6-astra", "condition": "mcp", "trial": 1,
         "passed": True, "score": 1.0, "started_at": "2026-10-01T00:00:00+00:00",
         "ended_at": "2026-10-01T00:01:00+00:00", "wall_time_sec": 60.0, "exit_code": 0,
         "artifact_dir": str(ep.relative_to(runs)), "run_id": run_id, "metrics": metrics,
         "provenance": {}}
    RunResult.model_validate(r)
    (ep / "result.json").write_text(json.dumps(r, indent=2))


class EpAuthor(SimAuthor):
    """SimAuthor, plus what a creation episode leaves: its own run's result.json, a
    transcript, the brief and the arm's private developer instructions."""

    async def author(self, cell, arm):
        out = await super().author(cell, arm)
        d = self._dir(cell)
        (d / "agent").mkdir(exist_ok=True)
        (d / "agent" / "transcript.txt").write_text(_codex(f"Authored {cell.case_id}. {SHARED}"))
        (d / "instruction_sent.md").write_text(f"Feature brief for {cell.case_id}.")
        (d / "private").mkdir(exist_ok=True)
        (d / "private" / "developer_instructions.md").write_text(f"# Creator\n\n{PRIVATE}\n")
        _result(self.runs, d, task_id=create_runner.task_id(cell.case_id),
                task_type=create_runner.TASK_TYPE, run_id=f"create-{cell.key}",
                metrics={"app_id": "ankidroid", "case_id": cell.case_id,
                         "heldout": cell.arm == "B" and cell.trial == 1})
        return out


class EpRunner(SimRunner):
    """SimRunner, plus the five grade-run episode dirs, named in the plan's attempts."""

    async def grade(self, plan, *, run_id, manifest_name, extra):
        for run in plan.runs:
            d = self.runs / plan.grade_id / f"{manifest_name}.{run.key}"
            (d / "agent").mkdir(parents=True, exist_ok=True)
            (d / "agent" / "transcript.txt").write_text(_codex(f"Ran {run.key}. {SHARED}"))
            _result(self.runs, d, task_id=f"{plan.grade_id}~{run.key}",
                    task_type=grader.TASK_TYPE, run_id=run_id,
                    metrics={"app_id": "ankidroid", "case_id": plan.case_id})
            run.attempts = [{"episode_dir": str(d.relative_to(self.runs)), "excluded": ""}]
        return await super().grade(plan, run_id=run_id, manifest_name=manifest_name,
                                   extra=extra)


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    _drive(runs, _spec(name=NAME, trials=1), EpAuthor(runs, {"A": "honest", "B": "harmful"}),
           EpRunner(runs))
    return runs


def _snapshot(root: Path) -> dict[str, str]:
    """Every file of the runs tree but the view's own output folder."""
    own = view.experiment_out(root, NAME)
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_relative_to(own)}


def _rows(index: Path) -> list[dict]:
    m = re.search(r'<script type="application/json" id="rows">(.*?)</script>',
                  index.read_text(), re.S)
    return json.loads(m.group(1).replace("<\\/", "</"))


def _hrefs(page: str) -> list[str]:
    return re.findall(r'(?:href|src)="([^"#]+)"', page)


CELLS = 2 * len(BRIEFS)            # two arms × the briefs × one trial
EPISODES = CELLS * (1 + len(grader.PLAN_ORDER))


# ── the read path ──────────────────────────────────────────────────────────────

def test_experiment_episodes_names_each_cells_creation_then_its_grade_runs(runs):
    eps = ab.experiment_episodes(runs, NAME)
    assert len(eps) == EPISODES
    first = [e for e in eps if e["cell"] == eps[0]["cell"]]
    assert [e["stage"] for e in first] == ["author"] + ["grade"] * len(grader.PLAN_ORDER)
    assert [e["role"] for e in first[1:]] == [f"{r}-{i}" for r, i in grader.PLAN_ORDER]
    assert all((runs / e["episode_dir"] / "result.json").is_file() for e in eps)
    cells = ab.cell_summaries(runs, NAME)
    assert len(cells) == CELLS and all(c["status"] == ab.GRADED for c in cells)
    assert {c["arm"] for c in cells} == {"A", "B"}
    assert all("power" in c["axes"] for c in cells)
    with pytest.raises(FileNotFoundError):
        ab.experiment_episodes(runs, "nope")


# ── the view ───────────────────────────────────────────────────────────────────

def test_index_links_the_report_the_board_and_every_episode(runs):
    before = _snapshot(runs)
    res = view.build_experiment_view(runs, NAME)
    assert res.out_dir == runs / "_runs" / "_create" / "ab" / NAME / "view"
    assert res.episodes == EPISODES and not res.missing
    idx = res.index.read_text()
    for page in ("report.html", "report.json", "create.html"):
        assert f'href="{page}"' in idx and (res.out_dir / page).is_file(), page
    pages = sorted(p.stem for p in (res.out_dir / "ep").glob("*.html"))
    assert len(pages) == EPISODES
    # Stable keys (QUA-2840), not positions: every page is linked from the index.
    assert set(re.findall(r'href="ep/([A-Za-z0-9._-]+)\.html"', idx)) == set(pages)
    assert {r["id"] for r in _rows(res.index)} == set(pages)
    rows = _rows(res.index)
    assert all(r["cell"] for r in rows)
    assert sum(r["stage"] == "author" for r in rows) == CELLS
    assert "cell · stage" in idx
    report = res.report.read_text()
    assert "VERDICT:" in report and NAME in report
    assert json.loads((res.out_dir / "report.json").read_text())["experiment"] == NAME
    assert "CreateBench board" in (res.out_dir / "create.html").read_text()
    # a creation page shows its authored case and its cell
    author = next(r for r in rows if r["stage"] == "author")
    page = (res.out_dir / "ep" / f"{author['id']}.html").read_text()
    assert view.AUTHORED_CASE in page and author["cell"] in page
    assert _snapshot(runs) == before                     # the runs tree is never written


def test_held_out_episodes_keep_their_badge(runs):
    res = view.build_experiment_view(runs, NAME)
    held = [r for r in _rows(res.index) if r["held"]]
    assert held and all(r["stage"] == "author" for r in held)
    assert view.HELDOUT_BANNER in (res.out_dir / "ep" / f"{held[0]['id']}.html").read_text()
    m = json.loads((res.out_dir / view.MANIFEST).read_text())
    assert m["held_out"] == len(held) == m["runs"][0]["held_out"]


def test_the_manifest_lists_the_experiment_as_one_run(runs):
    res = view.build_experiment_view(runs, NAME)
    m = json.loads((res.out_dir / view.MANIFEST).read_text())
    assert m["format"] == view.MANIFEST_FORMAT == 2 and m["kind"] == "experiment"
    assert m["withheld"] is None                         # a local view is not gated
    [row] = m["runs"]
    # The keys the bench viewer's front page reads (bench_viewer.index_rows).
    assert set(row) >= {"run_id", "started_at", "agents", "conditions", "arms", "episodes",
                        "held_out", "completed", "scored"}
    assert row["run_id"] == NAME and row["arms"] == ["A", "B"]
    assert row["episodes"] == EPISODES == m["episodes"]
    assert (row["completed"], row["scored"]) == (CELLS, CELLS)      # cells, not episodes
    # Format 2's run state, for the experiment: its cells are its units.
    assert row["state"] == {"segment": None, "units_planned": CELLS, "units_done": CELLS,
                            "units_owed": 0, "stopped": None, "complete": True}
    x = m["experiment"]
    assert x["name"] == NAME and x["verdict"] in ab.EXIT and x["cells"] == {"graded": CELLS}
    assert len(x["runs"]) == CELLS + 1        # one run per creation episode + the driver's
    assert x["pages"]["report"] == "report.html"


def test_portable_export_stands_alone_without_private_text(runs, tmp_path):
    before = _snapshot(runs)
    res = view.build_experiment_view(runs, NAME, tmp_path / "export", portable=True,
                                     allow_outside_runs=True)
    out = res.out_dir
    assert json.loads((out / view.MANIFEST).read_text())["portable"] is True
    names = {p.name for p in out.rglob("*")}
    assert "private" not in names and "developer_instructions.md" not in names
    probe = " ".join(PRIVATE.split()[:8])
    assert not [p for p in out.rglob("*") if p.is_file() and probe in p.read_text(errors="replace")]
    assert sum(1 for _ in out.glob("ep/*/transcript.txt")) == EPISODES
    # Moved elsewhere, every relative link on every page still resolves.
    moved = tmp_path / "moved"
    shutil.copytree(out, moved)
    for page in [moved / "index.html", moved / "report.html", moved / "create.html",
                 *(moved / "ep").glob("*.html")]:
        for href in _hrefs(page.read_text()):
            if href.startswith("ep/${"):
                continue
            target = (page.parent / href).resolve()
            assert target.is_relative_to(moved.resolve()) and target.exists(), (page.name, href)
    assert _snapshot(runs) == before


def _first_transcript(runs: Path, stage: str) -> Path:
    e = next(e for e in ab.experiment_episodes(runs, NAME) if e["stage"] == stage)
    return runs / e["episode_dir"] / "agent" / "transcript.txt"


def test_private_text_in_a_transcript_fails_the_portable_build(runs, tmp_path):
    # The agent echoed its developer instructions into a message (JSON-escaped).
    _first_transcript(runs, "author").write_text(_codex(f"My instructions say:\n{PRIVATE}"))
    with pytest.raises(view.ViewError, match="private/") as exc:
        view.build_experiment_view(runs, NAME, tmp_path / "x", portable=True,
                                   allow_outside_runs=True)
    assert "transcript.txt" in str(exc.value) and PRIVATE[:40] not in str(exc.value)
    assert not (tmp_path / "x" / view.MANIFEST).exists()
    # A local view links into the runs tree anyway: not checked, not refused.
    assert view.build_experiment_view(runs, NAME).index.is_file()


def test_a_credential_marker_is_withheld_by_the_gate(runs, tmp_path):
    # Before the reconcile with epic/qua-2839 an experiment view refused (no manifest) on a
    # credential marker, with its own scan (`credential_hits`); its TODO was to "swap the
    # refusing credential scan for the per-episode gate". It now goes through the one gate
    # every portable view has (QUA-2841): the matching files are never written, the
    # manifest lists them under `withheld`, and the CLI exits 65 — the publisher's "do not
    # upload" contract. The credential text is nowhere in the folder.
    _first_transcript(runs, "grade").write_text(_codex("token sk-ant-api03-abcdef in the log"))
    res = view.build_experiment_view(runs, NAME, tmp_path / "x", portable=True,
                                     allow_outside_runs=True)
    m = json.loads((res.out_dir / view.MANIFEST).read_text())
    files = {h["file"] for h in m["withheld"]}
    assert {h["marker"] for h in m["withheld"]} == {"sk-ant-"}
    assert any(f.endswith("/transcript.txt") for f in files) and any(
        f.endswith(".html") for f in files)
    assert res.withheld == m["withheld"]
    assert not [p for p in res.out_dir.rglob("*")
                if p.is_file() and b"sk-ant-api03" in p.read_bytes()]
    assert view.scan_view(res.out_dir) == []
    out = CliRunner().invoke(cli.main, ["view", "--experiment", NAME, "--runs-dir", str(runs),
                                        "--portable"])
    assert out.exit_code == view.EXIT_WITHHELD, out.output


def test_private_text_hits_catch_long_copies_through_escaping_not_shared_phrases(tmp_path):
    ep = tmp_path / "ep"
    (ep / "private").mkdir(parents=True)
    (ep / "private" / "developer_instructions.md").write_text(f"{PRIVATE}\n\n{SHARED}\n")
    out = tmp_path / "out"
    out.mkdir()
    n = view.PRIVATE_WINDOW + view.PRIVATE_STRIDE - 1
    copied = " ".join(PRIVATE.split()[3:3 + n])
    (out / "a.json").write_text(json.dumps({"text": copied.replace(" ", "\n", 5)}))
    (out / "b.html").write_text(f"<pre>{view.E(copied)}</pre>")
    (out / "c.txt").write_text(f"a short shared phrase: {SHARED}")
    (out / "d.png").write_bytes(b"\x89PNG\r\n\x1a\n" + PRIVATE.encode())   # images not read
    hits = view.private_text_hits(out, [ep])
    assert sorted(h["file"] for h in hits) == ["a.json", "b.html"]
    assert all(h["source"] == "private/developer_instructions.md" for h in hits)
    assert view.private_text_hits(out, [tmp_path / "no-private"]) == []


# ── format 2 (the reconcile with epic/qua-2839) ────────────────────────────────

#: The bench viewer publisher's episode-key pattern (qualgent-research-infra
#: `bench_viewer.KEY_RE`): a key names S3 paths and sync filters.
PUBLISHER_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,71}$")


def _publisher_checks(out: Path) -> None:
    """What the publisher asks of a folder before it uploads it (`read_manifest`,
    `read_view`, `vet_view`, `index_rows`), replicated: format 2, portable, gated with
    nothing withheld, every summary key a safe key, every front-page row's keys."""
    m = json.loads((out / view.MANIFEST).read_text())
    assert m["format"] == 2 and m["portable"] is True and m["withheld"] == []
    keys = sorted(p.stem for p in (out / "ep").glob("*.json"))
    assert keys and all(PUBLISHER_KEY_RE.match(k) for k in keys)
    for row in m["runs"]:
        assert set(row) >= {"run_id", "started_at", "agents", "conditions", "arms",
                            "episodes", "held_out", "completed", "scored", "state"}
        assert isinstance(row["state"], dict) and "complete" in row["state"]
    assert json.loads((out / view.RUN_STATE).read_text())["portable"] is True


def test_a_portable_experiment_view_is_a_publishable_format_2_folder(runs, tmp_path):
    res = view.build_experiment_view(runs, NAME, tmp_path / "x", portable=True,
                                     allow_outside_runs=True)
    assert res.withheld == []
    _publisher_checks(res.out_dir)
    # One summary per episode, each keyed like a run view's (`view.episode_key`).
    summaries = [json.loads(p.read_text()) for p in (res.out_dir / "ep").glob("*.json")]
    assert len(summaries) == EPISODES
    assert all(s["exp"]["cell"] and "episode_dir" not in s["exp"] for s in summaries)
    state = json.loads((res.out_dir / view.RUN_STATE).read_text())
    assert set(state["runs"]) == {NAME} and state["experiment"]["name"] == NAME


def test_index_from_rebuilds_an_experiment_view_byte_for_byte(runs, tmp_path):
    res = view.build_experiment_view(runs, NAME, tmp_path / "x", portable=True,
                                     allow_outside_runs=True)
    copy = tmp_path / "copy"
    (copy / "ep").mkdir(parents=True)
    for p in (res.out_dir / "ep").glob("*.json"):           # the summaries alone
        shutil.copyfile(p, copy / "ep" / p.name)
    shutil.copyfile(res.out_dir / view.RUN_STATE, copy / view.RUN_STATE)
    again = view.build_index(copy)
    assert again.withheld == [] and again.episodes == EPISODES
    # The same rows, in plan order, the cells table and the links: one renderer.
    assert again.index.read_text() == res.index.read_text()
    strip = lambda m: {k: v for k, v in m.items() if k != "generated_at"}  # noqa: E731
    assert strip(json.loads((copy / view.MANIFEST).read_text())) == strip(
        json.loads((res.out_dir / view.MANIFEST).read_text()))
    rows = _rows(again.index)
    first = rows[:1 + len(grader.PLAN_ORDER)]
    assert [r["stage"] for r in first] == ["author"] + [
        f"grade · {r}-{i}" for r, i in grader.PLAN_ORDER]


def test_private_text_fails_a_run_view_too(runs, tmp_path):
    # The gate is the same for a plain run view: a creation run whose transcript echoes
    # its private instructions is refused, the file withheld, and no manifest written.
    e = next(e for e in ab.experiment_episodes(runs, NAME) if e["stage"] == "author")
    run_id = json.loads((runs / e["episode_dir"] / "result.json").read_text())["run_id"]
    _first_transcript(runs, "author").write_text(_codex(f"Instructions:\n{PRIVATE}"))
    with pytest.raises(view.ViewError, match="private/") as exc:
        view.build_view(runs, [run_id], tmp_path / "r", portable=True,
                        allow_outside_runs=True)
    assert "transcript.txt" in str(exc.value) and PRIVATE[:40] not in str(exc.value)
    out = tmp_path / "r"
    assert not (out / view.MANIFEST).exists()
    probe = " ".join(PRIVATE.split()[:8])
    assert not [p for p in out.rglob("*") if p.is_file() and probe in p.read_text(errors="replace")]
    assert "private" not in {p.name for p in out.rglob("*")}
    # The summaries record the withheld files, so a later --index-from refuses too.
    assert view.build_index(out).withheld


# ── the CLI ────────────────────────────────────────────────────────────────────

def test_cli_view_experiment(runs, tmp_path):
    cr = CliRunner()
    out = cr.invoke(cli.main, ["view", "--experiment", NAME, "--runs-dir", str(runs),
                               "--portable"])
    assert out.exit_code == 0, out.output
    assert "Experiment view written" in out.output and "Portable" in out.output
    assert (view.experiment_out(runs, NAME) / view.MANIFEST).is_file()
    both = cr.invoke(cli.main, ["view", "--experiment", NAME, "--run", "x", "--runs-dir",
                                str(runs)])
    assert both.exit_code == 2 and "exclusive" in both.output
    nope = cr.invoke(cli.main, ["view", "--experiment", "nope", "--runs-dir", str(runs)])
    assert nope.exit_code == 1 and NAME in nope.output       # names the experiments it has
    outside = cr.invoke(cli.main, ["view", "--experiment", NAME, "--runs-dir", str(runs),
                                   "--out", str(tmp_path / "o")])
    assert outside.exit_code == 1 and "outside the runs root" in outside.output
