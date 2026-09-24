"""`conftest._isolate_env` strips every `QGB_*` variable by PREFIX (QUA-2807).

It used to strip a fixed list, so every variable added after the list was written
(`QGB_DEVICE_CLOCK`, `QGB_DEVICE_TIMEZONE`, `QGB_HELDOUT_SOURCE`) reached the tests from a
developer's `.env` or shell, and the suite asserted against whatever they held."""

from __future__ import annotations

import os

from conftest import ENV_STRIP_KEEP, LIVE_DEVICE_ENV, strip_qgb_env


def test_the_kept_switches_are_the_suites_own():
    import adb_replay

    assert ENV_STRIP_KEEP == {LIVE_DEVICE_ENV, adb_replay.SAVED_RUNS_ENV}


def test_no_harness_variable_reaches_a_test():
    """The autouse fixture already ran: only the suite's own variables are left."""
    left = {k for k in os.environ if k.startswith("QGB_")}
    assert left <= {"QGB_LOG", *ENV_STRIP_KEEP}, left


def test_variables_added_after_the_old_list_are_stripped(monkeypatch):
    new = ("QGB_DEVICE_CLOCK", "QGB_DEVICE_TIMEZONE", "QGB_HELDOUT_SOURCE",
           "QGB_A_VARIABLE_NOBODY_HAS_ADDED_YET")
    for var in new:
        monkeypatch.setenv(var, "from-a-developer-env")
    monkeypatch.setenv(LIVE_DEVICE_ENV, "0")
    monkeypatch.setenv("_QGB_DEVICE_GUARD_TEST", "kept: the guard's own, not a QGB_ prefix")
    monkeypatch.setenv("NOT_QGB_X", "kept")

    removed = strip_qgb_env(monkeypatch)

    assert set(new) <= set(removed)
    assert not any(v in os.environ for v in new)
    assert os.environ[LIVE_DEVICE_ENV] == "0"          # the suite's own switch survives
    assert "_QGB_DEVICE_GUARD_TEST" in os.environ and "NOT_QGB_X" in os.environ
