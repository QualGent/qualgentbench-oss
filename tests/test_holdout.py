"""scripts/holdout.py — move / list / verify against a TEMP copy of the data tree. The
real src/qualgentbench/data/ is never touched: every test builds its own repo under
tmp_path (git init + commit) and points the script at it with --repo-root."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "holdout.py"
DATA = REPO / "src" / "qualgentbench" / "data"
sys.path.insert(0, str(SCRIPT.parent))

import holdout  # noqa: E402

# Two real public journey apps; the temp tree carries only these two so the move's leak
# check is about the move, not about the corpus's own cross-mentions. orgzly (it pushes a
# seed asset) is the app moved in the temp tree, ankidroid stays public.
MOVED, STAYS = "orgzly", "ankidroid"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    data = root / "src" / "qualgentbench" / "data"
    for sub in ("test-cases", "truth", "benchmarks"):
        (data / sub).mkdir(parents=True)
    for app in (MOVED, STAYS):
        shutil.copy2(DATA / "test-cases" / f"{app}.yaml", data / "test-cases")
        shutil.copy2(DATA / "truth" / f"journey-{app}.json", data / "truth")
        shutil.copy2(DATA / "benchmarks" / f"{app}.yaml", data / "benchmarks")
        for src in holdout.corpus.push_sources(DATA / "benchmarks" / f"{app}.yaml"):
            (root / src).parent.mkdir(parents=True, exist_ok=True)
            (root / src).write_bytes(b"fixture")
    # Hunt-side derived truth keyed by app id, in the real file's format.
    stability = {MOVED: [{"area": "a", "derived": "ok"}], STAYS: [{"area": "b", "derived": "ok"}],
                 "unrelated": []}
    (data / "truth" / "hard-stability.json").write_text(json.dumps(stability, indent=1) + "\n")
    (root / "tests" / "fixtures").mkdir(parents=True)
    (root / "tests" / "fixtures" / "note.txt").write_text("nothing about the apps\n")
    (root / ".gitignore").write_text("heldout/\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _run(root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    import os
    e = {**os.environ, **(env or {})}
    e.pop("QGB_HELDOUT_DIR", None)
    if env and "QGB_HELDOUT_DIR" in env:
        e["QGB_HELDOUT_DIR"] = env["QGB_HELDOUT_DIR"]
    return subprocess.run([sys.executable, str(SCRIPT), "--repo-root", str(root), *args],
                          capture_output=True, text=True, env=e)


def _data(root: Path) -> Path:
    return root / "src" / "qualgentbench" / "data"


def test_list_before_and_after_a_move(repo):
    out = _run(repo, "list").stdout
    assert f"public   (2): {STAYS}, {MOVED}" in out and "held-out (0)" in out
    assert _run(repo, "move", MOVED).returncode == 0
    out = _run(repo, "list").stdout
    assert f"public   (1): {STAYS}" in out and f"held-out (1): {MOVED}" in out
    assert "version None" not in out.splitlines()[1]


def test_move_relocates_every_file_of_the_app_and_stages_the_deletions(repo):
    pushes = holdout.corpus.push_sources(_data(repo) / "benchmarks" / f"{MOVED}.yaml")
    assert pushes, "the fixture app must push at least one asset for this test to mean anything"
    r = _run(repo, "move", MOVED)
    assert r.returncode == 0, r.stderr + r.stdout
    dest = repo / "heldout"
    for rel in (f"test-cases/{MOVED}.yaml", f"truth/journey-{MOVED}.json", f"benchmarks/{MOVED}.yaml", *pushes):
        assert (dest / rel).is_file(), rel
        assert not (repo / ("src/qualgentbench/data/" + rel if not rel.startswith("assets/") else rel)).exists(), rel
    # Deletions are staged through git, so they cannot be forgotten; the destination is
    # invisible to git (gitignored).
    status = _git(repo, "status", "--porcelain")
    assert f"D  src/qualgentbench/data/test-cases/{MOVED}.yaml" in status
    assert "heldout" not in status
    # The reminder is printed.
    assert "NEVER commit the destination" in r.stdout
    # The app's entry left the hunt-side stability truth; the rest is byte-identical.
    doc = json.loads((_data(repo) / "truth" / "hard-stability.json").read_text())
    assert MOVED not in doc and STAYS in doc and "unrelated" in doc
    assert (dest / "truth" / f"hard-stability.{MOVED}.json").is_file()
    # The app that stayed is untouched.
    assert (_data(repo) / "test-cases" / f"{STAYS}.yaml").is_file()


def test_move_refuses_a_second_time_and_an_unknown_app(repo):
    assert _run(repo, "move", MOVED).returncode == 0
    r = _run(repo, "move", MOVED)
    assert r.returncode == 2 and "already held out" in r.stderr
    r = _run(repo, "move", "no-such-app")
    assert r.returncode == 2 and "not a journey app" in r.stderr


def test_move_copies_but_keeps_an_asset_another_spec_pushes(repo):
    # Make the staying app push one of the moved app's assets too.
    spec = _data(repo) / "benchmarks" / f"{STAYS}.yaml"
    shared = holdout.corpus.push_sources(_data(repo) / "benchmarks" / f"{MOVED}.yaml")[0]
    import yaml
    doc = yaml.safe_load(spec.read_text())
    doc.setdefault("device_setup", {}).setdefault("push", []).append({"src": shared, "dest": "/sdcard/x"})
    spec.write_text(yaml.safe_dump(doc))
    r = _run(repo, "move", MOVED)
    assert r.returncode == 0, r.stderr
    assert (repo / shared).is_file() and (repo / "heldout" / shared).is_file()
    assert "shared with another spec" in r.stdout


def test_verify_passes_after_a_clean_move_and_uses_the_env_var(repo):
    assert _run(repo, "move", MOVED).returncode == 0
    r = _run(repo, "verify", env={"QGB_HELDOUT_DIR": str(repo / "heldout")})
    assert r.returncode == 0, r.stdout
    assert f"1 app(s) {MOVED}" in r.stdout and "OK" in r.stdout
    # The default location works without the env var too.
    assert _run(repo, "verify").returncode == 0


def test_verify_catches_a_held_out_name_leaking_back(repo):
    assert _run(repo, "move", MOVED).returncode == 0
    (repo / "tests" / "fixtures" / "crash.log").write_text(f"process {MOVED} died\n")
    r = _run(repo, "verify")
    assert r.returncode == 1
    assert f"tests/fixtures/crash.log: mentions '{MOVED}'" in r.stdout
    (repo / "tests" / "fixtures" / "crash.log").unlink()
    # A file NAME carrying the id is a leak too, wherever it sits under data/.
    (_data(repo) / "truth" / f"journey-{MOVED}.json").write_text("{}")
    r = _run(repo, "verify")
    assert r.returncode == 1 and f"file name contains '{MOVED}'" in r.stdout


def test_verify_token_match_does_not_fire_inside_other_words(repo):
    assert _run(repo, "move", MOVED).returncode == 0
    (repo / "tests" / "fixtures" / "ok.txt").write_text(f"x{MOVED}y and {MOVED}_v2 are other tokens\n")
    assert _run(repo, "verify").returncode == 0
    (repo / "tests" / "fixtures" / "ok.txt").write_text(f"{MOVED}-add-measurement is a case id\n")
    assert _run(repo, "verify").returncode == 1


def test_verify_reports_a_broken_or_incomplete_held_out_app(repo):
    assert _run(repo, "move", MOVED).returncode == 0
    held = repo / "heldout"
    (held / "benchmarks" / f"{MOVED}.yaml").unlink()
    r = _run(repo, "verify")
    assert r.returncode == 1 and "journey mode needs the spec" in r.stdout
    (held / "test-cases" / f"{MOVED}.yaml").write_text("app: x\ntest_cases: [\n")
    r = _run(repo, "verify")
    assert r.returncode == 1 and "does not load" in r.stdout


def test_verify_without_a_split_is_a_no_op(repo):
    r = _run(repo, "verify")
    assert r.returncode == 0 and "nothing to verify" in r.stdout


def test_move_refuses_a_destination_inside_the_data_dir(repo):
    r = _run(repo, "--heldout-dir", str(_data(repo) / "heldout"), "move", MOVED)
    assert r.returncode == 2 and "inside the packaged data dir" in r.stderr
    assert (_data(repo) / "test-cases" / f"{MOVED}.yaml").is_file()


# ── sync (QUA-2782): fetch, verify, print the export line ─────────────────────

def test_sync_copies_a_local_split_verifies_it_and_prints_the_export_line(repo, tmp_path):
    canonical = tmp_path / "canonical"
    assert _run(repo, "--heldout-dir", str(canonical), "move", MOVED).returncode == 0
    r = _run(repo, "sync", "--from", str(canonical))
    assert r.returncode == 0, r.stdout + r.stderr
    dest = (repo / "heldout").resolve()
    assert (dest / "test-cases" / f"{MOVED}.yaml").is_file()
    # The harness reads the env var only, so the line is the point of the command.
    assert f"export QGB_HELDOUT_DIR={dest}" in r.stdout.splitlines()
    # With the env var set, sync verifies THAT directory and names it.
    r = _run(repo, "sync", env={"QGB_HELDOUT_DIR": str(canonical)})
    assert r.returncode == 0 and f"export QGB_HELDOUT_DIR={canonical.resolve()}" in r.stdout


def test_sync_prints_no_export_line_for_a_split_that_does_not_verify(repo):
    assert _run(repo, "move", MOVED).returncode == 0
    (repo / "heldout" / "benchmarks" / f"{MOVED}.yaml").unlink()
    r = _run(repo, "sync")
    assert r.returncode == 1 and "export QGB_HELDOUT_DIR" not in r.stdout
    assert "not printing an export line" in r.stdout


def test_sync_without_a_split_or_a_source_says_how_to_fetch_one(repo):
    r = _run(repo, "sync")
    assert r.returncode == 2 and "--from" in r.stderr and "QGB_HELDOUT_SOURCE" in r.stderr


def test_sync_from_s3_runs_the_documented_aws_command(repo, tmp_path):
    """No network: the aws call is stubbed and 'syncs' by moving the app into place."""
    calls: list[list[str]] = []
    dest = repo / "heldout"

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        assert _run(repo, "move", MOVED).returncode == 0      # lands in <repo>/heldout
        return subprocess.CompletedProcess(cmd, 0)

    lines: list[str] = []
    rc = holdout.sync(repo_root=repo, data_root=_data(repo), heldout_root=dest,
                      source="s3://bucket/qualgentbench/heldout", out=lines.append, run=fake_run)
    assert rc == 0, lines
    assert calls == [["aws", "s3", "sync", "s3://bucket/qualgentbench/heldout/", f"{dest}/",
                      "--delete"]]
    assert f"export QGB_HELDOUT_DIR={dest}" in lines

    def failing(cmd, **_kw):
        return subprocess.CompletedProcess(cmd, 255)
    with pytest.raises(holdout.HoldoutError, match="exited 255"):
        holdout.sync(repo_root=repo, data_root=_data(repo), heldout_root=dest,
                     source="s3://bucket/x", out=lines.append, run=failing)
