"""`doctor` must refuse a device that cannot evaluate the corpus's database oracles.

Nothing checked this, which is why it went unnoticed through two complete runs: Google
Play system images ship no `sqlite3` binary, the oracle shelled out to one, and an
unevaluated oracle read as success. The check therefore runs the REAL oracle code path
rather than a probe of its own — a bespoke probe would stay green while the thing it
stands for rots. No device: the adb layer is monkeypatched, as the other tests do.
"""

from __future__ import annotations

import shlex
import sqlite3

import pytest

from qualgentbench import doctor, replay as rp
from qualgentbench.verify import device_oracle


def _db_bytes(tmp_path):
    """A real SQLite file, as `run-as cat` would deliver it."""
    path = tmp_path / "probe.db"
    con = sqlite3.connect(path)
    con.execute("create table things (name text)")
    con.commit()
    con.close()
    return path.read_bytes()


def _serving(payload: bytes, asked: list[str]):
    """Answer every pull with `payload`, recording how it was asked for. exec-out folds
    the shell's stderr into stdout and still exits 0, so failures arrive as bytes."""
    def _adb_bytes(serial, *args, timeout=60):
        asked.append(args[-1])
        return 0, payload, ""
    return _adb_bytes


@pytest.fixture
def _one_app(monkeypatch):
    """Report exactly one corpus app with a `db:` oracle as installed, and drop the
    oracle's flush-settle — a test pays no wait meant for a live app's writers."""
    monkeypatch.setattr(device_oracle, "_SETTLE_S", (0, 0), raising=False)
    app_id, package, db = doctor._db_oracle_apps()[0]

    async def _installed(self, device, platform):
        return [package, "com.android.settings"]

    monkeypatch.setattr(doctor.DeviceSession, "list_installed_apps", _installed)
    return app_id, package, db


def _is_warning(result) -> bool:
    """doctor's own vocabulary: a warning prints ⚠ and never counts as a failure
    (see cli._print_checks)."""
    return result.warning and not (result.passed and not result.warning)


# ── the corpus decides what to check ──────────────────────────────────────────

def test_the_candidates_come_from_the_corpus_and_cover_both_path_forms():
    """Hardcoding an app would leave doctor checking a ghost after a retirement, and
    the two `db:` forms fail differently: a sandbox name goes through run-as, an
    absolute path is read by the shell user (AnkiDroid's external collection)."""
    candidates = doctor._db_oracle_apps()
    assert candidates, "the corpus has db oracles — doctor must find them"
    assert all(app and pkg and db for app, pkg, db in candidates)
    assert any(db.startswith("/") for _, _, db in candidates), "absolute form"
    assert any(not db.startswith("/") for _, _, db in candidates), "sandbox form"
    # A Kotlin patch that mentions `db:` in its source text is not an expectation.
    assert not any("SupportSQLiteDatabase" in db for _, _, db in candidates)


# ── the three outcomes ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_readable_database_passes_through_the_replay_oracle(tmp_path,
                                                                   monkeypatch,
                                                                   _one_app):
    """And it must get there through `replay._check_db`: the check is only honest if
    it exercises the code a run uses, so no on-device sqlite3 may be asked for."""
    app_id = _one_app[0]
    asked: list[str] = []
    monkeypatch.setattr(device_oracle, "_adb_bytes",
                        _serving(_db_bytes(tmp_path), asked))

    result = await doctor.check_db_oracle("emulator-5554")

    assert result.passed and not result.warning
    assert app_id in result.detail and "emulator-5554" in result.detail
    assert asked, "the check must actually read the device"
    assert not any("sqlite3" in cmd for cmd in asked), (
        "a Play system image has no sqlite3 — that is the bug this check guards")
    assert any("cat" in shlex.split(cmd) for cmd in asked)


@pytest.mark.asyncio
async def test_a_device_that_cannot_evaluate_an_oracle_is_a_hard_failure(monkeypatch,
                                                                        _one_app):
    """The whole point: a refused sandbox means every database episode would score
    unverified, so this must stop a run rather than print an advisory."""
    monkeypatch.setattr(
        device_oracle, "_adb_bytes",
        _serving(b"run-as: Package 'x' is not debuggable", []))

    result = await doctor.check_db_oracle("emulator-5554")

    assert not result.passed and not result.warning, "a warning would be ignored"
    assert "cannot be evaluated" in result.detail
    assert result.fix and "sqlite3 ON the device" in result.fix


@pytest.mark.asyncio
async def test_a_torn_or_unqueryable_file_is_also_a_failure(monkeypatch, _one_app):
    """A file with the right header that sqlite cannot read is not "nothing staged
    yet" — the device answered, and the answer is unusable."""
    monkeypatch.setattr(
        device_oracle, "_adb_bytes",
        _serving(device_oracle._SQLITE_MAGIC + b"\x00" * 200, []))

    result = await doctor.check_db_oracle("emulator-5554")
    assert not result.passed and not result.warning


@pytest.mark.asyncio
async def test_an_app_that_has_no_database_yet_is_only_a_warning(monkeypatch, _one_app):
    """A fresh install has created nothing to read. Failing here would refuse a
    perfectly good device for not having been staged yet."""
    monkeypatch.setattr(
        device_oracle, "_adb_bytes",
        _serving(b"cat: databases/x.db: No such file or directory", []))

    result = await doctor.check_db_oracle("emulator-5554")
    assert _is_warning(result)
    assert "UNVERIFIED" in result.detail and result.fix


@pytest.mark.asyncio
async def test_no_installed_db_oracle_app_is_unverified_not_broken(monkeypatch):
    """Nothing to read from means nothing is proven — which is a warning with the
    word UNVERIFIED in it, not silence and not a refusal."""
    async def _none(self, device, platform):
        return []

    monkeypatch.setattr(doctor.DeviceSession, "list_installed_apps", _none)
    result = await doctor.check_db_oracle("emulator-5554")
    assert _is_warning(result) and "UNVERIFIED" in result.detail


@pytest.mark.asyncio
async def test_no_device_is_a_warning_and_reads_nothing(monkeypatch):
    """doctor already failed the Device check by then — this must not read a device
    that is not there, nor invent a second failure for the same cause."""
    def _boom(*a, **k):
        raise AssertionError("no device means no adb")

    monkeypatch.setattr(device_oracle, "_adb_bytes", _boom)
    result = await doctor.check_db_oracle(None)
    assert _is_warning(result) and "no device" in result.detail


# ── and the suite actually asks the question ──────────────────────────────────

@pytest.mark.asyncio
async def test_the_doctor_suite_includes_the_db_oracle_check(tmp_path, monkeypatch,
                                                             _one_app):
    """A check nobody runs is a comment. `qualgent-bench doctor` must print it."""
    asked: list[str] = []
    monkeypatch.setenv("QGB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(device_oracle, "_adb_bytes",
                        _serving(_db_bytes(tmp_path), asked))

    async def _hf():
        return doctor.CheckResult("HuggingFace", True, "skipped in tests")

    async def _devices(self):
        return ["emulator-5554"]

    monkeypatch.setattr(doctor, "check_hf_reachable", _hf)
    monkeypatch.setattr(doctor.DeviceSession, "available_devices", _devices)

    results = await doctor.run_doctor(url=None)
    oracle = [r for r in results if r.name == "DB oracle"]
    assert len(oracle) == 1, "the db-oracle check must be part of the doctor suite"
    assert oracle[0].passed and asked


@pytest.mark.asyncio
async def test_the_check_delegates_to_the_replay_oracle_and_nothing_else(monkeypatch,
                                                                        _one_app):
    """Pinned deliberately: the moment this check grows its own reader it stops being
    evidence about the oracle a run uses."""
    seen = []

    async def _spy(serial, bundle, expect, ran):
        seen.append((serial, bundle, expect.db, expect.query))
        return rp.ReplayResult(rp.HOLDS, "spied", ran)

    monkeypatch.setattr(rp, "_check_db", _spy)
    result = await doctor.check_db_oracle("emulator-5554")

    assert result.passed and not result.warning
    assert len(seen) == 1
    serial, bundle, db, query = seen[0]
    assert serial == "emulator-5554" and bundle == _one_app[1] and db == _one_app[2]
    assert "sqlite_master" in query, "a neutral read: the image is under test, not the app"


@pytest.mark.asyncio
async def test_the_probe_is_bounded_and_asks_each_app_once(monkeypatch):
    """`doctor` is what you run before a session, so a device with nothing staged must
    cost it a few seconds — not one flush-settle plus retry per app in the corpus."""
    async def _installed(self, device, platform):
        return [pkg for _app, pkg, _db in doctor._db_oracle_apps()]

    asked: list[str] = []

    async def _nothing_there(serial, bundle, expect, ran):
        asked.append(bundle)
        return rp.ReplayResult(
            rp.INCONCLUSIVE,
            "db query failed: no readable x in the app sandbox (not created yet): cat",
            ran)

    monkeypatch.setattr(doctor.DeviceSession, "list_installed_apps", _installed)
    monkeypatch.setattr(rp, "_check_db", _nothing_there)

    result = await doctor.check_db_oracle("emulator-5554")
    assert _is_warning(result)
    assert 0 < len(asked) <= doctor._PROBE_LIMIT
    assert len(asked) == len(set(asked)), "a second database on the same app proves nothing"
