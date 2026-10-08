"""`journey.SCORER_VERSION` is pinned to the scoring source (QUA-2927).

The hash covers the source of the three functions that decide a journey verdict
(`journey_verdict`, `match_report`, `_witness`) and the credit-rule constants they read.
Editing any of them without bumping SCORER_VERSION fails here, so a published number can
always be labelled with the scorer that produced it. To update: bump SCORER_VERSION when
the edit can move a verdict (add a history line beside it), then record the new hash in
`tests/data/scorer_pin.txt` under the new version. An edit that cannot move a verdict (a
comment, a docstring) refreshes the current version's hash without a bump.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from pathlib import Path

from qualgentbench import journey

PIN = Path(__file__).parent / "data" / "scorer_pin.txt"

#: The scorer's functions, in hash order.
FUNCTIONS = ("journey_verdict", "match_report", "_witness")
#: The credit-rule constants those functions read, in hash order: the evidence floor and
#: the space fold every screen match goes through, the RESULT line and report-tool
#: verdicts, the device-oracle modes, the refused-reply envelope, and the predicates that
#: decide which device text can carry a witness.
CONSTANTS = ("VERSIONS", "DEVICE_ORACLE_MODES", "_MIN_EVIDENCE_CHARS", "_SPACE_RUN_RE",
             "_RESULT_RE", "_REPORT_TOOL_VERDICTS", "_REFUSED_REPLY_RE", "_RAW_OBSERVE_RE",
             "_STATUS_ONLY_RE", "_RAW_SCREEN_READ_RE", "_HIERARCHY_ATTR_RE")

FAILURE = "scoring source changed: bump SCORER_VERSION and refresh the pin"


def _constant(value: object) -> str:
    # A compiled pattern's repr truncates long patterns; hash the whole pattern + flags.
    if isinstance(value, re.Pattern):
        return repr((value.pattern, int(value.flags)))
    return repr(value)


def scoring_source_sha256(module=journey) -> str:
    h = hashlib.sha256()
    for name in FUNCTIONS:
        h.update(inspect.getsource(getattr(module, name)).encode())
    for name in CONSTANTS:
        h.update(f"{name}={_constant(getattr(module, name))}\n".encode())
    return h.hexdigest()


def read_pin(path: Path = PIN) -> dict[int, str]:
    """`<version> <sha256>` per line; `#` starts a comment."""
    pins: dict[int, str] = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            version, sha = line.split()
            pins[int(version)] = sha
    return pins


def test_scorer_version_is_pinned_to_the_scoring_source():
    pins = read_pin()
    assert journey.SCORER_VERSION in pins, (
        f"SCORER_VERSION {journey.SCORER_VERSION} has no pin in {PIN.name}: record "
        f"'{journey.SCORER_VERSION} {scoring_source_sha256()}'")
    actual = scoring_source_sha256()
    assert actual == pins[journey.SCORER_VERSION], f"{FAILURE} (now {actual})"


def test_versions_only_grow():
    pins = read_pin()
    assert journey.SCORER_VERSION == max(pins), "SCORER_VERSION is behind the newest pin"


def test_an_edit_to_match_report_changes_the_hash(monkeypatch):
    """The guard is live: a one-character edit to the matcher's source moves the hash."""
    src = inspect.getsource(journey.match_report)
    edited = src.replace("return blocking", "return blockinG", 1)
    assert edited != src and len(edited) == len(src)
    real = inspect.getsource

    def fake(obj):
        return edited if obj is journey.match_report else real(obj)

    before = scoring_source_sha256()
    monkeypatch.setattr(inspect, "getsource", fake)
    assert scoring_source_sha256() != before


def test_a_constant_edit_changes_the_hash(monkeypatch):
    before = scoring_source_sha256()
    monkeypatch.setattr(journey, "_MIN_EVIDENCE_CHARS", journey._MIN_EVIDENCE_CHARS + 1)
    assert scoring_source_sha256() != before


def test_every_verdict_carries_the_scorer_version():
    from test_journey import _spec, _task
    v = journey.journey_verdict("", "m", _task(_spec()))
    assert v.metrics["scorer_version"] == journey.SCORER_VERSION
