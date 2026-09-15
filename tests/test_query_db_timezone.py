"""`query_db` evaluates an oracle's SQL under the DEVICE's timezone, not the host's.

Oracles are written against the device's day: `date('now','localtime')` in a check
means the day the app showed the agent, and the app's clock is the emulator's pinned
zone (`episode_runner.pin_device_timezone`). The host's sqlite reads TZ from the host
process, so before this the four date checks and `tasks-change-due-time`'s hour check
held only where host zone == device zone. `apply_sql` already ran fixtures in a child
interpreter with TZ set; `query_db` now goes through the same helper.

Nothing here touches a device: adb is a dict of files plus a scripted `getprop`.
"""

from __future__ import annotations

import pytest

from qualgentbench import episode_runner as er
from qualgentbench.verify import device_oracle

from test_device_setup_sql import _Device, _seed_db

# 2023-11-14T22:13:20Z: already the 15th at UTC+14 (Kiritimati), still the 14th at
# UTC-11 (Pago Pago) — the same instant, two device days.
EPOCH = 1700000000
LOCAL_DAY = {"db": "main.db", "query": f"select date({EPOCH}, 'unixepoch', 'localtime')"}
UTC_DAY = {"db": "main.db", "query": f"select date({EPOCH}, 'unixepoch')"}
ZONES = {"east": "Pacific/Kiritimati", "west": "Pacific/Pago_Pago"}


@pytest.fixture
def asked(monkeypatch, tmp_path):
    """A fake device: the seed database behind `run-as cat`, and `getprop
    persist.sys.timezone` answered per serial from ZONES (empty for an unknown one).
    Yields the list of serials whose zone was asked for."""
    dev = _Device(_seed_db(tmp_path))
    asked: list[str] = []

    def adb(serial, *args, timeout=30):
        if args[:2] == ("shell", "getprop"):
            assert args[2] == "persist.sys.timezone"
            asked.append(serial)
            return 0, ZONES.get(serial, "") + "\n", ""
        return dev.adb(serial, *args, timeout=timeout)

    monkeypatch.setattr(device_oracle, "_adb", adb)
    monkeypatch.setattr(device_oracle, "_adb_bytes", dev.adb_bytes)
    monkeypatch.setattr(device_oracle, "_SETTLE_S", (0, 0))
    monkeypatch.setattr(device_oracle, "_zone_cache", {})
    return asked


def test_localtime_in_an_oracle_follows_the_device_zone(asked):
    assert device_oracle.query_db(LOCAL_DAY, "com.app", "east") == ("2023-11-15", "ok")
    assert device_oracle.query_db(LOCAL_DAY, "com.app", "west") == ("2023-11-14", "ok")


def test_a_query_without_localtime_is_unaffected(asked):
    assert device_oracle.query_db(UTC_DAY, "com.app", "east") == ("2023-11-14", "ok")
    assert device_oracle.query_db(UTC_DAY, "com.app", "west") == ("2023-11-14", "ok")


def test_the_zone_is_read_once_per_serial(asked):
    device_oracle.query_db(LOCAL_DAY, "com.app", "east")
    device_oracle.query_db(UTC_DAY, "com.app", "east")
    assert asked == ["east"]
    device_oracle.query_db(LOCAL_DAY, "com.app", "west")
    assert asked == ["east", "west"]
    assert device_oracle.device_timezone("east") == "Pacific/Kiritimati"


def test_an_empty_property_falls_back_to_the_harness_pin(asked):
    """One pin for staging and for the oracle: `episode_runner.DEVICE_TIMEZONE` IS the
    oracle module's constant. 1970-01-01T00:00Z is 18:00 the evening before in Chicago."""
    assert er.DEVICE_TIMEZONE is device_oracle.DEVICE_TIMEZONE == "America/Chicago"
    hour = {"db": "main.db", "query": "select strftime('%H', datetime(0, 'unixepoch', 'localtime'))"}
    assert device_oracle.query_db(hour, "com.app", "unpinned") == ("18", "ok")
    assert asked == ["unpinned"]
    assert device_oracle.device_timezone("unpinned") == "America/Chicago"


def test_a_garbage_property_is_not_a_zone(asked, monkeypatch):
    monkeypatch.setattr(device_oracle, "_adb",
                        lambda serial, *args, timeout=30: (0, "error: device offline", ""))
    assert device_oracle.device_timezone("x") == device_oracle.DEVICE_TIMEZONE


def test_a_bad_query_is_still_reported_as_a_db_oracle_error(asked):
    value, detail = device_oracle.query_db({"db": "main.db", "query": "select v from nope"},
                                           "com.app", "east")
    assert value is None and detail.startswith("db oracle error: no such table: nope")
    # The value path still returns the first column as text, "" for no row.
    assert device_oracle.query_db({"db": "main.db", "query": "select v from t"}, "com.app", "east") == ("seeded", "ok")
    assert device_oracle.query_db({"db": "main.db", "query": "select v from t where 0"}, "com.app", "east") == ("", "ok")
