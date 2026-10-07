"""Every journey defect carries an attribution canary (QUA-2860).

A seeded patch that calls `QgbFlags.fired("<id>")` leaves `files/.qgb/fired/<id>` in
the app sandbox when its seeded branch runs (`scripts/build_app.py` generates the shim,
`verify/canary.py` reads it back). That marker is what lets a grader attribute a FAIL
to the target defect by construction instead of by reading screens or stacks, so a
journey defect without one cannot earn attributed credit. Until 2026-09-30 canaries were
added opportunistically: 23 of the 38 public journey defects had one. These tests make
the rule structural: a new defect in any `test-cases/<app>.yaml` fails here until its
patch reports itself.

Device-free: the assertions read the specs' patch text, not a build.
"""

from __future__ import annotations

import re

import pytest
import yaml

from qualgentbench import corpus, journey

_FIRED = re.compile(r'([\w.]+)\.QgbFlags\.fired\("([^"]+)"\)')
_ON = re.compile(r'QgbFlags\.on\("([^"]+)"\)')


def _journey_apps() -> list[str]:
    """Public journey apps, plus the held-out split when one is configured."""
    return sorted(set(corpus.public_apps()) | set(corpus.heldout_apps()))


def _spec(app_id: str) -> dict:
    return yaml.safe_load(corpus.spec_path(app_id).read_text())


def _sites(bug: dict) -> list[dict]:
    return list(bug.get("patches") or ([bug["patch"]] if bug.get("patch") else []))


def _all_specs() -> list[tuple[str, dict]]:
    return [(p.stem, yaml.safe_load(p.read_text())) for p in corpus.spec_paths()]


def test_there_are_journey_apps():
    # A glob that silently matched nothing would make every test below vacuous.
    assert len(corpus.public_apps()) >= 6


@pytest.mark.parametrize("app_id", _journey_apps())
def test_every_journey_defect_has_a_fired_canary(app_id):
    doc = journey.load_cases(app_id)
    assert doc, f"{app_id}: no test-case file"
    bugs = {str(b["id"]): b for b in _spec(app_id).get("bugs", [])}
    missing = []
    for defect_id in journey.load_defects(doc):
        bug = bugs.get(defect_id)
        assert bug is not None, f"{app_id}: journey defect {defect_id!r} has no bug in the spec"
        fired = {i for s in _sites(bug) for _, i in _FIRED.findall(str(s.get("replace", "")))}
        if defect_id not in fired:
            missing.append(defect_id)
    assert not missing, (
        f"{app_id}: journey defect(s) {missing} never call QgbFlags.fired(\"<id>\"). Add the "
        f"call inside the defect's own flag branch, on the line before the fault takes "
        f"effect, and only on the path that faults (see the canary notes in the spec)")


@pytest.mark.parametrize("app_id,spec", _all_specs(), ids=lambda v: v if isinstance(v, str) else "")
def test_a_patch_only_fires_its_own_id(app_id, spec):
    """A marker names the seeded site that ran. A patch that fired another bug's id
    would make `gate_crash` / `derive_journey` blame (or clear) the wrong defect."""
    for bug in spec.get("bugs", []):
        for site in _sites(bug):
            for _, fired_id in _FIRED.findall(str(site.get("replace", ""))):
                assert fired_id == str(bug["id"]), (
                    f"{app_id}: bug {bug['id']!r} fires {fired_id!r} in {site.get('file')}")


@pytest.mark.parametrize("app_id,spec", _all_specs(), ids=lambda v: v if isinstance(v, str) else "")
def test_fired_calls_the_generated_shim_inside_the_flag_gate(app_id, spec):
    """The call must name the shim's package (the generated `QgbFlags` lives there and
    patches sit in other packages), and the patch site must also read the defect's own
    flag: a canary outside the gate would write a marker on the CLEAN arm, which the
    harness treats as a broken flag gate."""
    pkg = str((spec.get("flags") or {}).get("package") or "")
    for bug in spec.get("bugs", []):
        for site in _sites(bug):
            replace = str(site.get("replace", ""))
            calls = _FIRED.findall(replace)
            if not calls:
                continue
            assert pkg, f"{app_id}: bug {bug['id']!r} calls fired() but the spec has no flags: block"
            for called_pkg, _ in calls:
                assert called_pkg == pkg, (
                    f"{app_id}: bug {bug['id']!r} calls {called_pkg}.QgbFlags, the shim is {pkg}")
            assert str(bug["id"]) in _ON.findall(replace), (
                f"{app_id}: bug {bug['id']!r} calls fired() in {site.get('file')} at a site "
                f"that does not read its own flag")
