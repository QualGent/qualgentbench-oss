"""The launcher's auto-resume loop: what `scripts/launch.py` does with exit 75.

Nothing here boots an emulator, pulls an image or spends a rate limit. The stub for
`docker run` leaves behind exactly what the real container leaves behind — the file
named by `--run-id-file`, and `_runs/<run_id>/stop.json` when the credit guard stopped
it — so the launcher is driven by the same files in the same places a real sweep would
put them, and only the host effects (boot, kill, sleep) are recorded instead of done.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# scripts/ is not a package and launch.py is deliberately stdlib-only (it runs on a
# host with no harness installed), so it is loaded by path rather than imported.
_LAUNCH_PY = Path(__file__).resolve().parents[1] / "scripts" / "launch.py"
_spec = importlib.util.spec_from_file_location("qgb_launch", _LAUNCH_PY)
launch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launch)

RUN_ID = "20260909-120000-a1b2"


def _flag(cmd: list[str], name: str) -> str | None:
    """The value following `name` in an argv list, or None if it is not there."""
    return cmd[cmd.index(name) + 1] if name in cmd else None


def _five_hour(resume_after: float | None, *, waiting: bool = True) -> dict:
    """stop.json as `CreditGuard.write_stop` writes it for a five-hour block."""
    return {"schema_version": 1, "reason": "five_hour_limit",
            "stopped_at": "2026-09-09T12:00:00+00:00",
            "wait_for_five_hour_reset": waiting,
            "resume_after": resume_after, "rate_limit_type": "five_hour",
            "done": 4, "remaining": 6,
            "resume": f"qualgent-bench run --resume {RUN_ID}"}


def _seven_day() -> dict:
    """...and for a seven-day budget stop, which is a hand-off, not a wait."""
    return {"schema_version": 1, "reason": "seven_day_threshold",
            "stopped_at": "2026-09-09T12:00:00+00:00",
            # Present on every stop file; the launcher must not read it as consent to
            # wait out a window that does not reopen for days.
            "wait_for_five_hour_reset": True,
            "utilization": 0.86, "utilization_pct": 86.0, "threshold_pct": 85,
            "resets_at": 1_800_000_000, "rejected": False,
            "done": 4, "remaining": 6,
            "resume": f"qualgent-bench run --resume {RUN_ID}"}


class FakeDocker:
    """`docker run … qualgent-bench run …`, scripted.

    `script` is one (exit code, stop.json body or None) per segment. The run id comes
    off `--resume` when the launcher passed one, so a segment that resumes the wrong
    id would be visible in the files it writes, not just in the argv.
    """

    def __init__(self, runs_dir: Path, script: list[tuple[int, dict | None]]) -> None:
        self.runs_dir, self.script, self.calls = runs_dir, list(script), []

    def __call__(self, cmd: list[str]) -> int:
        self.calls.append(list(cmd))
        rc, stop = self.script.pop(0) if self.script else (0, None)
        run_id = _flag(cmd, "--resume") or RUN_ID
        (self.runs_dir / launch.RUN_ID_FILE).write_text(run_id + "\n")
        if stop is not None:
            meta = self.runs_dir / "_runs" / run_id
            meta.mkdir(parents=True, exist_ok=True)
            (meta / "stop.json").write_text(json.dumps({"run_id": run_id, **stop}))
        return rc


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Every host-side effect `main()` has, replaced by a recorder.

    Preflight, the image and the config are answered rather than performed; boots,
    kills and waits are recorded so the tests can assert the ORDER of the teardown /
    wait / reboot cycle, which is the whole point of the loop.
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    config = tmp_path / "bench.config.yaml"
    config.write_text("image: qualgentbench:test\n")

    state = SimpleNamespace(runs_dir=runs_dir, config=config,
                            boots=[], killed=[], waits=[], docker=None)

    monkeypatch.setattr(launch, "docker_ready", lambda: None)
    monkeypatch.setattr(launch, "image_ready", lambda image, pull: "sha256:test")
    monkeypatch.setattr(launch, "container_preflight", lambda *a, **kw: {
        "config": {"devices": {"avds": ["avd-a"], "max_lanes": 1}}, "checks": []})
    monkeypatch.setattr(launch, "host_checks", lambda cfg, path: ([], {
        "tools": {"adb": "/usr/bin/adb", "emulator": "/usr/bin/emulator"},
        "runs_dir": runs_dir, "env_file": None}))

    def boot(emulator, adb, avds):
        state.boots.append(list(avds))
        return [(avd, f"emulator-{5554 + 2 * i}", object()) for i, avd in enumerate(avds)]

    monkeypatch.setattr(launch, "boot_avds", boot)
    monkeypatch.setattr(launch, "wait_for_boot", lambda adb, booted: None)
    monkeypatch.setattr(launch, "kill_emulators",
                        lambda adb, booted: state.killed.append([s for _, s, _ in booted]))
    monkeypatch.setattr(launch, "start_adb_keepalive", lambda adb: (lambda: None))
    monkeypatch.setattr(launch, "countdown", lambda seconds: state.waits.append(seconds))
    return state


def _launch(state, monkeypatch, script, *argv) -> int:
    state.docker = FakeDocker(state.runs_dir, script)
    monkeypatch.setattr(launch.subprocess, "call", state.docker)
    monkeypatch.setattr(sys, "argv", ["launch.py", str(state.config), "--yes", *argv])
    return launch.main()


# ── the loop ──────────────────────────────────────────────────────────────────


def test_a_five_hour_stop_waits_reboots_and_resumes_the_same_run(host, monkeypatch):
    """The headline: stop, sleep out the block, boot the AVDs again, carry on under
    the same run id."""
    rc = _launch(host, monkeypatch,
                 [(75, _five_hour(time.time() + 3600)), (0, None)])

    assert rc == 0
    assert len(host.docker.calls) == 2
    first, second = host.docker.calls
    # The first segment starts a run; the second continues it.
    assert "--resume" not in first
    assert _flag(second, "--resume") == RUN_ID
    # The id was known on the very first iteration because the container published it
    # inside the runs mount, where the host can read it back.
    assert _flag(first, "--run-id-file") == "/work/runs/.launch-run-id"
    assert (host.runs_dir / launch.RUN_ID_FILE).read_text().strip() == RUN_ID
    # Waited about the hour that was left, plus the margin, and no longer.
    assert 3600 <= host.waits[0] <= 3600 + launch.RESET_MARGIN_SEC + 5
    # The emulators went down for the wait and came back for the resume — which is
    # the reason this wait belongs to the launcher rather than to the harness.
    assert host.boots == [["avd-a"], ["avd-a"]]
    assert host.killed == [["emulator-5554"], ["emulator-5554"]]


def test_a_seven_day_stop_prints_the_export_command_and_does_not_rerun(
        host, monkeypatch, capsys):
    """A seven-day window is days from reopening: waiting is wrong, handing the
    checkpoint to another account is right."""
    rc = _launch(host, monkeypatch, [(75, _seven_day()), (0, None)])
    out = capsys.readouterr().out

    assert rc == 75
    assert len(host.docker.calls) == 1
    assert host.waits == []
    assert f"qualgent-bench checkpoint export {RUN_ID}" in out


def test_ctrl_c_during_the_wait_tears_down_and_stops(host, monkeypatch, capsys):
    """Interrupting a five-hour wait must not leave emulators behind or kill them
    twice: they are already down by the time the sleep starts."""
    def interrupt(seconds):
        host.waits.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(launch, "countdown", interrupt)
    rc = _launch(host, monkeypatch, [(75, _five_hour(time.time() + 600)), (0, None)])

    assert rc == 130
    assert len(host.docker.calls) == 1          # never resumed
    assert len(host.waits) == 1                 # died in the wait, not before it
    assert host.killed == [["emulator-5554"]]   # torn down once, for the wait
    assert "interrupted" in capsys.readouterr().out


def test_no_auto_resume_runs_once_and_returns_the_stop_code(host, monkeypatch):
    """The off switch: the pre-loop behaviour, exit code and all."""
    rc = _launch(host, monkeypatch,
                 [(75, _five_hour(time.time() + 600)), (0, None)], "--no-auto-resume")

    assert (rc, len(host.docker.calls), host.waits) == (75, 1, [])


def test_five_hour_stop_with_waiting_turned_off_hands_back_instead(
        host, monkeypatch, capsys):
    """`checkpoint.wait_for_five_hour_reset: false` is the config's answer and the
    launcher's to obey — the harness stops either way."""
    rc = _launch(host, monkeypatch,
                 [(75, _five_hour(time.time() + 600, waiting=False)), (0, None)])
    out = capsys.readouterr().out

    assert (rc, len(host.docker.calls), host.waits) == (75, 1, [])
    assert f"qualgent-bench run --resume {RUN_ID}" in out
    # It is a wait, not a hand-off: nothing to export.
    assert "checkpoint export" not in out


def test_exit_75_without_a_stop_file_stops_instead_of_resuming_blind(
        host, monkeypatch, capsys):
    rc = _launch(host, monkeypatch, [(75, None), (0, None)])

    assert rc == 75
    assert len(host.docker.calls) == 1
    assert "no stop.json" in capsys.readouterr().out


def test_a_stale_stop_file_cannot_spin_the_loop(host, monkeypatch, capsys):
    """The hazard the guard's own freshness check exists for, one level up.

    Segment 2 exits 75 without managing to write a stop file. Segment 1's is still on
    disk, and its `resume_after` is by then in the past — reading it would mean a
    zero-length wait and an instant re-run, forever. The launcher clears the previous
    segment's file before starting the next one, so "no stop.json" stays honest.
    """
    rc = _launch(host, monkeypatch,
                 [(75, _five_hour(time.time() + 600)), (75, None), (0, None)])

    assert rc == 75
    assert len(host.docker.calls) == 2      # stopped; did not go round again
    assert len(host.waits) == 1
    assert "no stop.json" in capsys.readouterr().out


def test_a_stop_file_naming_another_run_is_refused(host, monkeypatch, capsys):
    """Defence in depth on the same hazard: state that does not name this run is not
    this run's state, whatever directory it was found in."""
    def scribble(cmd):
        host.docker.calls.append(list(cmd))
        (host.runs_dir / launch.RUN_ID_FILE).write_text(RUN_ID + "\n")
        meta = host.runs_dir / "_runs" / RUN_ID
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "stop.json").write_text(
            json.dumps({**_five_hour(time.time() + 600), "run_id": "some-other-run"}))
        return 75

    host.docker = FakeDocker(host.runs_dir, [])
    monkeypatch.setattr(launch.subprocess, "call", scribble)
    monkeypatch.setattr(sys, "argv", ["launch.py", str(host.config), "--yes"])
    rc = launch.main()

    assert rc == 75
    assert len(host.docker.calls) == 1
    assert "mismatched state" in capsys.readouterr().out


def test_the_loop_gives_up_after_ten_segments(host, monkeypatch, capsys):
    """An account that is still blocked after ten sittings is a person's problem, not
    a loop's. Nothing is lost — the run id resumes by hand."""
    rc = _launch(host, monkeypatch,
                 [(75, _five_hour(time.time() + 600))] * (launch.MAX_SEGMENTS + 2))

    assert rc == 75
    assert len(host.docker.calls) == launch.MAX_SEGMENTS
    # The last segment is not followed by a wait nobody will use.
    assert len(host.waits) == launch.MAX_SEGMENTS - 1
    assert f"qualgent-bench run --resume {RUN_ID}" in capsys.readouterr().out


def test_serials_are_taken_from_the_emulators_this_segment_booted(host, monkeypatch):
    """A reboot may land on different console ports, so the resume has to be told
    about the devices it actually has."""
    ports = iter([5554, 5560])

    def boot(emulator, adb, avds):
        host.boots.append(list(avds))
        port = next(ports)
        return [(avd, f"emulator-{port}", object()) for avd in avds]

    monkeypatch.setattr(launch, "boot_avds", boot)
    _launch(host, monkeypatch, [(75, _five_hour(time.time() + 600)), (0, None)])

    first, second = host.docker.calls
    assert _flag(first, "--devices") == "emulator-5554"
    assert _flag(second, "--devices") == "emulator-5560"


# ── the wait itself ───────────────────────────────────────────────────────────


def test_wait_seconds_adds_the_margin_and_caps_the_wait():
    now = 1_000_000.0
    assert launch.wait_seconds({"resume_after": now + 600}, now=now) == (
        600 + launch.RESET_MARGIN_SEC)
    # A reset already in the past is not a negative sleep.
    assert launch.wait_seconds({"resume_after": now - 10_000}, now=now) == 0
    # And a `resume_after` that should not have been trusted cannot park the host for
    # a day: no five-hour window needs more than six hours.
    assert launch.wait_seconds({"resume_after": now + 86_400}, now=now) == launch.MAX_WAIT_SEC


def test_an_unknown_reset_time_waits_out_a_whole_window():
    """`resume_after` is nullable in the contract. Guessing short spends the next
    segment on a second rejection; waiting it out costs time nobody was using."""
    assert launch.wait_seconds({"resume_after": None}, now=0) == launch.UNKNOWN_RESET_WAIT_SEC
    assert launch.wait_seconds({}, now=0) == launch.UNKNOWN_RESET_WAIT_SEC


def test_the_countdown_sleeps_in_chunks_and_always_terminates():
    """Chunked so Ctrl-C lands promptly and a quiet terminal still shows progress;
    driven by what it asked for rather than by the clock, so it cannot hang."""
    slept: list[float] = []
    launch.countdown(150, sleep=slept.append)
    assert slept == [60, 60, 30]
    launch.countdown(0, sleep=slept.append)
    assert len(slept) == 3


def test_reading_a_missing_or_empty_run_id_file_is_not_a_crash(tmp_path):
    assert launch.read_run_id(tmp_path / "nope") is None
    (tmp_path / "blank").write_text("\n")
    assert launch.read_run_id(tmp_path / "blank") is None
    (tmp_path / "id").write_text(f"{RUN_ID}\n")
    assert launch.read_run_id(tmp_path / "id") == RUN_ID


# ── the other half of the contract: who writes the run id ─────────────────────

_SUITE = {
    "app": {"id": "birday", "name": "Birday", "package": "com.birday",
            "platform": "android", "difficulty": "easy"},
    "apk": {"repo": "qualgent/qualgentbench-apps", "filename": "easy/birday.apk",
            "sha256": "a" * 64},
    "exploration": {"id": "explore-birday", "features": [{"id": "a", "state": "broken"}]},
    "tasks": [{"id": "birday-t1", "bug_id": "b1", "type": "bug"}],
}


class _FakeSession:
    async def available_devices(self):
        return ["emu-1"]


async def test_run_publishes_its_run_id_where_the_launcher_looks(tmp_path, monkeypatch):
    """`--run-id-file` exists for the FIRST iteration of the loop, when there is no
    stop.json to read the id out of yet — and the id it publishes has to be the one
    the run keeps its state under, or the resume would address a run that never ran."""
    from qualgentbench import bugs, cli, lanes

    runs = tmp_path / "runs"
    apk = tmp_path / "birday.apk"
    apk.write_bytes(b"apk")
    monkeypatch.setattr(bugs, "load_apps", lambda *a, **kw: [_SUITE])
    monkeypatch.setattr(cli, "_resolve_app_apk", lambda app, spec=None, mode="hunt": apk)

    async def _no_lanes(plan, cfg):
        return cfg.results

    monkeypatch.setattr(lanes, "run_lanes", _no_lanes)

    target = tmp_path / "mount" / ".launch-run-id"
    await cli._run_episodes(
        ["anthropic/claude-opus-4-8"], "claude-code", _FakeSession(), "", runs, 1,
        mode="hunt", devices=["emu-1"], plain=True, yes=True, run_id_file=target)

    run_id = target.read_text().strip()
    assert run_id and "\n" not in target.read_text().strip()
    # Same id the run is keeping its plan under — not a second one.
    assert (runs / "_runs" / run_id / "plan.json").is_file()


def test_an_unwritable_run_id_file_does_not_kill_the_sweep(tmp_path):
    """Telemetry for the launcher, not a precondition for the run: the id is printed
    and stored in plan.json besides."""
    from qualgentbench import cli

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    cli._write_run_id_file(blocker / "sub" / "id", "r1")   # must not raise
    cli._write_run_id_file(None, "r1")
