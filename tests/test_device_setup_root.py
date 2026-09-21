"""`device_setup: root: true` must earn its place, and must never write an app's files.

`root: true` runs `adb root`, which restarts adbd as root for the WHOLE device, so every
fixture step after it runs as uid 0. AnkiDroid's fixture carried it long after the one
root-only step it existed for (a LeakCanary `pm disable`, removed 2026-09-14). Its
`mkdir`/`cp` then created the collection directory in the app's EXTERNAL dir owned by
root, and on a device that had never had AnkiDroid installed the first launch failed
with `StorageAccessException: No write access to AnkiDroid directory` (QUA-2731's first
episode, QUA-2743). A device where the app had lived before already had the directory
with the right owner, which is why it survived.

These are spec lints over every registered app, so they need no device.
"""

from __future__ import annotations

import re

from qualgentbench import bugs

# A root fixture writing here creates app files owned by uid 0 that the app cannot use.
_APP_EXTERNAL = re.compile(r"/(?:sdcard|storage/emulated/\d+)/Android/(?:data|obb)/")
# What the shell uid genuinely cannot do. `/data/...` outside the shell's own
# /data/local/tmp is another uid's private storage — fossify-messages empties the
# telephony provider's database there. Extend this with a reason if a new fixture
# needs root for something else; do not drop the check.
_ROOT_ONLY = re.compile(r"(?<![\w/])/data/(?!local/tmp\b)\S+")


def _root_specs() -> list[dict]:
    return [s for s in bugs.load_apps() if (s.get("device_setup") or {}).get("root")]


def _steps(setup: dict) -> list[str]:
    return ([str(c) for c in setup.get("shell") or []]
            + [str(item.get("dest", "")) for item in setup.get("push") or []])


def test_no_root_fixture_writes_into_an_apps_external_dir():
    """The AnkiDroid shape exactly: a root fixture that creates files under
    Android/data/<pkg> leaves them owned by root, and the app cannot open them."""
    bad = {s["app"]["id"]: hits for s in _root_specs()
           if (hits := [step for step in _steps(s["device_setup"])
                        if _APP_EXTERNAL.search(step)])}
    assert not bad, (
        f"a `root: true` fixture writes into an app's external dir, so the files are "
        f"owned by root and the app cannot use them on a fresh install: {bad}")


def test_root_is_declared_only_where_a_step_needs_it():
    """`root: true` is a device-wide side effect, not documentation. Every fixture
    that asks for it must contain a step the shell uid could not run."""
    vestigial = [s["app"]["id"] for s in _root_specs()
                 if not any(_ROOT_ONLY.search(step) for step in _steps(s["device_setup"]))]
    assert not vestigial, (
        f"`root: true` with no step that needs root: {vestigial}. Drop it, or extend "
        f"_ROOT_ONLY with the step that needs it and why.")


def test_the_lints_see_the_one_fixture_that_does_need_root():
    """Guard against the lints above passing vacuously: fossify-messages empties the
    telephony provider's database, which only root can touch."""
    ids = [s["app"]["id"] for s in _root_specs()]
    assert "fossify-messages" in ids
    messages = next(s for s in _root_specs() if s["app"]["id"] == "fossify-messages")
    assert any(_ROOT_ONLY.search(step) for step in _steps(messages["device_setup"]))
    assert "ankidroid" not in ids
