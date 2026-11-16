"""`scripts/pre_derive_check.py` (QUA-3451): the three device-free gates, in order,
stopping at the first failure."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from qualgentbench import corpus

_PATH = Path(__file__).parents[1] / "scripts" / "pre_derive_check.py"
_spec = importlib.util.spec_from_file_location("pre_derive_check", _PATH)
pre = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pre)


class _Runner:
    def __init__(self, codes: dict[str, int] | None = None):
        self.codes = codes or {}
        self.ran: list[list[str]] = []

    def __call__(self, argv: list[str]) -> int:
        self.ran.append(argv)
        return self.codes.get(Path(argv[0]).name, 0)


@pytest.fixture
def app() -> str:
    return corpus.public_apps()[0]


def test_the_gates_and_their_order(app):
    gates = pre.gates([app, "other"])
    assert [n for n, _ in gates] == ["lint_journey_cases", "lint_create_briefs", "holdout verify"]
    assert [Path(a[0]).name for _, a in gates] == \
        ["lint_journey_cases.py", "lint_create_briefs.py", "holdout.py"]
    assert gates[0][1][1:] == ["--app", f"{app},other"]
    assert gates[1][1][1:] == ["--app", f"{app},other"]
    assert gates[2][1][1:] == ["verify"]
    for _, argv in gates:
        assert Path(argv[0]).is_file()


def test_all_green_passes(app, capsys):
    runner = _Runner()
    assert pre.main(["--app", app], runner=runner) == 0
    assert len(runner.ran) == 3
    assert "PRE-DERIVE PASS" in capsys.readouterr().out


def test_stops_at_the_first_failure_with_its_exit_code(app, capsys):
    runner = _Runner({"lint_journey_cases.py": 1})
    assert pre.main(["--app", app], runner=runner) == 1
    assert len(runner.ran) == 1
    assert "PRE-DERIVE FAIL: lint_journey_cases (exit 1)" in capsys.readouterr().out


def test_a_later_failure_stops_there(app):
    runner = _Runner({"lint_create_briefs.py": 1})
    assert pre.main(["--app", app], runner=runner) == 1
    assert [Path(a[0]).name for a in runner.ran] == \
        ["lint_journey_cases.py", "lint_create_briefs.py"]


def test_keep_going_runs_every_gate(app, capsys):
    runner = _Runner({"lint_journey_cases.py": 1, "holdout.py": 3})
    assert pre.main(["--app", app, "--keep-going"], runner=runner) == 1
    assert len(runner.ran) == 3
    out = capsys.readouterr().out
    assert "lint_journey_cases (exit 1)" in out and "holdout verify (exit 3)" in out


def test_an_unknown_app_is_refused_before_any_gate(capsys):
    runner = _Runner()
    assert pre.main(["--app", "zz-no-such-app"], runner=runner) == 2
    assert runner.ran == []
    assert "no test-case file for zz-no-such-app" in capsys.readouterr().out


def test_the_real_gates_pass_on_a_public_app(app, capsys):
    """End to end on the packaged corpus: each gate as its own process."""
    assert pre.main(["--app", app]) == 0
    assert "PRE-DERIVE PASS" in capsys.readouterr().out
