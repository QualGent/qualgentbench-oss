"""`device_setup` must either stage the world the spec describes or say that it could
not — never half of it, silently.

Four fixtures seeded rows with an on-device `sqlite3` that Google Play images do not
ship; `run-as: exec failed for sqlite3` went to the log and the episode was scored
against a world that was not there. These tests pin the two halves of the fix without
a device: a shell step that fails RAISES (`DeviceSetupError`, which the episode records
as `staging_failed` → `env_failure`), and the `sql:` step writes rows from the host —
pull, apply in one transaction, checkpoint, write back through the app's uid, verify.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path

import pytest

from qualgentbench import episode_runner as er
from qualgentbench.verify import device_oracle


# ── shell steps fail loudly ───────────────────────────────────────────────────

def _scripted_adb(replies: dict[str, tuple[int, str]]):
    """`_adb` whose answer is keyed on a substring of the joined command; the
    timezone pin always succeeds."""
    calls: list[str] = []

    async def fake(*args):
        cmd = " ".join(args)
        calls.append(cmd)
        if "getprop" in cmd:
            return 0, er.DEVICE_TIMEZONE + "\n"
        for needle, reply in replies.items():
            if needle in cmd:
                return reply
        return 0, ""
    return fake, calls


def test_a_shell_step_that_exits_non_zero_raises(monkeypatch):
    fake, _ = _scripted_adb({"pm grant": (255, "Exception occurred while executing 'grant'")})
    monkeypatch.setattr(er, "_adb", fake)
    with pytest.raises(er.DeviceSetupError) as exc:
        asyncio.run(er.run_device_setup("emulator-1", {"shell": ["pm grant x y"]}))
    assert "rc=255" in str(exc.value) and "pm grant x y" in str(exc.value)


def test_a_missing_on_device_binary_raises_even_at_rc_zero(monkeypatch):
    """The exact payload that hid the medtimer fixture: adb folds the remote stderr
    into stdout and the pipeline's own status was 0."""
    fake, _ = _scripted_adb({"sqlite3": (0, "run-as: exec failed for sqlite3: No such file or directory\n")})
    monkeypatch.setattr(er, "_adb", fake)
    with pytest.raises(er.DeviceSetupError) as exc:
        asyncio.run(er.run_device_setup("emulator-1", {
            "shell": ["run-as com.app sqlite3 databases/db 'insert ...'"]}))
    assert "run-as: exec failed" in str(exc.value)
    assert "was not staged" in str(exc.value)


def test_ordinary_shell_output_is_not_a_failure(monkeypatch):
    """What the corpus' healthy steps actually print must keep passing."""
    fake, calls = _scripted_adb({
        "am broadcast": (0, "Broadcasting: Intent { act=android.intent.action.MEDIA_SCANNER_SCAN_FILE }\n"
                            "Broadcast completed: result=0\n"),
        "monkey": (0, "  bash arg: -p\nEvents injected: 1\n## Network stats: elapsed time=12ms\n"),
        "pm disable": (0, "Component {x/y} new state: disabled\n"),
    })
    monkeypatch.setattr(er, "_adb", fake)
    asyncio.run(er.run_device_setup("emulator-1", {"shell": [
        "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/x.png",
        "monkey -p com.app -c android.intent.category.LAUNCHER 1",
        "pm disable x/y", "sleep 5", "am force-stop com.app"]}))
    assert sum("shell" in c for c in calls) >= 5


def test_the_sql_step_runs_host_side_under_the_device_timezone(monkeypatch):
    fake, _ = _scripted_adb({})
    monkeypatch.setattr(er, "_adb", fake)
    seen = {}

    def fake_apply(spec, serial=None, *, tz=None, repo_root=None):
        seen.update(spec=spec, serial=serial, tz=tz, repo_root=repo_root)
        return "db: 1 row(s) changed"
    monkeypatch.setattr(device_oracle, "apply_sql", fake_apply)
    item = {"package": "com.app", "db": "main.db", "statements": "insert into t values (1);"}
    asyncio.run(er.run_device_setup("emulator-1", {"sql": [item]}))
    assert seen["spec"] == item and seen["serial"] == "emulator-1"
    assert seen["tz"] == er.DEVICE_TIMEZONE
    assert seen["repo_root"] == str(Path(er.__file__).resolve().parents[2])


def test_a_failing_sql_fixture_is_a_device_setup_error(monkeypatch):
    fake, _ = _scripted_adb({})
    monkeypatch.setattr(er, "_adb", fake)

    def refuse(spec, serial=None, *, tz=None, repo_root=None):
        raise device_oracle.SqlFixtureError("sql: sandbox refused main.db — run-as cannot enter")
    monkeypatch.setattr(device_oracle, "apply_sql", refuse)
    with pytest.raises(er.DeviceSetupError) as exc:
        asyncio.run(er.run_device_setup("emulator-1", {
            "sql": [{"package": "com.app", "db": "main.db", "statements": "select 1;"}]}))
    assert "sandbox refused" in str(exc.value)


async def test_the_episode_records_a_failed_staging_as_env_failure(tmp_path, monkeypatch):
    """Drive the real `run_episode` through staging with a fixture that raises, then
    stop it dead at the first thing after staging. The spec must carry
    `staging_failed`, which the scorer turns into `env_failure` — excluded, never an
    agent 0."""
    from qualgentbench import failures
    from qualgentbench.schemas import Condition
    from qualgentbench.task import BenchmarkTask

    class _Stop(Exception):
        pass

    class _FakeSession:
        def __init__(self, *_a, **_kw):
            pass

        async def force_release(self, *_a, **_kw): pass
        async def check_device_available(self, *_a, **_kw): pass
        async def reset_app(self, *_a, **_kw): pass
        async def launch_app(self, *_a, **_kw): pass
        async def first_available_device(self): return "emulator-5554"

    async def _noop(*_a, **_kw): pass

    async def _broken_setup(device, spec_setup):
        raise er.DeviceSetupError(
            "device_setup shell step failed (rc=0, output has 'run-as: exec failed'): "
            "'run-as com.app sqlite3 …' — the episode's seeded start state was not staged")

    monkeypatch.setattr(er, "DeviceSession", _FakeSession)
    for fn in ("normalize_app_env", "wipe_shared_storage", "write_bug_flags",
               "isolate_app_under_test", "repin_portrait_after_launch"):
        monkeypatch.setattr(er, fn, _noop)
    monkeypatch.setattr(er, "run_device_setup", _broken_setup)
    async def _device_clean(*_a, **_kw):
        return True

    # The episode-start invariant reads the device (QUA-2781); tests/test_device_clock.py
    # pins it. Here the device is clean.
    monkeypatch.setattr(er, "_refuse_dirty_device", _device_clean)

    async def _no_u2(*_a, **_kw):
        return []

    # Stopped before isolation too (QUA-2781): the previous episode's leftover server.
    monkeypatch.setattr(er, "stop_u2_server", _no_u2)
    monkeypatch.setattr(er, "InteractionLog", lambda *_a, **_kw: (_ for _ in ()).throw(_Stop()))

    task = BenchmarkTask(id="medtimer-skip-logged-dose", name="t", instruction="do it",
                         app_file_id="", app_name="MedTimer", platform="android",
                         bundle_id="com.futsch1.medtimer",
                         bug_spec={"app_id": "medtimer", "mode": "journey",
                                   "device_setup": {"shell": ["run-as com.app sqlite3 …"]}})
    opts = er.EpisodeOptions(**{
        "agent": "claude-code", "model": "anthropic/claude-opus-4-8",
        "condition": Condition.no_routines, "trial": 1, "mcp_server": "",
        "runs_dir": tmp_path / "runs", "task_type": "bug_task",
        "device_serial": "emulator-5554", "run_id": "run-abc", "attempt": 1,
        "segment": 0, "app_id": "medtimer"})
    with pytest.raises(_Stop):
        await er.run_episode(task, opts)
    assert "run-as: exec failed" in task.bug_spec["staging_failed"]
    assert failures.is_excluded({"env_failure": bool(task.bug_spec["staging_failed"])})


# ── apply_sql against a fake device ───────────────────────────────────────────

class _Device:
    """The three adb shapes `apply_sql` uses, over a dict of device files: exec-out
    `cat`, `push`, and the `cat tmp | run-as pkg sh -c 'cat > db'` write-back."""
    _WRITE = re.compile(r"^cat (\S+) \| (?:run-as (\S+) )?sh -c 'cat > (\S+)' && ")

    def __init__(self, files: dict[str, bytes], *, debuggable: bool = True):
        self.files = files
        self.debuggable = debuggable
        self.shell: list[str] = []

    def adb(self, serial, *args, timeout=30):
        if args[0] == "push":
            with open(args[1], "rb") as fh:
                self.files[args[2]] = fh.read()
            return 0, "1 file pushed", ""
        assert args[0] == "shell"
        cmd = args[1]
        self.shell.append(cmd)
        m = self._WRITE.match(cmd)
        if m:
            staging, pkg, target = m.groups()
            if pkg and not self.debuggable:
                return 1, "", "run-as: package not debuggable: " + pkg
            self.files[target] = self.files.pop(staging)
            self.files.pop(target + "-wal", None)
            self.files.pop(target + "-shm", None)
            return 0, "", ""
        return 0, "", ""

    def adb_bytes(self, serial, *args, timeout=60):
        assert args[0] == "exec-out"
        m = re.match(r"^(?:run-as (\S+) )?cat (\S+)$", args[1])
        pkg, path = m.groups()
        if pkg and not self.debuggable:
            return 0, f"run-as: package not debuggable: {pkg}".encode(), ""
        if path not in self.files:
            return 0, f"cat: {path}: No such file or directory".encode(), ""
        return 0, self.files[path], ""


def _seed_db(tmp_path, *, wal: bool = False) -> bytes:
    """A real database as the app would leave it; `wal=True` leaves a row in the
    -wal so the pull has to apply it (returns main+wal as a dict)."""
    path = tmp_path / "seed.db"
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("create table t (v text)")
    con.execute("insert into t values ('seeded')")
    con.commit()
    if wal:
        # Leave the frame in the WAL: close without a checkpoint.
        con.execute("PRAGMA wal_autocheckpoint=0")
        con.execute("insert into t values ('in-wal')")
        con.commit()
        wal_bytes = (tmp_path / "seed.db-wal").read_bytes()
        con.close()
        return {"databases/main.db": path.read_bytes(), "databases/main.db-wal": wal_bytes}
    con.close()
    return {"databases/main.db": path.read_bytes()}


def _rows(data: bytes, tmp_path, sql="select v from t order by rowid"):
    p = tmp_path / "check.db"
    p.write_bytes(data)
    con = sqlite3.connect(p)
    try:
        return [r[0] for r in con.execute(sql)]
    finally:
        con.close()


def _wire(monkeypatch, dev: _Device):
    monkeypatch.setattr(device_oracle, "_adb", dev.adb)
    monkeypatch.setattr(device_oracle, "_adb_bytes", dev.adb_bytes)


def test_apply_sql_writes_the_rows_back_and_verifies(monkeypatch, tmp_path):
    dev = _Device(_seed_db(tmp_path))
    _wire(monkeypatch, dev)
    detail = device_oracle.apply_sql(
        {"package": "com.app", "db": "main.db",
         "statements": ["insert into t values ('one');", "insert into t values ('two');"]},
        "emulator-1")
    assert detail.startswith("main.db: 2 row(s) changed")
    assert _rows(dev.files["databases/main.db"], tmp_path) == ["seeded", "one", "two"]
    # The app was stopped before its file was swapped, and no stale sidecar remains.
    assert dev.shell[0] == "am force-stop com.app"
    assert "databases/main.db-wal" not in dev.files and "databases/main.db-shm" not in dev.files
    assert not [f for f in dev.files if f.startswith("/data/local/tmp/")], "staging file removed"


def test_apply_sql_folds_the_wal_in_before_writing_back(monkeypatch, tmp_path):
    """A row still in the app's -wal must survive: the pushed main file has to carry
    it, because the -wal is deleted on the device."""
    dev = _Device(_seed_db(tmp_path, wal=True))
    _wire(monkeypatch, dev)
    device_oracle.apply_sql({"package": "com.app", "db": "main.db",
                             "statements": "insert into t values ('new');"}, "emulator-1")
    assert _rows(dev.files["databases/main.db"], tmp_path) == ["seeded", "in-wal", "new"]
    assert "databases/main.db-wal" not in dev.files


def test_apply_sql_evaluates_localtime_in_the_device_zone(monkeypatch, tmp_path):
    """tasksorg stamps 'today 18:00 local' with `datetime('now','localtime')`. Local
    means the DEVICE's pinned zone, not whatever the host runs in."""
    dev = _Device(_seed_db(tmp_path))
    _wire(monkeypatch, dev)
    device_oracle.apply_sql(
        {"package": "com.app", "db": "main.db",
         "statements": "insert into t values (strftime('%H', datetime(0,'unixepoch','localtime')));"},
        "emulator-1", tz="America/Chicago")
    # 1970-01-01T00:00Z is 18:00 the previous evening in Chicago (CST, UTC-6).
    assert _rows(dev.files["databases/main.db"], tmp_path)[-1] == "18"


def test_apply_sql_reads_a_repo_relative_file(monkeypatch, tmp_path):
    dev = _Device(_seed_db(tmp_path))
    _wire(monkeypatch, dev)
    (tmp_path / "seed.sql").write_text("-- comment\ndelete from t;\ninsert into t values ('from-file');\n")
    device_oracle.apply_sql({"package": "com.app", "db": "main.db", "file": "seed.sql"},
                            "emulator-1", repo_root=str(tmp_path))
    assert _rows(dev.files["databases/main.db"], tmp_path) == ["from-file"]


def test_a_bad_statement_writes_nothing_back(monkeypatch, tmp_path):
    files = _seed_db(tmp_path)
    before = files["databases/main.db"]
    dev = _Device(files)
    _wire(monkeypatch, dev)
    with pytest.raises(device_oracle.SqlFixtureError) as exc:
        device_oracle.apply_sql(
            {"package": "com.app", "db": "main.db",
             "statements": ["insert into t values ('one');", "insert into nope values (1);"]},
            "emulator-1")
    assert "no such table: nope" in str(exc.value)
    assert dev.files["databases/main.db"] == before, "one transaction: nothing applied"
    assert not any(c.startswith("cat ") for c in dev.shell), "no write-back attempted"


def test_a_release_build_is_refused_with_the_reason(monkeypatch, tmp_path):
    dev = _Device(_seed_db(tmp_path), debuggable=False)
    _wire(monkeypatch, dev)
    with pytest.raises(device_oracle.SqlFixtureError) as exc:
        device_oracle.apply_sql({"package": "com.app", "db": "main.db",
                                 "statements": "select 1;"}, "emulator-1")
    assert device_oracle.db_denied(str(exc.value).removeprefix("sql: "))
    assert "run-as cannot enter" in str(exc.value)


def test_a_database_the_app_has_not_created_yet_is_named(monkeypatch, tmp_path):
    dev = _Device({})
    _wire(monkeypatch, dev)
    with pytest.raises(device_oracle.SqlFixtureError) as exc:
        device_oracle.apply_sql({"package": "com.app", "db": "main.db",
                                 "statements": "select 1;"}, "emulator-1")
    assert "not created yet" in str(exc.value)


def test_a_fixture_without_sql_or_package_is_rejected(monkeypatch, tmp_path):
    _wire(monkeypatch, _Device(_seed_db(tmp_path)))
    with pytest.raises(device_oracle.SqlFixtureError):
        device_oracle.apply_sql({"package": "com.app", "db": "main.db"}, "emulator-1")
    with pytest.raises(device_oracle.SqlFixtureError):
        device_oracle.apply_sql({"db": "main.db", "statements": "select 1;"}, "emulator-1")


# ── QUA-2804: the per-episode nonce is written but never published ────────────

def test_write_bug_flags_writes_a_nonce_into_the_flags_and_marker(monkeypatch):
    """The nonce rides as a `#`-comment line in qgb_flags.txt and as files/.qgb/nonce,
    it is stored on bug_spec for the scanner, and it is NOT counted as an active bug."""
    calls: list[str] = []

    async def fake_adb(*args):
        calls.append(args[-1])          # the shell command string
        return 0, b""

    monkeypatch.setattr(er, "_adb", fake_adb)
    spec = {"active_bugs": ["bug-1", "bug-2"], "type": "bug"}
    asyncio.run(er.write_bug_flags("emulator-5554", "com.futsch1.medtimer", spec))

    nonce = spec["flag_nonce"]
    assert nonce.startswith("QGB-NONCE-") and len(nonce) > len("QGB-NONCE-")
    assert spec["active_bugs_written"] == ["bug-1", "bug-2"]      # the nonce is not a bug
    cmd = calls[0]
    assert f"#{nonce}" in cmd and "files/qgb_flags.txt" in cmd
    assert "files/.qgb/nonce" in cmd
    # the real bug ids are still written, one per line
    assert "bug-1" in cmd and "bug-2" in cmd


def test_flag_file_shape_is_identical_on_both_arms(monkeypatch):
    """QUA-2814: the seeded and clean arms of a case write a file with the SAME line
    count and byte size, so a rooted `wc -l`/`wc -c`/`stat` cannot tell them apart. The
    active bug ids are still present verbatim, and every line carries or is a `#`-comment
    except the ids themselves."""
    seeded = er.flag_file_lines("QGB-NONCE-" + "a" * 32, ["list-row-dead", "second-bug"])
    clean = er.flag_file_lines("QGB-NONCE-" + "b" * 32, [])
    assert len(seeded) == len(clean) == er.FLAG_FILE_LINES
    seeded_txt = "".join(ln + "\n" for ln in seeded)
    clean_txt = "".join(ln + "\n" for ln in clean)
    assert len(seeded_txt) == len(clean_txt)                     # identical byte size
    assert all(len(ln) == er.FLAG_FILE_WIDTH for ln in seeded + clean)
    # the ids are there for the shim (after trimming the pad); the nonce leads
    trimmed = [ln.strip() for ln in seeded]
    assert trimmed[0] == "#QGB-NONCE-" + "a" * 32
    assert "list-row-dead" in trimmed and "second-bug" in trimmed
    # a nonce-bearing comment follows each id, so a `tail -n +2` still meets the nonce
    assert any(ln.strip().startswith("#") for ln in seeded[2:])
    # the clean arm carries no real id, only the nonce and comment padding
    assert all(ln.strip().startswith("#") for ln in clean)


def test_write_bug_flags_reuses_a_preset_nonce(monkeypatch):
    """A nonce staging already generated (e.g. a replay reset) is not regenerated."""
    async def fake_adb(*args):
        return 0, b""

    monkeypatch.setattr(er, "_adb", fake_adb)
    spec = {"active_bugs": [], "type": "clean", "flag_nonce": "QGB-NONCE-preset"}
    asyncio.run(er.write_bug_flags("emulator-5554", "com.x", spec))
    assert spec["flag_nonce"] == "QGB-NONCE-preset"
    assert spec["active_bugs_written"] == []
