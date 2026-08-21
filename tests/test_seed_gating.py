"""Every seeded defect must have a QgbFlags OFF switch.

An ungated patch is welded into the APK, which silently breaks derived truth,
guided-mode isolation, and randomized active subsets.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_SPECS = Path(__file__).parents[1] / "src" / "qualgentbench" / "data" / "benchmarks"

# Seeded defects known to be ungated, pending a regate + rebuild of their APK.
# Remove an entry when its patch gains a `QgbFlags.on(...)` guard. Never add one.
_UNGATED_DEBT: dict[str, set[str]] = {}


def _replacements(bug: dict) -> str:
    """All `replace` bodies this bug injects, from `patch:` or `patches:`."""
    sites = bug.get("patches") or ([bug["patch"]] if bug.get("patch") else [])
    return "\n".join((s or {}).get("replace") or "" for s in sites)


def _specs() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(_SPECS.glob("*.yaml")):
        out.append((path.stem, yaml.safe_load(path.read_text())))
    return out


@pytest.mark.parametrize("app_id,spec", _specs(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_seeded_defect_is_gated(app_id, spec):
    debt = _UNGATED_DEBT.get(app_id, set())
    ungated = {
        bug["id"] for bug in (spec.get("bugs") or [])
        if "QgbFlags" not in _replacements(bug)
    }
    new = ungated - debt
    assert not new, (
        f"{app_id}: seeded defect(s) {sorted(new)} have no QgbFlags guard, so they "
        f"cannot be switched off. Gate the patch, or the derived-truth layer will "
        f"label the area `collateral` instead of `broken`.")
    fixed = debt - ungated
    assert not fixed, (
        f"{app_id}: {sorted(fixed)} are now gated — remove them from _UNGATED_DEBT "
        f"so the allowlist keeps shrinking.")


@pytest.mark.parametrize("app_id,spec", _specs(), ids=lambda v: v if isinstance(v, str) else "")
def test_a_gated_app_declares_its_shim(app_id, spec):
    """A patch that calls QgbFlags needs a declared `flags:` shim or it won't compile."""
    uses = any("QgbFlags" in _replacements(b) for b in (spec.get("bugs") or []))
    if uses:
        assert spec.get("flags", {}).get("file"), (
            f"{app_id}: patches call QgbFlags but the spec declares no `flags:` block, "
            f"so no shim is generated")


def test_the_gate_reads_multi_site_bugs_too():
    """Reading only `patch:` would silently exempt multi-site `patches:` bugs."""
    bug = {"id": "x", "patches": [{"file": "a", "find": "f",
                                   "replace": 'if (QgbFlags.on("x")) return'}]}
    assert "QgbFlags" in _replacements(bug)
    assert "QgbFlags" not in _replacements({"id": "y", "patches": [{"replace": "boom"}]})


def test_the_debt_list_only_covers_apps_that_exist():
    known = {p.stem for p in _SPECS.glob("*.yaml")}
    assert not set(_UNGATED_DEBT) - known


def test_the_debt_is_visible_as_a_number():
    """A count that must be edited down is harder to forget than a comment."""
    total = sum(len(v) for v in _UNGATED_DEBT.values())
    assert total == 0, (
        f"ungated seeded defects: {total} (was 0). Update this number as the debt "
        f"shrinks — it is the reminder that derived truth cannot cover those apps.")
