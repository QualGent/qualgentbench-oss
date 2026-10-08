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


def test_the_experiment_manifest_carries_its_environment_pins_and_set_key(runs, tmp_path):
    # QUA-2917: additive keys; the publisher's checks above are unchanged.
    res = view.build_experiment_view(runs, NAME, tmp_path / "x", portable=True,
                                     allow_outside_runs=True)
    m = json.loads((res.out_dir / view.MANIFEST).read_text())
    x = m["experiment"]
    env = ab.load_state(ab.state_path(runs, NAME))["environment"]
    assert x["environment"] == env
    assert x["environment"]["runner"]["grader_version"] == grader.GRADER_VERSION
    [entry] = m["runs"]
    assert set(x["arm_pins"]) == set(entry["arms"])
    assert all(set(p) == {"qualgent_mcp", "devloop", "template_sha256"}
               for p in x["arm_pins"].values())
    assert entry["set_key"] == entry["versions"]["set_key"] == (
        f"c-{env['corpus_version']}-g{grader.GRADER_VERSION}-cb{env['create_brief_version']}")
    assert entry["versions"]["mode"] == "create"
    assert entry["board"] == {"now": [], "recorded": [], "by_app_now": [],
                              "by_app_recorded": []} and entry["cases"] == []
    assert entry["models"] and entry["episodes"] == EPISODES
    assert entry["public"]["episodes"] + entry["heldout"]["episodes"] == EPISODES
    assert entry["heldout"]["episodes"] == entry["held_out"]
    # The per-run breakdown keeps its old shape.
    assert all(set(r) == set(view._RUN_KEYS) for r in x["runs"])
    # run.json carries both, so --index-from reproduces them.
    state = json.loads((res.out_dir / view.RUN_STATE).read_text())["experiment"]
    assert state["environment"] == env and state["arm_pins"] == x["arm_pins"]


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


# ── the report's charts (QUA-2922) ─────────────────────────────────────────────

def _figure(page: str, fid: str) -> str:
    m = re.search(rf'<figure class="fig" id="{fid}">(.*?)</figure>', page, re.DOTALL)
    assert m, fid
    return m.group(1)


def test_run_json_carries_the_reports_chart_inputs(runs):
    res = view.build_experiment_view(runs, NAME)
    x = json.loads((res.out_dir / view.RUN_STATE).read_text())["experiment"]
    v = ab.report(runs, NAME)["verdict"]
    assert x["arm_order"] == ["A", "B"]
    assert x["brief_power"] == v["brief_power"] and x["by_group"] == v["by_group"]
    assert x["uptake"] == v["uptake"] and x["uptake"]["arms"]
    assert [e["outcome"] for e in x["expectations"]] == [e["outcome"] for e in v["expectations"]]
    assert all(set(e) == set(view._EXPECTATION_KEYS) for e in x["expectations"])
    assert all(e["why"] == w["why"] and e["a"] == w.get("a") and e["b"] == w.get("b")
               for e, w in zip(x["expectations"], v["expectations"]))
    assert x["preconditions"] == [{k: p.get(k) for k in view._PRECONDITION_KEYS}
                                  for p in v["preconditions"]]
    assert x["pages"]["board_json"] == "create.json"
    assert (res.out_dir / "create.json").is_file()
    # The manifest's experiment block is unchanged but for the linked page.
    m = json.loads((res.out_dir / view.MANIFEST).read_text())["experiment"]
    assert "brief_power" not in m and m["pages"]["board_json"] == "create.json"


def test_x1_forest_has_a_row_per_brief_with_both_arms(runs):
    res = view.build_experiment_view(runs, NAME)
    page = res.index.read_text()
    x1 = _figure(page, "x1")
    bp = json.loads((res.out_dir / view.RUN_STATE).read_text())["experiment"]["brief_power"]
    briefs = [r["brief"] for g in bp.values() for r in g["briefs"]]
    assert sorted(briefs) == sorted(BRIEFS)
    rows = x1.split('<g class="row">')[1:]
    assert len(rows) == len(BRIEFS) + len(bp)              # each brief + a pooled row per group
    for r in rows:
        assert r.count('class="dot"') == 2                 # arm A and arm B
        assert r.count('<g class="pt s2">') == 1           # arm B in --s2
    for b in BRIEFS:
        assert sum(b in r for r in rows) == 1
    assert sum("pooled · " in r for r in rows) == len(bp)
    # B fell below A on the assert brief (the harmful arm): flagged; the legend says so.
    flagged = [b for g in bp.values() for b in g["briefs"] if b["b_below_a"]]
    assert flagged and all(f"▼ {b['brief']}" in x1 for b in flagged)
    assert "arm A (qualgent_mcp aaaaaaa, devloop ddddddd)" in x1
    assert "arm B (qualgent_mcp bbbbbbb, devloop ddddddd)" in x1
    assert "never pooled into the headline" in x1
    assert "B below A on 1/1 brief(s)" in page
    assert "xmlns" not in page and "http://" not in page and "https://" not in page
    # the table twin
    assert "X1 as a table" in page and all(f"<td>{b}</td>" in page for b in BRIEFS)


def test_x1_x2_twins_and_captions_share_the_run_page_helpers(runs):
    # QUA-2931: one twin helper and one caption style across the view's pages.
    page = view.build_experiment_view(runs, NAME).index.read_text()
    for fid, what in (("x1", "X1"), ("x2", "X2")):
        m = re.search(rf'<figure class="fig" id="{fid}">.*?</figure>(<details>.*?</details>)',
                      page, re.S)
        assert m, fid
        assert m.group(1).startswith(view._twin([], [], what).split("<thead>")[0])
        assert '<figcaption class="dim"' in m.group(0)


def test_x2_uptake_and_x3_checklist(runs):
    res = view.build_experiment_view(runs, NAME)
    page = res.index.read_text()
    x2 = _figure(page, "x2")
    assert "uptake of screen-title/v1" in x2
    # Arm B took it (all, assert, walk), drawn in arm B's colour as in X1 (QUA-2943);
    # arm A took none, so no arm-A bar is drawn at all.
    assert x2.count('class="bar s2"') == 3 and x2.count('class="bar"') == 0
    assert '<circle class="key s2"' in x2 and "</title>arm B" in x2 and "</title>arm A" in x2
    assert ">0/2<" in x2 and ">2/2<" in x2                  # k/n at the tips
    m = re.search(r'<div class="tablewrap" id="x3">(.*?)</div>', page, re.DOTALL)
    x3 = m.group(1)
    v = ab.report(runs, NAME)["verdict"]
    assert x3.split("<tbody>")[1].count("<tr>") == len(v["expectations"]) + len(
        v["preconditions"])
    for e in v["expectations"]:
        mark = "✓" if e["outcome"] == ab.MET else "✗" if e["outcome"] == ab.NOT_MET else "○"
        assert f'{mark}</span> {e["outcome"]}' in x3 and view.E(e["why"]) in x3
    assert "✓</span> MET" in x3 and "✗</span> NOT MET" in x3


def test_an_old_run_json_still_rebuilds_without_charts(runs, tmp_path):
    """A run.json written before QUA-2922 has no report fields: `--index-from` renders
    the index it always did (no charts), never an error."""
    res = view.build_experiment_view(runs, NAME, tmp_path / "x", portable=True,
                                     allow_outside_runs=True)
    doc = json.loads((res.out_dir / view.RUN_STATE).read_text())
    for k in ("arm_order", "brief_power", "by_group", "uptake", "expectations",
              "preconditions"):
        doc["experiment"].pop(k)
    (res.out_dir / view.RUN_STATE).write_text(json.dumps(doc))
    page = view.build_index(res.out_dir).index.read_text()
    assert "Report charts" not in page and 'id="cells"' in page


def _rd(k: int, n: int) -> dict:
    return {"k": k, "n": n, "p": round(k / n, 4), "ci": [0.1, 0.9]}


def test_the_pooled_row_names_the_registered_tests_and_preconditions_are_checked():
    """A v3-shaped report (synthetic briefs): the pooled assert row carries the registered
    one-sided Fisher p and the brief-level sign test, the walk row only its brief tally;
    a failed precondition is NOT MET with its why; a missing rate draws n/a, never 0%."""
    x = {"arm_order": ["A", "B"], "arm_pins": {"A": {}, "B": {}},
         "brief_power": {
             "assert": {"briefs": [{"brief": "b-1", "a": _rd(2, 2), "b": _rd(0, 2),
                                    "b_below_a": True},
                                   {"brief": "b-2", "a": _rd(1, 2), "b": None,
                                    "b_below_a": None}],
                        "b_below_a": 1, "judged": 1},
             "walk": {"briefs": [{"brief": "w-1", "a": _rd(1, 1), "b": _rd(1, 1),
                                  "b_below_a": False}], "b_below_a": 0, "judged": 1}},
         "by_group": {"assert": {"power": {"a": _rd(3, 4), "b": _rd(0, 2)}},
                      "walk": {"power": {"a": _rd(1, 1), "b": _rd(1, 1)}}},
         "uptake": None,
         "expectations": [
             {"expectation": "power down (pooled, assert)", "axis": "power",
              "scope": "pooled", "stratum": "assert", "test": "fisher", "outcome": "MET",
              "why": "Fisher exact one-sided p = 0.0123 < 0.05", "a": _rd(3, 4),
              "b": _rd(0, 2), "p_value": 0.0123},
             {"expectation": "power down (each, assert)", "axis": "power", "scope": "each",
              "stratum": "assert", "test": "sign", "outcome": "INCONCLUSIVE",
              "why": "the sign test needs >= 6", "p_value": 0.5,
              "sign": {"for": 1, "judged": 1}}],
         "preconditions": [{"precondition": "arm A power on assert", "met": False,
                            "why": "arm A's power is 40%, under 50%", "a": _rd(2, 5),
                            "b": _rd(0, 5)}]}
    x1 = view._x1_html(x)
    assert "Fisher one-sided p = 0.0123 (MET)" in x1
    assert "sign test p = 0.5 on 1/1 judged brief(s) (INCONCLUSIVE)" in x1
    chart = x1.split("</svg>")[0]
    assert "B below A on 0/1 brief(s)" in chart and "Fisher" not in chart.split("walk briefs")[1]
    assert "n/a: arm B" in x1                         # b-2 has no arm-B rate: no 0% mark
    assert view._x2_html(x) == ""                     # no uptake check: no X2
    x3 = view._x3_html(x)
    assert "✗</span> NOT MET</span></td><td>precondition: arm A power on assert" in x3
    assert "arm A&#x27;s power is 40%, under 50%" in x3
    assert "✓</span> MET" in x3 and "○</span> INCONCLUSIVE" in x3 and ">0.0123<" in x3
    page = view._report_charts(x)
    assert page.startswith('<h2 id="charts">') and "xmlns" not in page and "http" not in page


# ── plain language (QUA-2938) ──────────────────────────────────────────────────

def test_every_verdict_the_code_can_produce_has_a_plain_meaning():
    assert set(ab.VERDICT_MEANING) == set(ab.EXIT)          # DETECTED, MISSED, ...
    x = {"arm_order": ["A", "B"], "briefs": 3, "cells": {"graded": 5, "faulted": 1},
         "expectations": [{"axis": "power", "direction": "down", "scope": "pooled",
                           "stratum": "assert"},
                          {"axis": "specificity", "direction": "flat", "scope": "each",
                           "stratum": "walk"}],
         "preconditions": [{"precondition": "p"}]}
    for v, meaning in ab.VERDICT_MEANING.items():
        assert meaning and "Wilson" not in meaning and "Fisher" not in meaning
        asked, means = view._experiment_plain({**x, "verdict": v})
        assert view.E(meaning) in means and view.E(ab.VERDICT_LIMITS) in means
    assert "3 briefs" in asked and "6 cells" in asked
    assert "lower for arm B than for arm A" in asked
    assert "about the same for both arms" in asked and "brief by brief" in asked
    assert "1 condition" in asked
    # An unknown verdict string says nothing rather than guessing.
    assert view._experiment_plain({**x, "verdict": "SOMETHING"})[1] == ""


def test_the_experiment_page_says_what_it_asked_and_what_the_verdict_means(runs):
    res = view.build_experiment_view(runs, NAME)
    page = res.index.read_text()
    x = json.loads((res.out_dir / view.RUN_STATE).read_text())["experiment"]
    assert "What this experiment asked" in page and "What the verdict means" in page
    assert page.index("What this experiment asked") < page.index("VERDICT:")
    assert view.E(ab.VERDICT_MEANING[x["verdict"]]) in page
    assert page.count("<li>How often the written tests") == len(x["expectations"])
    # The verdict line, prediction, arms, cells headers and chart titles have hover text.
    m = re.search(r'<span class="term"([^>]*)>VERDICT: ', page)
    assert m and f'title="{view.E(ab.VERDICT_MEANING[x["verdict"]])}"' in m.group(1)
    for term in ("prediction", "brief", "experiment arm", "trials", "power", "uptake",
                 "strong-test"):
        assert f'data-term="{view.E(term)}"' in page, term
    assert f"<title>{view.E(view.PLAIN['power'])}</title>" in page
    assert f"<title>{view.E(view.PLAIN['experiment arm'])}</title>" in page
    assert 'class="help"' not in page
    board = (res.out_dir / "create.html").read_text()
    assert f"<title>{view.E(view.PLAIN['strong-test'])}</title>" in board


def test_an_experiment_help_base_survives_index_from(runs, tmp_path):
    from qualgentbench import glossary
    res = view.build_experiment_view(runs, NAME, tmp_path / "x", portable=True,
                                     allow_outside_runs=True, help_base="glossary.html")
    page = res.index.read_text()
    # QUA-2943: a term no documentation entry defines (anchor None) gets no link.
    terms = [t for t in re.findall(r'data-term="([^"]+)"', page)
             if glossary.TERMS[view.html.unescape(t)][1] is not None]
    hrefs = re.findall(r'<a class="help" href="([^"]+)"', page)
    assert terms and len(terms) == len(hrefs)
    assert all(h == f"glossary.html#{glossary.TERMS[view.html.unescape(t)][1]}"
               for t, h in zip(terms, hrefs))
    assert "glossary.html#detected" in page or "glossary.html#verdict" in page
    copy = tmp_path / "copy"
    (copy / "ep").mkdir(parents=True)
    for p in (res.out_dir / "ep").glob("*.json"):
        shutil.copyfile(p, copy / "ep" / p.name)
    shutil.copyfile(res.out_dir / view.RUN_STATE, copy / view.RUN_STATE)
    assert view.build_index(copy).index.read_bytes() == res.index.read_bytes()


# ── executive read-through fixes (QUA-2941) ────────────────────────────────────

def test_the_experiment_page_says_what_the_change_was_and_names_the_arms(runs):
    from qualgentbench.create import uptake
    res = view.build_experiment_view(runs, NAME)
    page = res.index.read_text()
    x = json.loads((res.out_dir / view.RUN_STATE).read_text())["experiment"]
    rule = x["uptake"]["rule"]
    plain = re.search(r'<div class="plain">(.*?)</div>', page, re.DOTALL).group(1)
    # The change, in plain words: the rule arm B's test writer was given.
    assert "The change under test:" in plain and view.E(uptake.PLAIN[rule]) in plain
    assert view.E(uptake.RULES[rule].text) not in page     # never the rule's own text
    # A positive control says so, in plain words, with its definition.
    assert "positive-control" in x["prediction"]
    assert 'data-term="positive control"' in plain and "deliberately harmful" in plain
    assert "arm A is A (the control) and arm B is B (the change under test)" in plain
    # Charts and checklist say which arm is which by name.
    assert "<th>arm A: A</th><th>arm B: B</th>" in page
    assert "▼ = arm B below arm A" in _figure(page, "x1")
    assert "the rule arm B was given" in _figure(page, "x2")
    # Group headings carry their plain definitions; the caption defines both groups.
    assert f"<title>{view.E(view.PLAIN['assert briefs'])}</title>" in page
    assert view.E(view.PLAIN["walk briefs"]) in page


def test_an_unknown_rule_or_a_plain_prediction_says_nothing_rather_than_guessing():
    x = {"arm_order": ["ctl", "new"], "briefs": 1, "cells": {"graded": 2},
         "prediction": "some-change/v1", "uptake": {"rule": "not-a-rule/v9"}}
    asked, _ = view._experiment_plain(x)
    assert "The change under test:" not in asked and "positive control" not in asked
    assert "arm A is ctl (the control) and arm B is new (the change under test)" in asked


def test_p_values_have_one_format_everywhere():
    assert view._fmt_p(0.0) == "< 0.000001" and view._fmt_p(1.66e-09) == "< 0.000001"
    assert view._fmt_p(0.00391) == "0.00391" and view._fmt_p(2.5e-05) == "0.000025"
    assert view._fmt_p(None) == "—" and view._p_text(0.0) == "p < 0.000001"
    assert (view._p_in_text("Fisher exact one-sided p = 1.66e-09 < 0.05")
            == "Fisher exact one-sided p < 0.000001, below 0.05")
    assert (view._p_in_text("right direction, but Fisher exact one-sided p = 0.0712 >= 0.05")
            == "right direction, but Fisher exact one-sided p = 0.0712, not below 0.05")
    x = {"arm_order": ["ctl", "new"], "arm_pins": {},
         "brief_power": {"assert": {"briefs": [{"brief": "b-1", "a": _rd(2, 2),
                                                "b": _rd(0, 2), "b_below_a": True}],
                                    "b_below_a": 1, "judged": 1}},
         "by_group": {"assert": {"power": {"a": _rd(2, 2), "b": _rd(0, 2)}}},
         "expectations": [{"expectation": "power down (pooled, assert)", "axis": "power",
                           "stratum": "assert", "test": "fisher", "outcome": "MET",
                           "why": "Fisher exact one-sided p = 1.66e-09 < 0.05",
                           "a": _rd(2, 2), "b": _rd(0, 2), "p_value": 0.0}],
         "preconditions": []}
    page = view._report_charts(x)
    # The chart, the table twin and the checklist print the same p; never "p = 0".
    assert page.count("p &lt; 0.000001") >= 3 and "p = 0 " not in page and "e-09" not in page
    assert "<td>&lt; 0.000001</td>" in page
    assert "new below ctl on 1/1 brief(s)" in page and "B below A" not in page


# ── round-2 read-through fixes (QUA-2943) ──────────────────────────────────────

def _role(row: dict) -> str:
    """A grade row's run key (`clean-1`, `target-1`, …) from its stage label."""
    return row["stage"].split(" · ", 1)[1] if row["stage"].startswith("grade") else ""


def test_experiment_episodes_carry_the_grades_outcome_for_each_grade_run(runs):
    for e in ab.experiment_episodes(runs, NAME):
        if e["stage"] == "author":
            assert e["outcome"] == ""
            continue
        doc = json.loads(next((runs / "_runs").glob(
            f"*/{grader.GRADES_DIR}/*{e['cell']}*.json")).read_text())
        assert e["outcome"] == doc["grade"]["runs"][e["role"]]["outcome"], e


def test_grade_episodes_show_the_test_outcome_instead_of_run_board_columns(runs):
    res = view.build_experiment_view(runs, NAME)
    page = res.index.read_text()
    head = re.search(r'<table class="idx"><thead><tr>(.*?)</tr></thead><tbody id="tb">',
                     page, re.DOTALL).group(1)
    assert 'data-term="test outcome"' in head
    for gone in ("bugs found/present", "false reports", "agent's verdict", "completed<br>"):
        assert gone not in head, gone
    assert 'id="f-out"' in page and 'id="f-bugs"' not in page and 'id="f-comp"' not in page
    rows = _rows(res.index)
    outcome = {(e["cell"], e["role"]): e["outcome"]
               for e in ab.experiment_episodes(runs, NAME) if e["stage"] == "grade"}
    words = set()
    for r in rows:
        o = r["out"]
        assert o["g"] and o["w"] and o["t"], r          # a glyph, a word and a tooltip
        if not _role(r):
            assert o["w"] in ("wrote a test", "no test written", "test writing")
            continue
        role = _role(r)
        want = view.TEST_OUTCOMES[(role.split("-")[0], outcome[(r["cell"], role)])]
        assert (o["k"], o["g"], o["w"], o["d"], o["t"]) == want, r
        words.add((role.split("-")[0], o["w"]))
    # A written test that PASSED on the build with its bug reads "missed", never found.
    assert ("target", "missed") in words and ("target", "caught") in words
    assert ("clean", "good") in words
    missed = view.TEST_OUTCOMES[("target", "missed")]
    assert missed[3] == "passed on the build with its bug" and missed[1] == "✗"
    assert view.TEST_OUTCOMES[("clean", "failed")][2] == "false failure"
    # The episode page says the same.
    target = next(r for r in rows if _role(r).startswith("target")
                  and r["out"]["w"] == "missed")
    ep = (res.out_dir / "ep" / f"{target['id']}.html").read_text()
    assert "test outcome:" in ep and "✗</span> missed" in ep


def test_summaries_without_a_recorded_outcome_show_the_verdict_never_a_grade_word(runs,
                                                                                 tmp_path):
    """An `--index-from` over summaries written before QUA-2943 (no `exp.outcome`)."""
    res = view.build_experiment_view(runs, NAME, tmp_path / "x", portable=True,
                                     allow_outside_runs=True)
    copy = tmp_path / "copy"
    (copy / "ep").mkdir(parents=True)
    for p in (res.out_dir / "ep").glob("*.json"):
        doc = json.loads(p.read_text())
        doc.get("exp", {}).pop("outcome", None)
        (copy / "ep" / p.name).write_text(json.dumps(doc))
    shutil.copyfile(res.out_dir / view.RUN_STATE, copy / view.RUN_STATE)
    rows = _rows(view.build_index(copy).index)
    grade = [r for r in rows if _role(r)]
    assert grade and all(r["out"]["w"] in ("passed", "failed", "no answer", "excluded")
                         for r in grade)
    assert all("grade's call" in r["out"]["t"] for r in grade if r["out"]["w"] != "no answer")
    assert not {"caught", "missed", "good", "false failure"} & {r["out"]["w"] for r in grade}


def test_experiment_labels_are_plain(runs):
    res = view.build_experiment_view(runs, NAME)
    page = res.index.read_text()
    text = view.html.unescape(re.sub(r"<[^>]+>", " ", page))
    for jargon in ("grade manifest", "driver run", "units done"):
        assert jargon not in text, jargon
    assert 'data-term="driver run"' in page and "written and graded in run" in text
    cells = re.search(r'<h2 id="cells">.*?</thead>', page, re.DOTALL).group(0)
    assert 'data-term="lint"' in cells and ">static checks<" in cells and ">lint<" not in cells


def test_a_weak_flat_expectation_says_so():
    def x3(direction: str, n: int) -> str:
        return view._x3_html({"arm_order": ["A", "B"], "preconditions": [], "expectations": [
            {"expectation": f"power {direction}", "axis": "power", "direction": direction,
             "outcome": "MET", "why": "intervals overlap", "a": _rd(n, n),
             "b": _rd(n // 2, n), "p_value": None}]})
    weak = x3("flat", 4)
    assert "Weak evidence: only 4 cells per arm" in weak and "4/4 against 1/4" in weak
    assert "Weak evidence" not in x3("flat", view.FLAT_WEAK_CELLS)
    assert "Weak evidence" not in x3("down", 4)
    # The example is the widest gap the report's own overlap rule still calls flat.
    from qualgentbench.create.ab import _overlap
    from qualgentbench.rates import rate
    assert _overlap(rate(4, 4), rate(1, 4)) and not _overlap(rate(4, 4), rate(0, 4))
    assert "3 in arm B" in view._flat_weak_note(
        {"direction": "flat", "a": {"n": 4}, "b": {"n": 3}})


def test_assert_and_walk_briefs_are_named_and_linked(runs, tmp_path):
    res = view.build_experiment_view(runs, NAME, tmp_path / "x", portable=True,
                                     allow_outside_runs=True, help_base="g.html")
    plain = re.search(r'<div class="plain">(.*?)</div>', res.index.read_text(),
                      re.DOTALL).group(1)
    for kind in ("assert", "walk"):
        assert (f'data-term="{kind} briefs"' in plain
                and f'href="g.html#{kind}-briefs"' in plain), kind
    assert "Two kinds of brief: in " in plain
