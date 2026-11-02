"""The device class: which class of device an episode ran on, and the set it files under.

* `QGB_DEVICE_CLASS` names the class; unset or empty is the legacy class the corpus
  truth was derived on. A malformed value is refused before anything runs.
* Every result.json records it as `provenance.device_class`.
* The view's manifest lists it in `versions`, and a journey run on any class but the
  legacy one gets `-d<class>` appended to its `set_key`. A run with no recorded class
  (written before the stamp) or on the legacy class keeps the key it always had, so
  sets that already exist never regroup.
* Two classes in one run are `mixed`: their results never pool.

Nothing here reaches a device or a model.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import click
import pytest
import test_view as tv

from qualgentbench import cli, view
from qualgentbench import episode_runner as er
from qualgentbench.result import (
    DEVICE_CLASS_ENV,
    LEGACY_DEVICE_CLASS,
    device_class,
    device_class_of,
    is_device_class,
)
from qualgentbench.schemas import Condition

OTHER = "linux-x86_64-api35-gapis"
#: The set key `tv._stamped`'s run has always had, written out: the format is the
#: contract with every published set, so it is pinned literally, not rebuilt.
LEGACY_KEY = "j-aaaaaaaaaaaa-cccccccccccc-b3"


# ── reading the class ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_or_empty_is_the_legacy_class(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv(DEVICE_CLASS_ENV, raising=False)
    else:
        monkeypatch.setenv(DEVICE_CLASS_ENV, raw)
    assert device_class() == LEGACY_DEVICE_CLASS
    cli._gate_device_class()                      # nothing to refuse


@pytest.mark.parametrize("raw", [OTHER, f"  {OTHER}\n", LEGACY_DEVICE_CLASS, "x"])
def test_a_named_class_is_read_as_given(monkeypatch, raw):
    monkeypatch.setenv(DEVICE_CLASS_ENV, raw)
    assert device_class() == raw.strip()
    cli._gate_device_class()


@pytest.mark.parametrize("raw", ["Linux-X86_64", "a/b", "../x", "-x", "x-", "_x", "a b",
                                 "a.b", "x" * 65, "café"])
def test_a_malformed_class_is_refused_before_anything_runs(monkeypatch, raw):
    monkeypatch.setenv(DEVICE_CLASS_ENV, raw)
    assert not is_device_class(raw)
    with pytest.raises(ValueError, match=DEVICE_CLASS_ENV):
        device_class()
    with pytest.raises(click.ClickException, match=DEVICE_CLASS_ENV):
        cli._gate_device_class()


def test_a_result_without_a_class_reads_as_the_legacy_class():
    assert device_class_of(None) == device_class_of({}) == LEGACY_DEVICE_CLASS
    assert device_class_of({"device_class": None}) == LEGACY_DEVICE_CLASS
    assert device_class_of({"device_class": OTHER}) == OTHER
    assert is_device_class(LEGACY_DEVICE_CLASS) and is_device_class("x" * 64)


async def test_provenance_records_the_class(monkeypatch):
    async def no_avd(serial):
        return None

    monkeypatch.setattr(er, "_avd_name", no_avd)
    monkeypatch.setattr(er, "harness_identity", lambda: {"package_version": "0"})
    opts = er.EpisodeOptions(agent="codex-cli", model="m", condition=Condition.no_routines,
                             trial=1, mcp_server="", runs_dir=Path("runs"))
    monkeypatch.delenv(DEVICE_CLASS_ENV, raising=False)
    assert (await er._provenance(opts, "emulator-5554"))["device_class"] == \
        LEGACY_DEVICE_CLASS
    monkeypatch.setenv(DEVICE_CLASS_ENV, OTHER)
    assert (await er._provenance(opts, "emulator-5554"))["device_class"] == OTHER


async def test_a_malformed_class_refuses_the_episode_before_the_device(monkeypatch, tmp_path):
    """`run_episode` checks the class before it asks for a device, so a script that
    runs episodes without `run`'s gate spends nothing on a class it cannot record."""
    from types import SimpleNamespace

    asked = []

    class Session:
        def __init__(self, *a, **k):
            pass

        async def first_available_device(self):
            asked.append(True)

    monkeypatch.setattr(er, "DeviceSession", Session)
    monkeypatch.setenv(DEVICE_CLASS_ENV, "Not A Class")
    opts = er.EpisodeOptions(agent="codex-cli", model="m", condition=Condition.no_routines,
                             trial=1, mcp_server="", runs_dir=tmp_path)
    with pytest.raises(ValueError, match=DEVICE_CLASS_ENV):
        await er.run_episode(SimpleNamespace(bundle_id="com.example"), opts)
    assert not asked


# ── the set key ────────────────────────────────────────────────────────────────

def _stamp(runs: Path, classes: list[str | None]) -> None:
    """Write `classes` (in sorted result.json order) into the episodes' provenance;
    None leaves an episode as it was written before the stamp."""
    paths = sorted(runs.rglob("result.json"))
    assert len(paths) == len(classes)
    for path, dc in zip(paths, classes):
        if dc is None:
            continue
        doc = json.loads(path.read_text())
        doc["provenance"]["device_class"] = dc
        path.write_text(json.dumps(doc))


def _index_from(full: view.ViewResult, copy: Path) -> None:
    """Rebuild `full`'s index from its summaries alone and check it matches byte for
    byte (the manifest up to `generated_at`)."""
    (copy / "ep").mkdir(parents=True)
    for p in (full.out_dir / "ep").glob("*.json"):
        shutil.copyfile(p, copy / "ep" / p.name)
    shutil.copyfile(full.out_dir / view.RUN_STATE, copy / view.RUN_STATE)
    view.build_index(copy)
    assert (copy / "index.html").read_bytes() == full.index.read_bytes()
    a, b = ((d / view.MANIFEST).read_text().splitlines() for d in (copy, full.out_dir))
    assert [x for x in a if '"generated_at"' not in x] == [
        x for x in b if '"generated_at"' not in x]


def test_a_run_written_before_the_stamp_keeps_its_set_key(tmp_path):
    """No episode records a class: the run is the legacy class and its key is the one
    it always had; stamping the legacy class explicitly changes neither the key nor the
    page."""
    runs = tv._stamped(tmp_path / "runs")
    before = tv._build(runs, out=runs / "_runs" / "_before")
    run = tv._run_entry(before)
    v = run["versions"]
    assert run["set_key"] == v["set_key"] == LEGACY_KEY
    assert v["device_class"] == LEGACY_DEVICE_CLASS
    assert v["device_classes"] == [LEGACY_DEVICE_CLASS]
    assert v["device_class_unstamped"] == 3 and v["mixed"] is False
    _index_from(before, tmp_path / "before_copy")

    _stamp(runs, [LEGACY_DEVICE_CLASS] * 3)
    after = tv._build(runs, out=runs / "_runs" / "_after")
    run2 = tv._run_entry(after)
    assert run2["set_key"] == run2["versions"]["set_key"] == LEGACY_KEY
    assert run2["versions"]["device_class_unstamped"] == 0
    assert after.index.read_bytes() == before.index.read_bytes()
    assert {k: x for k, x in run2["versions"].items() if k != "device_class_unstamped"} \
        == {k: x for k, x in v.items() if k != "device_class_unstamped"}


def test_a_run_on_another_class_forms_its_own_set(tmp_path):
    runs = tv._stamped(tmp_path / "runs")
    _stamp(runs, [OTHER] * 3)
    full = tv._build(runs, out=runs / "_runs" / "_full")
    run = tv._run_entry(full)
    v = run["versions"]
    assert run["set_key"] == v["set_key"] == f"{LEGACY_KEY}-d{OTHER}"
    assert v["device_class"] == OTHER and v["device_classes"] == [OTHER]
    assert v["device_class_unstamped"] == 0 and v["mixed"] is False
    line = tv._versions_line(full.index.read_text())
    assert f"set {LEGACY_KEY}-d{OTHER}" in line
    assert "MIXED" not in full.index.read_text()
    _index_from(full, tmp_path / "copy")


def test_two_classes_in_one_run_are_mixed_and_file_under_no_set(tmp_path):
    """One episode on another class beside two written before the stamp (the legacy
    class): one run, two measurements."""
    runs = tv._stamped(tmp_path / "runs")
    _stamp(runs, [OTHER, None, None])
    full = tv._build(runs, out=runs / "_runs" / "_full")
    run = tv._run_entry(full)
    v = run["versions"]
    assert v["mixed"] is True and run["set_key"] is None and v["set_key"] is None
    assert v["device_class"] is None
    assert v["device_classes"] == sorted([OTHER, LEGACY_DEVICE_CLASS])
    assert v["device_class_unstamped"] == 2
    line = tv._versions_line(full.index.read_text())
    assert "MIXED" in line
    assert f"device class: {', '.join(sorted([OTHER, LEGACY_DEVICE_CLASS]))}" in line
    _index_from(full, tmp_path / "copy")


def test_a_recorded_class_that_is_not_a_class_name_files_under_no_set(tmp_path):
    runs = tv._stamped(tmp_path / "runs")
    _stamp(runs, ["Bad/Class"] * 3)
    run = tv._run_entry(tv._build(runs, out=runs / "_runs" / "_full"))
    assert run["versions"]["device_class"] == "Bad/Class"
    assert run["versions"]["mixed"] is False and run["set_key"] is None


def test_set_key_components():
    base = {"mode": "journey", "mixed": False, "corpus": "c0", "heldout": None,
            "brief": "3"}
    assert view._set_key(base) == "j-c0-none-b3"                  # a dict from before
    assert view._set_key({**base, "device_class": None}) == "j-c0-none-b3"
    assert view._set_key({**base, "device_class": LEGACY_DEVICE_CLASS}) == "j-c0-none-b3"
    assert view._set_key({**base, "device_class": OTHER}) == f"j-c0-none-b3-d{OTHER}"
    assert view._set_key({**base, "device_class": OTHER, "mixed": True}) is None
