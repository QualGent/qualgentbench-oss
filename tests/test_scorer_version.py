"""`journey.SCORER_VERSION` is pinned to the scoring source (QUA-2927, QUA-2934).

The hash (`scorer_closure.closure_sha256`) covers the transitive closure of
`journey_verdict` through the scoring modules (journey, bugs, contamination,
interactions): every function and class it can reach there, and every module-level
constant those read, in any package module. Each is hashed as its AST with docstrings
removed, in a form that is identical under Python 3.11-3.14, so a comment, docstring or
formatting edit never moves it and a code or constant edit always does. `scorer_closure.py`
documents what is NOT followed (the transcript parser and other non-scoring modules).

Editing the closure without bumping SCORER_VERSION fails here, so a published number can
always be labelled with the scorer that produced it. To update: bump SCORER_VERSION when
the edit can move a verdict (add a history line beside it), then record the new hash in
`tests/scorer_pin.txt` under the new version. An edit that cannot move a verdict (a
renamed local, a refactor) refreshes the current version's hash without a bump.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import scorer_closure
from scorer_closure import SRC, Closure, closure_sha256, render
from qualgentbench import journey

PIN = Path(__file__).parent / "scorer_pin.txt"   # not under a `data/` dir: .gitignore ignores those

FAILURE = "scoring source changed: bump SCORER_VERSION and refresh the pin"


def read_pin(path: Path = PIN) -> dict[int, str]:
    """`<version> <sha256>` per line; `#` starts a comment."""
    pins: dict[int, str] = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            version, sha = line.split()
            pins[int(version)] = sha
    return pins


def _source(module: str) -> str:
    return (SRC / f"{module}.py").read_text(encoding="utf-8")


def _edited(module: str, old: str, new: str) -> str:
    """The hash with one exact, unique edit applied to `module`'s source."""
    src = _source(module)
    assert src.count(old) == 1, f"edit anchor not unique in {module}.py: {old!r}"
    return closure_sha256(sources={module: src.replace(old, new)})


def test_scorer_version_is_pinned_to_the_scoring_source():
    pins = read_pin()
    actual = closure_sha256()
    assert journey.SCORER_VERSION in pins, (
        f"SCORER_VERSION {journey.SCORER_VERSION} has no pin in {PIN.name}: record "
        f"'{journey.SCORER_VERSION} {actual}'")
    assert actual == pins[journey.SCORER_VERSION], f"{FAILURE} (now {actual})"


def test_versions_only_grow():
    pins = read_pin()
    assert journey.SCORER_VERSION == max(pins), "SCORER_VERSION is behind the newest pin"


def test_the_closure_reaches_the_helpers_and_the_other_scoring_modules():
    """The rules the v1 history names live outside journey.py; they must be hashed."""
    items = set(Closure().items())
    for want in [("journey", n) for n in (
                     "journey_verdict", "match_report", "_witness", "_refused_reply",
                     "_quote_rules_out", "_witness_capable", "_evidence", "parse_report",
                     "_device_text", "_device_texts", "_observation_texts", "defect_stamp",
                     "report_from_tool", "Report", "BugReport", "_MIN_EVIDENCE_CHARS",
                     "_SPACE_RUN_RE", "_REFUSED_REPLY_RE", "DEVICE_ORACLE_MODES")] + [
                 ("interactions", "mcp_echoes_argument"), ("interactions", "mcp_reads"),
                 ("interactions", "SCREEN"), ("interactions", "MCP_TOOL_RULES"),
                 ("bugs", "_ordered_stream"), ("bugs", "_device_actions"),
                 ("bugs", "_count_tool_calls"), ("contamination", "scan"),
                 ("contamination", "_misfiled_self_path"),
                 ("contamination", "MISFILED_MAX_EDITS"),
                 # a constant another module defines, read through DEVICE_ORACLE_MODES
                 ("submission", "LIVENESS_MODES")]:
        assert want in items, f"{'.'.join(want)} is not pinned"
    # The version label is read by the scorer but is not a rule.
    assert ("journey", "SCORER_VERSION") not in items
    # Board code is not scoring code.
    assert ("journey", "summary") not in items


def test_a_helper_edit_in_another_scoring_module_trips_the_pin():
    before = closure_sha256()
    # contamination.py: the misfiled-write rule's edit distance.
    assert _edited("contamination", "prev[j - 1] + (ca != cb)",
                   "prev[j - 1] + 2 * (ca != cb)") != before
    assert _edited("contamination", "MISFILED_MAX_EDITS = 3",
                   "MISFILED_MAX_EDITS = 4") != before
    # interactions.py: the echo rule.
    assert _edited("interactions", "    return bool(rule and rule.echo)\n",
                   "    return bool(rule)\n") != before
    # A rule table defined in a non-scoring module but read by the scorer.
    assert _edited("submission", 'LIVENESS_MODES = ("crash", "anr", "stuck")',
                   'LIVENESS_MODES = ("crash", "anr")') != before


def test_an_edit_to_match_report_changes_the_hash():
    assert _edited("journey", "return blocking", "return blockinG") != closure_sha256()


def test_comments_docstrings_and_formatting_do_not_trip_the_pin():
    before = closure_sha256()
    # A comment inside a reached helper.
    assert _edited("contamination", "def _edits(a: str, b: str) -> int:\n",
                   "def _edits(a: str, b: str) -> int:\n    # Levenshtein, one row.\n") == before
    # A docstring rewritten.
    assert _edited("interactions",
                   '    """Is this tool\'s REPLY its own argument handed back (text entry)? Such a reply is\n',
                   '    """Reworded: is the reply the argument handed back? Such a reply is\n') == before
    # Formatting: blank lines and a re-wrapped call.
    assert _edited("contamination", "        prev = cur\n    return prev[-1]\n",
                   "        prev = cur\n\n\n    return (\n        prev[-1]\n    )\n") == before
    # A function outside the closure.
    assert _edited("journey", "def summary(results, by_app: bool = False)",
                   "def summary(results, by_app: bool = True)") == before


def test_rendering_ignores_fields_a_newer_python_adds_empty():
    """3.12 added `type_params` to every def and 3.13 changed what `ast.dump` prints for
    empty fields; `render` omits None/empty fields and orders the rest by name."""
    node = ast.parse("def f(a, *, b=1):\n    return a + b\n").body[0]
    base = render(node)
    grown = type("FunctionDef", (ast.AST,), {"_fields": ("zz_new", *node._fields, "type_params")})()
    for name in node._fields:
        setattr(grown, name, getattr(node, name))
    grown.zz_new, grown.type_params = None, []
    assert render(grown) == base
    # A pinned rendering: changes here are a change of the hash format for every pin.
    assert render(ast.parse("x = f'{a!r:>{w}}' + 1").body[0]) == (
        "Assign(targets=[Name(ctx=Store(), id=str:'x')], value=BinOp(left=JoinedStr("
        "values=[FormattedValue(conversion=int:114, format_spec=JoinedStr(values=["
        "Constant(value=str:'>'), FormattedValue(conversion=int:-1, value=Name(ctx=Load(), "
        "id=str:'w'))]), value=Name(ctx=Load(), id=str:'a'))]), op=Add(), "
        "right=Constant(value=int:1)))")


_OTHER_PYTHONS = [p for p in (shutil.which(f"python3.{m}") for m in (11, 12, 13, 14)) if p]


@pytest.mark.skipif(not _OTHER_PYTHONS, reason="no python3.11-3.14 on PATH")
def test_the_hash_is_the_same_under_every_installed_python():
    """The closure is computed from source with the stdlib alone, so every interpreter
    must agree with this one."""
    here = closure_sha256()
    for py in _OTHER_PYTHONS:
        out = subprocess.run([py, "-I", scorer_closure.__file__], capture_output=True,
                             text=True, timeout=120)
        assert out.returncode == 0, f"{py}: {out.stderr}"
        version, sha = out.stdout.split()[-2:]
        assert sha == here, f"python {version} ({py}) hashes {sha}, {sys.version.split()[0]} {here}"


def test_every_verdict_carries_the_scorer_version():
    from test_journey import _spec, _task
    v = journey.journey_verdict("", "m", _task(_spec()))
    assert v.metrics["scorer_version"] == journey.SCORER_VERSION
