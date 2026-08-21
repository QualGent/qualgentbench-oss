"""Independent, no-root task verification (adapted from LlamaTouch). The agent's
self-report is never trusted for scoring: the harness reads real device state and
matches it against a per-task Spec; the self-report is kept only as metadata."""

from .match import activity_matches, find_center, node_present, parse_vh
from .specs import Spec, VerifyResult, get_spec, run_spec

__all__ = [
    "Spec",
    "VerifyResult",
    "get_spec",
    "run_spec",
    "node_present",
    "find_center",
    "activity_matches",
    "parse_vh",
]
