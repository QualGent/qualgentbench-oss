"""Checkpoint bundles: a results-only tar.gz that can be handed to another machine.

The security half of this file is the point of the feature. A bundle is exported on
one person's subscription credits and finished on another's, so anything that could
carry authentication material out of the run dir has to be blocked by path AND
caught by content — the two tests named in the ticket (a planted `sk-ant-` key, a
`claude_home/` full of credentials) are the ones that must fail on broken code.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from qualgentbench import checkpoint
from qualgentbench.checkpoint import CheckpointError, SecretFound

RUN_ID = "20260908-120000-abcd"

# A real seeded-bug id from data/test-cases/fossify-calendar.yaml. It contains the
# substring "sk-", so it doubles as the regression guard for the scrub gate.
SK_TASK = "task-completion-not-persisted"


# ── fixture: a runs tree that looks like a real interrupted sweep ─────────────


def _episode(runs_dir: Path, task_id: str, trial: int, *, run_id: str = RUN_ID,
             result: dict | None = None, kind: str = "bug_task") -> Path:
    """One episode dir with every file the bundle allows AND every file it denies."""
    ep = runs_dir / task_id / f"2026-09-08T00-00-0{trial}Z_{task_id}_claude-code_m_raw_trial-{trial}"
    (ep / "verifier").mkdir(parents=True, exist_ok=True)
    (ep / "workspace").mkdir(parents=True, exist_ok=True)

    checkpoint.write_episode_marker(ep, run_id=run_id, app_id="birday", task_id=task_id,
                                    kind=kind, trial=trial)
    if result is not None:
        (ep / "result.json").write_text(json.dumps(result, indent=2))
        (ep / "verifier" / "ctrf.json").write_text('{"results": {"tests": []}}')
    (ep / "replay.json").write_text('{"replayed": true, "harness_sha": "deadbeef"}')
    (ep / "workspace" / "findings.yaml").write_text(
        f"findings:\n  - area: reminders\n    task: {SK_TASK}\n    note: the toggle resets\n")
    (ep / "instruction_sent.md").write_text(f"# Task\nReproduce {SK_TASK} on Birday.\n")
    (ep / "interactions.json").write_text('{"interactions": 41}')
    (ep / "adb_counts.json").write_text('{"shell": 12}')

    # Everything below is denylisted and must never reach an archive.
    (ep / "claude_home").mkdir(exist_ok=True)
    (ep / "claude_home" / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "sk-ant-oat01-DEADBEEF", '
        '"refreshToken": "sk-ant-ort01-DEADBEEF"}}')
    (ep / "claude_home" / "settings.json").write_text('{"env": {"ANTHROPIC_API_KEY": "x"}}')
    (ep / "codex_home").mkdir(exist_ok=True)
    (ep / "codex_home" / "auth.json").write_text('{"OPENAI_API_KEY": "sk-proj-DEADBEEF"}')
    (ep / "agent").mkdir(exist_ok=True)
    (ep / "agent" / "transcript.txt").write_text("Bearer abc.def\n" * 50)
    (ep / "evidence" / "frames").mkdir(parents=True, exist_ok=True)
    (ep / "evidence" / "frames" / "0001.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    (ep / "hooks").mkdir(exist_ok=True)
    (ep / "hooks" / "count").write_text("312")
    (ep / "app_snapshot.tar").write_bytes(b"\x00" * 512)
    (ep / "mcp_config.json").write_text('{"mcpServers": {"devloop": {"headers": '
                                        '{"Authorization": "Bearer secret"}}}}')
    (ep / ".env").write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-DEADBEEF\n")
    (ep / "settings.json").write_text('{"env": {"ANTHROPIC_AUTH_TOKEN": "x"}}')
    return ep


def _result(task_id: str, trial: int, *, run_id: str = RUN_ID,
            metrics: dict | None = None) -> dict:
    return {
        "task_id": task_id, "task_version": "v1", "task_type": "bug_task",
        "agent": "claude-code", "model": "anthropic/claude-opus-4-8",
        "condition": "raw", "trial": trial, "passed": True, "score": 1.0,
        "weighted_score": 1.0, "started_at": "2026-09-08T00:00:00+00:00",
        "ended_at": "2026-09-08T00:05:00+00:00", "wall_time_sec": 300.0,
        "exit_code": 0, "criteria": {}, "metrics": metrics or {"total_tokens": 1000},
        "failure_reason": None, "run_id": run_id,
        "artifact_dir": f"{task_id}/2026-09-08T00-00-0{trial}Z_{task_id}"
                        f"_claude-code_m_raw_trial-{trial}",
        "provenance": {"device": "emulator-5554", "lane": 0, "segment": 0},
    }


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    """Four planned units: one done, one done under an id containing "sk-", one
    rate-limited (excluded), one interrupted (orphan). Nothing has run trial 2 of
    the hunt, so the plan also has a never-started unit."""
    runs = tmp_path / "origin" / "runs"
    meta = runs / "_runs" / RUN_ID
    meta.mkdir(parents=True)
    meta.joinpath("plan.json").write_text(json.dumps({
        "run_id": RUN_ID, "mode": "guided", "segment": 0,
        "agent": "claude-code", "model": "anthropic/claude-opus-4-8",
        "devices": ["emulator-5554"], "episodes": 5,
        "environment": {"schema_version": 1, "package_version": "0.2.0",
                        "image_digest": "sha256:cafebabe",
                        "apps": {"birday": {"spec_version": "sha256:aaaa",
                                            "apk_sha256": "a" * 64}}},
        "units": [
            {"app": "birday", "task": "birday-t1", "kind": "bug_task", "trial": 1},
            {"app": "birday", "task": "birday-t1", "kind": "bug_task", "trial": 2},
            {"app": "fossify-calendar", "task": SK_TASK, "kind": "bug_task", "trial": 1},
            {"app": "birday", "task": "explore-birday", "kind": "bug_hunt", "trial": 1},
            {"app": "birday", "task": "explore-birday", "kind": "bug_hunt", "trial": 2},
        ],
    }, indent=2))
    meta.joinpath("schedule.jsonl").write_text(
        '{"ts": "2026-09-08T00:00:00", "event": "start", "lane": 0}\n')
    meta.joinpath("board.json").write_text(json.dumps({"run_id": RUN_ID, "episodes": []}))
    # Run-level provider state — denylisted, must not travel.
    meta.joinpath("rate_limit.json").write_text(
        '{"unifiedWindows": {"seven_day": {"utilization": 0.62}}}')

    _episode(runs, "birday-t1", 1, result=_result("birday-t1", 1))
    _episode(runs, SK_TASK, 1, result=_result(SK_TASK, 1))
    # Rate limited: scored, but not a result — stays home and stays in `remaining`.
    _episode(runs, "explore-birday", 1, kind="bug_hunt",
             result=_result("explore-birday", 1,
                            metrics={"failure_class": "rate_limited"}))
    # Interrupted: marker, no result.json.
    _episode(runs, "birday-t1", 2)
    # Another run's episode, in the same tree — must be ignored entirely.
    _episode(runs, "birday-t1", 7, run_id="other-run",
             result=_result("birday-t1", 7, run_id="other-run"))
    return runs


def _names(bundle: Path) -> set[str]:
    with tarfile.open(bundle, "r:gz") as tar:
        return set(tar.getnames())


def _blob(bundle: Path) -> bytes:
    """Every byte of the archive, decompressed — for "this string is nowhere in it"."""
    with tarfile.open(bundle, "r:gz") as tar:
        return b"".join((tar.extractfile(m) or io.BytesIO()).read()
                        for m in tar.getmembers()) + b"".join(
            n.encode() for n in tar.getnames())


# ── the denylist ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [
    "birday-t1/ep/claude_home/.credentials.json",
    "birday-t1/ep/codex_home/auth.json",
    "birday-t1/ep/agent/transcript.txt",
    "birday-t1/ep/evidence/frames/0001.jpg",
    "birday-t1/ep/hooks/count",
    "birday-t1/ep/app_snapshot.tar",
    "birday-t1/ep/mcp_config.json",
    "birday-t1/ep/settings.json",
    "birday-t1/ep/.env",
    "birday-t1/ep/.env.local",
    "_runs/r1/rate_limit.json",
    # Nested copies are the interesting case: the rule is about the path, so a
    # config home that turns up inside the workspace is refused just the same.
    "birday-t1/ep/workspace/claude_home/.credentials.json",
    "birday-t1/ep/workspace/.env",
])
def test_denylist_blocks_by_path(path):
    assert checkpoint.denied_by(path) is not None, path


@pytest.mark.parametrize("path", [
    "birday-t1/ep/result.json",
    "birday-t1/ep/verifier/ctrf.json",
    "birday-t1/ep/workspace/findings.yaml",
    "birday-t1/ep/instruction_sent.md",
    "_runs/r1/plan.json",
])
def test_denylist_leaves_the_scoring_files_alone(path):
    assert checkpoint.denied_by(path) is None, path


def test_the_allowlist_and_the_denylist_do_not_overlap():
    """If they ever did, the bundle would be silently missing a scoring file rather
    than loudly refusing to build."""
    for name in checkpoint.EPISODE_FILES + checkpoint.RUN_META_FILES:
        assert checkpoint.denied_by(f"task/ep/{name}") is None, name


# ── the scrub gate ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("planted, marker", [
    ("anthropic_key: sk-ant-api03-AAAABBBBCCCC", "sk-ant-"),
    ("openai_key: sk-proj-AAAABBBBCCCCDDDD", "sk-"),
    ("CLAUDE_CODE_OAUTH_TOKEN=abc123", "CLAUDE_CODE_OAUTH_TOKEN"),
    ("ANTHROPIC_API_KEY: hunter2", "ANTHROPIC_"),
    ("Authorization: Bearer eyJhbGciOi", "Bearer "),
    ('{"refreshToken": "abc"}', "refreshToken"),
    ('{"accessToken": "abc"}', "accessToken"),
])
def test_scan_catches_every_credential_marker(planted, marker):
    hit = checkpoint.scan_for_secrets(f"clean line\n{planted}\n".encode())
    assert hit == (marker, 2)


def test_scan_ignores_benchmark_ids_that_merely_contain_sk():
    """`task-completion-not-persisted` and `subtask-chip-low` are real seeded-bug ids
    and appear in result.json, findings.yaml and instruction_sent.md. If a bare "sk-"
    substring aborted the export, no fossify-calendar or tasksorg run could ever be
    checkpointed and the gate would be the first thing anyone disabled."""
    clean = (f"task_id: {SK_TASK}\nbugs: [subtask-chip-low, task-delete-broken]\n"
             f"note: risk-free, disk-backed, ask-first\n")
    assert checkpoint.scan_for_secrets(clean.encode()) is None


def test_scan_reads_undecodable_bytes_rather_than_skipping_them():
    assert checkpoint.scan_for_secrets(b"\xff\xfe binary sk-ant-api03-XX") is not None


# ── export ────────────────────────────────────────────────────────────────────


def test_export_packs_the_scoring_files_and_nothing_else(runs_dir, tmp_path):
    out = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz")
    names = _names(out.path)

    assert checkpoint.MANIFEST_NAME in names
    assert {f"_runs/{RUN_ID}/plan.json", f"_runs/{RUN_ID}/schedule.jsonl",
            f"_runs/{RUN_ID}/board.json"} <= names
    # Two completed episodes x eight scoring files, plus three run-level files.
    packed_episode_files = {n for n in names
                            if not n.startswith("_runs/") and n != checkpoint.MANIFEST_NAME}
    assert {Path(n).name for n in packed_episode_files} <= {
        Path(f).name for f in checkpoint.EPISODE_FILES}
    # The rate-limited attempt is not a result, so it does not travel; the other
    # run's episode is not ours.
    assert not any("explore-birday" in n for n in names)
    assert not any("trial-7" in n for n in names)


def test_claude_home_credentials_are_absent_from_the_archive(runs_dir, tmp_path):
    """Ticket acceptance: a claude_home/ with a fake credentials file is not in the
    bundle — by name and by content."""
    out = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz")

    names = _names(out.path)
    for denied in ("claude_home", "codex_home", "/agent/", "evidence", "hooks",
                   "app_snapshot.tar", "mcp_config.json", ".env", "settings.json",
                   "rate_limit.json"):
        assert not any(denied in n for n in names), f"{denied} reached the archive"
    blob = _blob(out.path)
    for secret in (b"sk-ant-oat01-DEADBEEF", b"sk-ant-ort01-DEADBEEF",
                   b"sk-proj-DEADBEEF", b"Bearer secret", b"CLAUDE_CODE_OAUTH_TOKEN"):
        assert secret not in blob, f"{secret!r} reached the archive"


def test_a_planted_anthropic_key_aborts_the_export(runs_dir, tmp_path):
    """Ticket acceptance: a credential in a file that IS on the allowlist stops the
    whole export, naming the file. The denylist cannot catch this one — findings.yaml
    is a legitimate bundle member that the agent wrote."""
    findings = next(runs_dir.glob("birday-t1/*trial-1/workspace/findings.yaml"))
    findings.write_text(findings.read_text() + "    evidence: sk-ant-api03-LEAKED\n")

    out = tmp_path / "b.tar.gz"
    with pytest.raises(SecretFound) as exc:
        checkpoint.export_bundle(runs_dir, RUN_ID, output=out)

    assert "workspace/findings.yaml" in str(exc.value)
    assert "sk-ant-" in str(exc.value)
    # Nothing partial survives an abort.
    assert not out.exists()
    assert not list(tmp_path.glob("*.partial"))


def test_an_ordinary_run_of_apps_with_sk_in_their_ids_exports_fine(runs_dir, tmp_path):
    """The counterpart to the test above: the gate must not be a false alarm on the
    normal case, or nobody will keep it switched on."""
    out = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz")
    assert any(SK_TASK in n for n in _names(out.path))


def test_export_refuses_a_scoring_file_replaced_by_a_symlink(runs_dir, tmp_path):
    secret = tmp_path / "credentials.json"
    secret.write_text('{"accessToken": "sk-ant-oat01-X"}')
    findings = next(runs_dir.glob("birday-t1/*trial-1/workspace/findings.yaml"))
    findings.unlink()
    findings.symlink_to(secret)

    with pytest.raises(CheckpointError, match="symlink"):
        checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz")


def test_export_discards_interrupted_episodes_before_packing(runs_dir, tmp_path):
    orphan = next(runs_dir.glob("birday-t1/*trial-2"))
    assert orphan.is_dir() and not (orphan / "result.json").exists()

    out = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz")

    assert not orphan.exists(), "the orphan should have been moved, not left in place"
    moved = runs_dir / "_discarded" / RUN_ID / "birday-t1" / orphan.name
    assert (moved / "episode.json").exists(), "discarded, not deleted"
    assert out.discarded == ("birday-t1/" + orphan.name,)
    assert out.counts["discarded"] == 1
    assert not any("trial-2" in n for n in _names(out.path))


def test_manifest_records_what_the_run_was_and_what_is_left(runs_dir, tmp_path):
    m = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz").manifest

    assert m["schema_version"] == 1
    assert m["run_id"] == RUN_ID and m["segment"] == 0
    assert m["agent"] == "claude-code"
    # The NAME, with the routing id kept beside it for the resume.
    assert m["model"] == "claude-opus-4-8"
    assert m["model_id"] == "anthropic/claude-opus-4-8"
    assert m["mode"] == "guided" and m["trials"] == 2
    assert m["scope"]["apps"] == ["birday", "fossify-calendar"]
    assert m["package_version"] == "0.2.0"
    assert m["image_digest"] == "sha256:cafebabe"
    assert m["host"] and m["created_at"].startswith("20")
    assert m["counts"] == {"planned": 5, "done": 2, "remaining": 3, "excluded": 1,
                           "discarded": 1, "files": m["counts"]["files"]}
    # The rate-limited unit is NOT done, so it is still owed.
    remaining = {(u["task"], u["trial"]) for u in m["remaining"]}
    assert remaining == {("birday-t1", 2), ("explore-birday", 1), ("explore-birday", 2)}


def test_manifest_carries_a_sha256_per_packed_file(runs_dir, tmp_path):
    import hashlib

    out = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz")
    listed = {f["path"]: f["sha256"] for f in out.manifest["files"]}
    assert listed and len(listed) == out.counts["files"]

    with tarfile.open(out.path, "r:gz") as tar:
        for path, digest in listed.items():
            data = (tar.extractfile(path) or io.BytesIO()).read()
            assert hashlib.sha256(data).hexdigest() == digest, path


def test_export_names_the_bundle_after_the_run_and_segment(runs_dir, tmp_path):
    plan_path = runs_dir / "_runs" / RUN_ID / "plan.json"
    plan = json.loads(plan_path.read_text())
    plan["segment"] = 2
    plan_path.write_text(json.dumps(plan))

    out = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path)
    assert out.path.name == f"qgb-checkpoint-{RUN_ID}-seg2.tar.gz"
    assert out.manifest["segment"] == 2


def test_export_of_an_unknown_run_says_so(runs_dir, tmp_path):
    with pytest.raises(CheckpointError, match="no run nope"):
        checkpoint.export_bundle(runs_dir, "nope", output=tmp_path / "b.tar.gz")


# ── import ────────────────────────────────────────────────────────────────────


def _export_and_import(runs_dir: Path, tmp_path: Path) -> tuple[Path, Path]:
    bundle = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz").path
    landing = tmp_path / "coworker" / "runs"
    checkpoint.import_bundle(bundle, landing)
    return bundle, landing


def test_round_trip_leaves_the_second_machine_owing_the_same_units(runs_dir, tmp_path):
    """Ticket acceptance: export on A, import into an empty B, and B's view of what
    is done and what is left matches A's."""
    before = checkpoint.run_summary(runs_dir, RUN_ID)
    _, landing = _export_and_import(runs_dir, tmp_path)
    after = checkpoint.run_summary(landing, RUN_ID)

    assert after["counts"]["done"] == before["counts"]["done"] == 2
    assert after["remaining"] == before["remaining"]
    assert after["counts"]["remaining"] == 3
    assert (after["run_id"], after["model_id"], after["mode"]) == \
           (before["run_id"], before["model_id"], before["mode"])
    assert after["scope"] == before["scope"]
    # What deliberately does NOT travel: the unquotable attempt stays on the machine
    # that produced it (its unit is in `remaining`, so nothing is lost), and so does
    # the discard pile.
    assert after["counts"]["excluded"] == 0 and before["counts"]["excluded"] == 1


def test_import_lays_down_the_files_and_names_the_resume_command(runs_dir, tmp_path):
    bundle = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz").path
    landing = tmp_path / "coworker" / "runs"

    result = checkpoint.import_bundle(bundle, landing)

    assert (landing / "_runs" / RUN_ID / "plan.json").exists()
    assert len(result.episodes) == 2
    for episode in result.episodes:
        assert (landing / episode / "result.json").exists()
        assert (landing / episode / "workspace" / "findings.yaml").exists()
        assert not (landing / episode / "claude_home").exists()
    assert result.resume_command.startswith(f"qualgent-bench run --resume {RUN_ID}")
    assert f"--runs-dir {landing}" in result.resume_command


def test_import_records_which_episodes_have_no_local_artifacts(runs_dir, tmp_path):
    """`imported.json` is how the replay/staleness pass learns an episode's evidence
    was never on this disk, instead of calling it broken."""
    _, landing = _export_and_import(runs_dir, tmp_path)

    marker = json.loads((landing / "_runs" / RUN_ID / "imported.json").read_text())
    assert marker["run_id"] == RUN_ID
    assert len(marker["episodes"]) == 2
    assert all("/" in e for e in marker["episodes"])
    assert marker["imports"][0]["bundle"] == "b.tar.gz"
    assert checkpoint.imported_episodes(landing, RUN_ID) == set(marker["episodes"])


def test_importing_the_same_bundle_twice_is_a_no_op(runs_dir, tmp_path):
    bundle, landing = _export_and_import(runs_dir, tmp_path)

    again = checkpoint.import_bundle(bundle, landing)

    assert len(again.written) == len(again.manifest["files"])
    marker = json.loads((landing / "_runs" / RUN_ID / "imported.json").read_text())
    assert len(marker["episodes"]) == 2, "episode list is a set, not an append log"
    assert len(marker["imports"]) == 2, "but each import is still recorded"


def test_import_refuses_a_run_that_already_exists_with_different_results(runs_dir,
                                                                        tmp_path):
    """Ticket acceptance. Two machines that both ran a unit produced two different
    answers; silently keeping one is how a board stops being reproducible."""
    bundle, landing = _export_and_import(runs_dir, tmp_path)
    victim = landing / next(iter(checkpoint.imported_episodes(landing, RUN_ID)))
    tampered = json.loads((victim / "result.json").read_text())
    tampered["passed"] = False
    (victim / "result.json").write_text(json.dumps(tampered))

    with pytest.raises(CheckpointError, match="already exists"):
        checkpoint.import_bundle(bundle, landing)

    # Refused means refused: the local file is still the local one.
    assert json.loads((victim / "result.json").read_text())["passed"] is False


def test_import_rejects_a_file_whose_checksum_does_not_match(runs_dir, tmp_path):
    bundle = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz").path
    _rewrite_bundle(bundle, edit={f"_runs/{RUN_ID}/board.json": b'{"tampered": true}'})

    with pytest.raises(CheckpointError, match="checksum mismatch"):
        checkpoint.import_bundle(bundle, tmp_path / "landing")


def test_import_rejects_a_member_the_manifest_never_listed(runs_dir, tmp_path):
    bundle = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz").path
    _rewrite_bundle(bundle, add={f"_runs/{RUN_ID}/extra.json": b"{}"})

    with pytest.raises(CheckpointError, match="not listed"):
        checkpoint.import_bundle(bundle, tmp_path / "landing")


def test_import_refuses_a_denylisted_member_even_if_the_manifest_lists_it(runs_dir,
                                                                         tmp_path):
    """A bundle arrives from someone else's machine. Its sender's gates are not this
    machine's evidence, so the denylist runs again on the way in."""
    bundle = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz").path
    _rewrite_bundle(bundle, add={"birday-t1/ep/claude_home/.credentials.json": b"{}"},
                    list_added=True)

    with pytest.raises(CheckpointError, match="denylisted"):
        checkpoint.import_bundle(bundle, tmp_path / "landing")


def test_import_refuses_a_path_that_escapes_the_runs_dir(runs_dir, tmp_path):
    bundle = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz").path
    _rewrite_bundle(bundle, add={"../../escaped.json": b"{}"}, list_added=True)

    with pytest.raises(CheckpointError, match="refusing member path"):
        checkpoint.import_bundle(bundle, tmp_path / "landing")
    assert not (tmp_path / "escaped.json").exists()


def test_import_rejects_a_bundle_carrying_a_credential(runs_dir, tmp_path):
    bundle = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz").path
    _rewrite_bundle(bundle, edit={f"_runs/{RUN_ID}/board.json":
                                  b'{"token": "sk-ant-api03-SMUGGLED"}'},
                    refresh_digests=True)

    with pytest.raises(SecretFound):
        checkpoint.import_bundle(bundle, tmp_path / "landing")


def test_import_rejects_something_that_is_not_a_bundle(tmp_path):
    junk = tmp_path / "notes.tar.gz"
    junk.write_bytes(b"not a tarball")
    with pytest.raises(CheckpointError, match="not a qualgent-bench checkpoint bundle"):
        checkpoint.import_bundle(junk, tmp_path / "landing")


def _rewrite_bundle(bundle: Path, *, add: dict[str, bytes] | None = None,
                    edit: dict[str, bytes] | None = None, list_added: bool = False,
                    refresh_digests: bool = False) -> None:
    """Rebuild `bundle` with members added or replaced — a hostile or corrupted
    archive, which is the only way to test the import-side gates."""
    import hashlib

    add, edit = add or {}, edit or {}
    with tarfile.open(bundle, "r:gz") as tar:
        members = {m.name: (tar.extractfile(m) or io.BytesIO()).read()
                   for m in tar.getmembers()}
    manifest = json.loads(members.pop(checkpoint.MANIFEST_NAME))
    members.update(edit)
    members.update(add)
    if list_added:
        manifest["files"].extend(
            {"path": name, "sha256": hashlib.sha256(data).hexdigest(),
             "bytes": len(data)} for name, data in add.items())
    if refresh_digests:
        for entry in manifest["files"]:
            if entry["path"] in edit:
                entry["sha256"] = hashlib.sha256(edit[entry["path"]]).hexdigest()
    members[checkpoint.MANIFEST_NAME] = json.dumps(manifest, indent=2).encode()

    with tarfile.open(bundle, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


# ── show ──────────────────────────────────────────────────────────────────────


def test_show_reads_a_bundle_without_unpacking_it(runs_dir, tmp_path):
    out = checkpoint.export_bundle(runs_dir, RUN_ID, output=tmp_path / "b.tar.gz")
    view = checkpoint.describe(out.path)
    assert view["run_id"] == RUN_ID
    assert len(view["remaining"]) == 3
    assert view["files"]


def test_show_reads_a_run_id_off_the_disk(runs_dir):
    view = checkpoint.describe(RUN_ID, runs_dir=runs_dir)
    assert view["run_id"] == RUN_ID
    assert "files" not in view, "nothing has been packed yet"
    # The live view can see an interrupted episode; a bundle never carries one.
    assert view["counts"]["orphans"] == 1
    assert view["counts"]["done"] == 2


def test_show_says_when_a_run_is_finished(runs_dir, tmp_path):
    plan_path = runs_dir / "_runs" / RUN_ID / "plan.json"
    plan = json.loads(plan_path.read_text())
    plan["units"] = [u for u in plan["units"]
                     if (u["task"], u["trial"]) in {("birday-t1", 1), (SK_TASK, 1)}]
    plan_path.write_text(json.dumps(plan))

    assert checkpoint.describe(RUN_ID, runs_dir=runs_dir)["remaining"] == []


# ── CLI ───────────────────────────────────────────────────────────────────────


def _cli(*args):
    from click.testing import CliRunner

    from qualgentbench.cli import main

    return CliRunner().invoke(main, list(args))


def test_cli_export_import_show(runs_dir, tmp_path):
    bundle = tmp_path / f"qgb-checkpoint-{RUN_ID}-seg0.tar.gz"

    out = _cli("checkpoint", "export", RUN_ID, "--runs-dir", str(runs_dir),
               "-o", str(bundle))
    assert out.exit_code == 0, out.output
    assert bundle.exists()
    assert "done 2" in out.output and "remaining 3" in out.output

    landing = tmp_path / "landing"
    out = _cli("checkpoint", "import", str(bundle), "--runs-dir", str(landing))
    assert out.exit_code == 0, out.output
    assert f"run --resume {RUN_ID}" in out.output

    out = _cli("checkpoint", "show", str(bundle))
    assert out.exit_code == 0, out.output
    assert "Remaining units (3)" in out.output

    out = _cli("checkpoint", "show", RUN_ID, "--runs-dir", str(landing), "--json")
    assert out.exit_code == 0, out.output
    assert json.loads(out.output)["counts"]["done"] == 2


def test_cli_export_reports_a_leak_as_a_clean_failure(runs_dir, tmp_path):
    findings = next(runs_dir.glob("birday-t1/*trial-1/workspace/findings.yaml"))
    findings.write_text("token: sk-ant-api03-LEAKED\n")

    out = _cli("checkpoint", "export", RUN_ID, "--runs-dir", str(runs_dir),
               "-o", str(tmp_path / "b.tar.gz"))

    assert out.exit_code != 0
    assert "workspace/findings.yaml" in out.output
    assert not (tmp_path / "b.tar.gz").exists()
