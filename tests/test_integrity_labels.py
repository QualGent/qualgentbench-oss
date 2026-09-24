"""The board's `Episode integrity:` note names every void kind (QUA-2816).

`INTEGRITY_LABELS` once listed three kinds by hand, and the two hard contamination kinds
QUA-2804 added — `adb_server_bypass` and `flag_nonce` — voided episodes the note never
named: a row read `14/16` with no word on why. `integrity_kinds` now reads the kinds off
the episode's own `contamination_reasons`, so an unlabelled kind still shows, and the
test below reads the HARD kinds straight out of `contamination.scan`'s source, so a kind
added to the scan without a label fails the suite.
"""

from __future__ import annotations

import ast
import inspect

import pytest
from test_journey import _rr

from qualgentbench import contamination, failures, journey


def _scan_hard_kinds() -> set[str]:
    """Every `kind` `contamination.scan` can append to `report.hard`: a literal
    `{"kind": "<k>"}`, or a `kind` variable guarded by `if kind in (<k>, …)`."""
    tree = ast.parse(inspect.getsource(contamination.scan))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    kinds: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and ast.unparse(node.func.value) == "report.hard"):
            continue
        (hit,) = node.args
        assert isinstance(hit, ast.Dict), ast.unparse(node)
        value = {ast.literal_eval(k): v for k, v in zip(hit.keys, hit.values)}["kind"]
        if isinstance(value, ast.Constant):
            kinds.add(value.value)
            continue
        # `{"kind": kind}`: the enclosing `if kind in (...)` names the kinds it can be.
        up = parents.get(node)
        while up is not None and not (
                isinstance(up, ast.If) and isinstance(up.test, ast.Compare)
                and ast.unparse(up.test.left) == ast.unparse(value)
                and isinstance(up.test.ops[0], ast.In)):
            up = parents.get(up)
        assert up is not None, f"cannot tell which kinds {ast.unparse(node)} appends"
        kinds |= set(ast.literal_eval(up.test.comparators[0]))
    return kinds


def test_the_source_reader_sees_the_known_hard_kinds():
    assert _scan_hard_kinds() >= {"adb_server_bypass", "adbd_rooted", "app_source_checkout",
                                  "benchmark_repo", "canary", "devloop_artifacts",
                                  "flag_nonce", "other_episode"}


def test_every_hard_contamination_kind_has_a_label():
    missing = _scan_hard_kinds() - set(journey.INTEGRITY_LABELS)
    assert not missing, (f"contamination.scan raises {sorted(missing)} with no "
                         "journey.INTEGRITY_LABELS entry: the board would not name it")


def test_every_integrity_exclusion_has_a_label():
    """`is_excluded`'s integrity keys (not env/infra/rate-limit, which are the run's,
    not the episode's integrity) and the flag-only kind are labelled too."""
    for kind in (failures.MCP_UNCLEAN, failures.MCP_UNVERIFIED, "contaminated"):
        assert kind in journey.INTEGRITY_LABELS


def _ep(task_id, **m):
    version = task_id.rsplit("~", 1)[1]
    return _rr(task_id, {"version": version, "completed": True, "false_reports": 0,
                         "bugs_present": ["a"] if version == "seeded" else [],
                         "bugs_found": ["a"] if version == "seeded" else [], "steps": 10,
                         "app_id": "x", "heldout": False, "corpus_version": "c" * 12, **m})


def _void(task_id, *reasons):
    return _ep(task_id, contaminated=True, contamination_reasons=list(reasons))


def test_the_board_names_adb_server_bypass_and_flag_nonce(capsys):
    from qualgentbench import cli

    rs = [_ep("c1~clean"), _ep("c1~seeded"),
          _void("c2~clean", "adb_server_bypass"), _void("c3~seeded", "adb_server_bypass"),
          _void("c4~clean", "flag_nonce")]
    (row,) = journey.summary(rs)
    assert row["excluded_episodes"] == 3
    assert row["integrity_flags"] == {"adb_server_bypass": 2, "flag_nonce": 1}
    note = journey.integrity_note([row])
    assert note == ("Episode integrity: 2 adb server selected around the meter "
                    "(contaminated, excluded); 1 episode flag nonce reached the agent "
                    "(contaminated, excluded)")
    cli._print_journey_table(rs)
    assert note in " ".join(capsys.readouterr().out.split())     # the console wraps it


@pytest.mark.parametrize("metrics, kinds", [
    ({"contaminated": True, "contamination_reasons": ["flag_nonce", "adbd_rooted"]},
     ["adbd_rooted", "flag_nonce"]),
    ({"contaminated": True, "contamination_reasons": []}, ["contaminated"]),
    ({"contaminated": True}, ["contaminated"]),
    ({"contaminated": False, "contamination_reasons": [], "mcp_unclean": True}, ["mcp_unclean"]),
    ({"contaminated": False, "mcp_isolation_unverified": True}, ["mcp_isolation_unverified"]),
    ({"contaminated": False, "contamination_reasons": []}, []),
])
def test_integrity_kinds(metrics, kinds):
    assert journey.integrity_kinds(metrics) == kinds


def test_a_kind_with_no_label_is_still_named():
    """A contamination kind added after the label table still reaches the note."""
    (row,) = journey.summary([_ep("c1~clean"), _void("c2~clean", "new_kind")])
    assert journey.integrity_note([row]) == (
        "Episode integrity: 1 new_kind (contaminated, excluded)")
