"""The step unit: one interaction, shared by every arm, agent and model. Both meters
classify into this vocabulary: one `type` per text entry regardless of length, one
`observe` per look however many calls it took."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

TAP = "tap"
SWIPE = "swipe"
TYPE = "type"
PRESS = "press"
LAUNCH = "launch"
TERMINATE = "terminate"
OBSERVE = "observe"
OTHER = "other"

KINDS = (TAP, SWIPE, TYPE, PRESS, LAUNCH, TERMINATE, OBSERVE, OTHER)

# Operations that only read back an artifact a capture just produced — part of the
# preceding `observe`, not a look of their own.
_READBACK = ("sync:", "cat", "ls", "stat", "test")

# ── adb service string → interaction ─────────────────────────────────────────
# Ordered: the first match wins, so `input text` is checked before bare `input`.
_ADB_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\binput\s+(?:\w+\s+)?text\b"), TYPE),
    (re.compile(r"\binput\s+(?:\w+\s+)?keyevent\b"), PRESS),
    (re.compile(r"\binput\s+(?:\w+\s+)?(?:swipe|draganddrop|roll)\b"), SWIPE),
    (re.compile(r"\binput\s+(?:\w+\s+)?(?:tap|press)\b"), TAP),
    (re.compile(r"\bam\s+(?:force-stop|kill)\b"), TERMINATE),
    (re.compile(r"\bam\s+start\b"), LAUNCH),
    (re.compile(r"\bmonkey\b"), LAUNCH),
    (re.compile(r"\b(?:uiautomator\s+dump|screencap|dumpsys|getevent)\b"), OBSERVE),
)

# ── MCP tool name → interaction ──────────────────────────────────────────────
# The tool name IS the intent — a lookup, not inference. Matched on the base name.
_MCP_RULES: tuple[tuple[str, str], ...] = (
    ("mobile_type_text", TYPE),
    ("mobile_edit_field", TYPE),
    ("mobile_swipe_coordinates", SWIPE),
    ("mobile_swipe", SWIPE),
    ("mobile_press_button", PRESS),
    ("mobile_long_press", TAP),
    ("mobile_double_tap", TAP),
    ("mobile_tap_and_observe", TAP),
    ("mobile_tap", TAP),
    ("mobile_launch_app", LAUNCH),
    ("mobile_terminate_app", TERMINATE),
    ("mobile_observe_screen", OBSERVE),
    ("mobile_take_screenshot", OBSERVE),
    ("mobile_dismiss_dialogs", TAP),
)

# Device-adjacent plumbing, not app interaction. qg_ is exempt everywhere —
# charging cleanup would let it eat an agent's budget.
_MCP_IGNORED = (
    "qg_", "mobile_list_", "mobile_get_screen_size", "mobile_get_orientation",
    "mobile_install_app", "mobile_uninstall_app", "mobile_insert_credential",
)


# One shell request can chain several device commands; each is its own step.
_CHAIN_RE = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
# `adb exec-out` quotes every argument on the wire (`uiautomator 'dump' '/dev/tty'`).
_QUOTES_RE = re.compile(r"""['"]""")


def classify_adb_all(request: str) -> list[str]:
    """Every interaction inside one ADB service request — one adb command = one step,
    however the agent batches them: `input tap … && uiautomator dump` is a tap AND an
    observe. `host:*` is plumbing and a read-back rides on the preceding observe (both
    empty). Segments that match no rule (`sleep`, `head`, `cat` after a dump) are
    dropped when the request carries a real interaction; a request made only of them
    is one `other`. A device-side loop is counted once — the meter cannot see how many
    times the shell ran it."""
    low = request.strip().lower()
    if low.startswith(("host:", "host-serial:", "host-transport")):
        return []
    if not low.startswith(("shell:", "exec:", "shell,v2", "sync:", "framebuffer:")):
        return []
    if low.startswith("sync:"):
        return []
    body = _QUOTES_RE.sub("", low.split(":", 1)[-1])
    kinds: list[str] = []
    for segment in _CHAIN_RE.split(body):
        segment = segment.strip()
        if not segment:
            continue
        for pattern, kind in _ADB_RULES:
            if pattern.search(segment):
                kinds.append(kind)
                break
    if kinds:
        return kinds
    return [] if _is_readback(low) else [OTHER]


def classify_adb(request: str) -> str | None:
    """The first interaction in an ADB service request, or None if it is not device
    work. Counting uses classify_adb_all — a chained request costs every step in it."""
    kinds = classify_adb_all(request)
    return kinds[0] if kinds else None


def _is_readback(request: str) -> bool:
    low = request.strip().lower()
    if low.startswith("sync:"):
        return True
    body = low.split(":", 1)[-1].lstrip()
    return any(re.match(rf"{cmd}\b", body) for cmd in _READBACK if cmd != "sync:")


def classify_mcp(tool_name: str) -> str | None:
    """Interaction for one MCP tool call, or None if it is not device work."""
    base = (tool_name or "").split("__")[-1].strip()
    if not base or any(base.startswith(p) for p in _MCP_IGNORED):
        return None
    for prefix, kind in _MCP_RULES:
        if base == prefix or base.startswith(prefix):
            return kind
    return OTHER if base.startswith("mobile_") else None


@dataclass
class InteractionLog:
    """Append-only interaction counts, shared by both meters. Flushed after every
    event — the budget hook is another process reading this file before each tool call."""

    path: Path
    counts: Counter = field(default_factory=Counter)
    total: int = 0
    # Diagnostic side-channel (e.g. mcp_meter_bytes) — never counted, so a meter
    # reporting a plausible 0 still leaves evidence of the traffic it saw.
    meta: dict = field(default_factory=dict)

    def record_adb(self, request: str) -> str | None:
        # Read-collapsing lives in classify_adb_all and is stateless on purpose — a
        # rule depending on ordering would score the same command differently.
        last = None
        for kind in classify_adb_all(request):
            last = self._append(kind)
        return last

    def record_mcp(self, tool_name: str) -> str | None:
        kind = classify_mcp(tool_name)
        return None if kind is None else self._append(kind)

    def _append(self, kind: str) -> str:
        self.counts[kind] += 1
        self.total += 1
        self.flush()
        return kind

    def flush(self) -> None:
        """Atomic — a reader in another process must never see a partial file and
        conclude fewer steps were spent than really were."""
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.as_metrics()))
            tmp.replace(self.path)
        except OSError:
            pass

    def set_meta(self, key: str, value) -> None:
        self.meta[key] = value
        self.flush()

    def as_metrics(self) -> dict:
        return {
            "interactions": self.total,
            **{f"interactions_{k}": self.counts.get(k, 0) for k in KINDS},
            **self.meta,
        }


def read_total(path: Path) -> int:
    try:
        data = json.loads(Path(path).read_text())
        return int(data.get("interactions") or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


# Per-episode step budget, enforced PreToolUse; written to the run dir and run by
# python3 so it can be tested as code. Cost comes from interactions.json — the shared
# step unit — so every adapter budgets from this one file, with a one-call lag.
BUDGET_HOOK = '''#!/usr/bin/env python3
import json, os, sys

COUNT = r"{count_file}"
METER = r"{meter_file}"
CAP = {cap}
SENTINEL = r"{sentinel}"


def _interactions():
    """The shared step unit, written by the meters below this agent. None means the
    file is unreadable — broken measurement, which is not the same as a real 0."""
    try:
        with open(METER) as fh:
            data = json.load(fh)
        return int(data.get("interactions") or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {{}}
name = payload.get("tool_name") or payload.get("name") or ""
name = name if isinstance(name, str) else ""

# Always allowed, never counted: denying the device-lock release would orphan the
# lock and brick the device for every later episode.
if name.endswith("qg_release_device"):
    sys.exit(0)

# Read the meter's running total instead of accumulating our own — concurrent hook
# processes clobber a read-modify-write counter. Non-device calls cost nothing.
spent = _interactions()
if spent is None:
    # Fail CLOSED: an unmeasured episode with no budget would run unbounded
    # and still be scored.
    try:
        open(SENTINEL, "a").close()
    except OSError:
        pass
    sys.stderr.write("Step meter unreadable - episode terminated (unmeasured).\\n")
    sys.exit(2)

try:
    with open(COUNT, "w") as fh:
        fh.write(str(spent))
except OSError:
    pass

if spent > CAP:
    # HARD stop, no "write your report now" nudge — the sentinel is what base.run()
    # kills on. A nudge would make the budget part of the treatment.
    try:
        open(SENTINEL, "a").close()
    except OSError:
        pass
    sys.stderr.write("Tool-call budget (%d) exhausted - episode terminated.\\n" % CAP)
    sys.exit(2)
sys.exit(0)
'''
