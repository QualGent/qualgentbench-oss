"""`run --system-addendum-file`: a run-wide system-prompt addendum (prompt-wording
experiments). It rides the agent's system channel, never the brief, and is recorded."""
from __future__ import annotations

from types import SimpleNamespace

import click
import pytest

from qualgentbench import cli
from qualgentbench.episode_runner import (EpisodeOptions, _developer_instructions,
                                          addendum_sha256)
from qualgentbench.lanes import LaneRun


def test_no_addendum_and_no_creation_is_none():
    assert _developer_instructions(None, None) is None
    assert _developer_instructions(None, "") is None


def test_addendum_alone_is_the_system_text():
    assert _developer_instructions(None, "Be careful.\n") == "Be careful."


def test_creation_template_comes_first_then_the_addendum():
    creation = SimpleNamespace(developer_instructions="Template.\n")
    assert _developer_instructions(creation, "Addendum.") == "Template.\n\nAddendum."
    assert _developer_instructions(creation, None) == "Template."


def test_defaults_are_off():
    assert EpisodeOptions.__dataclass_fields__["system_addendum"].default is None
    assert LaneRun.__dataclass_fields__["system_addendum"].default is None


def test_sha256_is_of_the_utf8_text():
    import hashlib
    assert addendum_sha256("é") == hashlib.sha256("é".encode()).hexdigest()


def test_gate_reads_and_strips(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("\n  Report only what you reproduced.  \n")
    assert cli._gate_system_addendum(f, "journey", None) == "Report only what you reproduced."


def test_gate_none_without_a_file():
    assert cli._gate_system_addendum(None, "journey", None) is None


def test_gate_refuses_empty_long_and_create(tmp_path):
    empty = tmp_path / "e.md"
    empty.write_text("  \n")
    with pytest.raises(click.ClickException):
        cli._gate_system_addendum(empty, "journey", None)
    long = tmp_path / "l.md"
    long.write_text("x" * (cli.SYSTEM_ADDENDUM_MAX_CHARS + 1))
    with pytest.raises(click.ClickException):
        cli._gate_system_addendum(long, "journey", None)
    ok = tmp_path / "ok.md"
    ok.write_text("text")
    with pytest.raises(click.UsageError):
        cli._gate_system_addendum(ok, "create", None)


def test_resume_takes_the_plans_text():
    plan = SimpleNamespace(raw={"system_addendum": {"text": "Frozen.", "sha256": "x"}})
    assert cli._gate_system_addendum(None, "journey", plan) == "Frozen."
    assert cli._gate_system_addendum(None, "journey", SimpleNamespace(raw={})) is None


def test_resume_refuses_the_flag():
    assert cli._SCOPE_FLAGS["system_addendum_file"] == "--system-addendum-file"


def test_condition_marker_is_stable():
    from qualgentbench.episode_runner import SYSTEM_ADDENDUM_MARKER
    assert SYSTEM_ADDENDUM_MARKER == "+sys-"
