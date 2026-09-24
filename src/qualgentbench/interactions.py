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
# The tool name IS the intent — a lookup, not inference. One table answers every
# question the meter and the scorers ask about a tool, so they cannot drift apart:
#
#   steps   what the meter charges, in order. () = not charged (plumbing, bookkeeping,
#           diagnostics). A tool that does two interactions costs both.
#   device  its RESULT is device evidence (grounding, the screen witness, probes).
#           False for tools whose answer is the agent's own bookkeeping echoed back or
#           host state — a report must never ground itself on its own tool reply.
#   reads   the result is a read of the app: SCREEN (the whole screen/tree — what a
#           screen witness and the screenshot-only exemption are judged against) or
#           QUERY (a targeted read: one element, a hit test — it grounds and witnesses
#           but alone does not revoke the exemption). None = not a read.
#   echo    the CALL is device work (it counts, it is charged) but its REPLY is the
#           call's own argument handed back — DevLoop's text entry answers
#           `Set focused field to: '<text>'` / `Typed: '<text>'` — so the reply never
#           grounds a report's quote (QUA-2805). Without it an agent could type a string
#           and quote the acknowledgement as a sighting; the bare arm's `input text`
#           answers nothing, so the arms differed. Journey grounding
#           (`journey._device_texts(results_only=True)`) drops these replies.
#           QUA-2817 widened it from text entry to EVERY non-read tool whose normal
#           reply repeats a caller-chosen string (`Opened URL: <url>`, `Launched <pkg>`,
#           `<pkg> is not running` in a log read's note…) or computes a caller-chosen
#           value (`js_evaluate`/`web_eval`). None of those replies is a read of the
#           app's screen, so dropping them costs an honest report nothing. A READ
#           (`reads` set) is never an echo: its answer is the screen.
#
# EXACT names only, matched on the base name (`mcp__device__mobile_tap` → `mobile_tap`).
# Prefix matching let `mobile_tap` swallow any future `mobile_tap_*`, which is exactly
# the silent classification this table exists to prevent. The one family is `qg_`
# (device-lock plumbing, exempt everywhere). Every DevLoop-MCP tool must have an entry:
# `tests/test_interactions.py` holds this table against `tests/fixtures/devloop_tools.json`
# (DevLoop's tools/list), so a new DevLoop tool fails the suite instead of costing a step.
# docs/architecture.md carries the same table in prose — change both together.
SCREEN = "screen"
QUERY = "query"


@dataclass(frozen=True)
class McpRule:
    steps: tuple[str, ...] = ()
    device: bool = True
    reads: str | None = None
    echo: bool = False


def _charge(*steps: str, reads: str | None = None, echo: bool = False) -> McpRule:
    return McpRule(steps=steps, device=True, reads=reads, echo=echo)


_FREE = McpRule()                        # a device read/diagnostic, not charged
_BOOKKEEPING = McpRule(device=False)     # agent bookkeeping / host state: no step, no evidence
# Text entry: one TYPE, and the reply is the typed argument echoed back (see `echo`).
_TEXT_ENTRY = McpRule(steps=(TYPE,), device=True, echo=True)
# A free device read/diagnostic whose reply repeats (or computes) a caller argument.
_FREE_ECHO = McpRule(echo=True)

MCP_TOOL_RULES: dict[str, McpRule] = {
    # ── taps ──
    "mobile_tap": _charge(TAP),
    "mobile_long_press": _charge(TAP),
    "mobile_double_tap": _charge(TAP),
    "mobile_dismiss_dialogs": _charge(TAP),
    "mobile_web_click": _charge(TAP),
    # A tap AND a look — the bare arm pays `input tap` + `uiautomator dump` for the
    # same act, and a step is one interaction (QUA-2775; it was one TAP before).
    "mobile_tap_and_observe": _charge(TAP, OBSERVE, reads=SCREEN),
    # ── text entry: the reply echoes the argument, so it never grounds (`echo`) ──
    # DevLoop's replies (tools/input.py, tools/web.py on dev): type_text answers
    # `Set focused field to: '<text>'` (replace_existing, the default) or
    # `Typed: '<text>'`; edit_field `Typed: '<value>'`; paste_text
    # `{clipboard_set, pasted, fill_method, length, next_action}`; web_fill `{ok, value}`
    # with `value` read back from the field it just set.
    "mobile_type_text": _TEXT_ENTRY,
    "mobile_edit_field": _TEXT_ENTRY,
    "mobile_paste_text": _TEXT_ENTRY,
    "mobile_web_fill": _TEXT_ENTRY,
    # ── gestures / keys ──
    # `echo` below (QUA-2817): the reply names the argument back — `Swiped <direction>`,
    # `Pressed <button>` (and `Unknown button: '<button>'` for any other string).
    "mobile_swipe": _charge(SWIPE, echo=True),
    "mobile_swipe_coordinates": _charge(SWIPE),
    "mobile_press_button": _charge(PRESS, echo=True),
    # ── app lifecycle ── (echo: `Launched <package_id>`, `Opened URL: <url>`,
    #    `Terminated <package_id>` — QUA-2817; open_url was the one QUA-2805 missed)
    "mobile_launch_app": _charge(LAUNCH, echo=True),
    "mobile_open_url": _charge(LAUNCH, echo=True),  # `am start -a VIEW -d <url>` on the bare arm
    # Install (free, like mobile_install_app) + launch + the visible element list.
    "mobile_setup_app": _charge(LAUNCH, OBSERVE, reads=SCREEN),
    "mobile_terminate_app": _charge(TERMINATE, echo=True),
    # ── screen reads ──
    "mobile_observe_screen": _charge(OBSERVE, reads=SCREEN),
    "mobile_take_screenshot": _charge(OBSERVE, reads=SCREEN),   # not DevLoop; other servers
    "mobile_native_hierarchy": _charge(OBSERVE, reads=SCREEN),
    "mobile_web_observe": _charge(OBSERVE, reads=SCREEN),
    "mobile_await_element": _charge(OBSERVE, reads=QUERY),
    "mobile_find_views": _charge(OBSERVE, reads=QUERY),
    "mobile_hit_test": _charge(OBSERVE, reads=QUERY),
    # ── one interaction with no kind of its own (the bare arm's `settings put`,
    #    `pm grant`, `logcat` all classify `other`) ──
    # echo (QUA-2817): `Orientation set to <orientation>`; set_permission's
    # {package_id, permission, …}; the log reads' `<package_id> is not running` /
    # `if none mention <package_id>` notes. A log read is not a screen read, so no
    # honest quote of what the app SHOWED is lost by dropping it.
    "mobile_set_orientation": _charge(OTHER, echo=True),
    "mobile_rotate_gesture": _charge(OTHER),
    "mobile_pinch": _charge(OTHER),
    "mobile_set_permission": _charge(OTHER, echo=True),
    "mobile_device_logs": _charge(OTHER, echo=True),
    "mobile_crash_logs": _charge(OTHER, echo=True),
    # ── device plumbing and reads of device configuration: free ──
    "mobile_install_app": _FREE_ECHO,          # `Installed <app_path> on <device>`
    "mobile_uninstall_app": _FREE_ECHO,        # `Uninstalled <package_id>`
    # Text entry too (a stored credential typed into the focused field), free as it
    # always was; its reply names the field it typed, an argument — never evidence.
    "mobile_insert_credential": McpRule(echo=True),
    "mobile_list_apps": _FREE,
    "mobile_get_screen_size": _FREE,
    "mobile_get_orientation": _FREE,
    "mobile_get_permissions": _FREE_ECHO,      # {package_id, summary, …}
    "mobile_push_media": _FREE_ECHO,           # `Pushed <file_path> -> …`
    # A wait with no content in its answer — the bare arm's host-side `sleep`, which
    # no meter sees.
    "mobile_await_screen_idle": _FREE,
    # ── capture / visual diff / diagnostics: free ──
    # TODO(QUA-2775): the js_/react_/web_eval entries below can read (and js_evaluate /
    # js_reload can change) app state uncharged. They are inert on today's corpus — no
    # React Native or WebView app — so free is harmless there; an RN/WebView app joining
    # the corpus must revisit them before its first board.
    # _FREE_ECHO (QUA-2817): the reply carries a caller-chosen name/id back
    # (baseline `name`, `package_name`, `bundle_id`, `recording_id`, `profile_id`/
    # `trace_id`/`view`, console-log `package_id`/`pattern`), or is a value the caller's
    # own expression computed (`js_evaluate`, `web_eval` answer `'<anything>'` with it).
    "mobile_visual_baseline": _FREE_ECHO,
    "mobile_visual_compare": _FREE_ECHO,
    "mobile_prepare_app_screen_capture": _FREE_ECHO,
    "mobile_restore_app_screen_capture": _FREE,
    "mobile_get_screen_recording_capabilities": _FREE_ECHO,
    "mobile_start_synthetic_screen_recording": _FREE,
    "mobile_stop_synthetic_screen_recording": _FREE_ECHO,
    "mobile_js_console_logs": _FREE_ECHO,
    "mobile_js_debugger_status": _FREE,
    "mobile_js_evaluate": _FREE_ECHO,
    "mobile_js_network_logs": _FREE,
    "mobile_js_network_request": _FREE,
    "mobile_js_profiler_query": _FREE_ECHO,
    "mobile_js_profiler_start": _FREE,
    "mobile_js_profiler_stop": _FREE,
    "mobile_js_reload": _FREE,
    "mobile_react_component_tree": _FREE,
    "mobile_react_find_component": _FREE,
    "mobile_react_inspect_element": _FREE,
    "mobile_react_profiler_query": _FREE_ECHO,
    "mobile_react_profiler_start": _FREE,
    "mobile_react_profiler_stop": _FREE,
    "mobile_native_profiler_query": _FREE_ECHO,
    "mobile_native_profiler_start": _FREE,
    "mobile_native_profiler_stop": _FREE,
    "mobile_profiler_combined_report": _FREE_ECHO,
    "mobile_web_eval": _FREE_ECHO,
    "mobile_web_list_targets": _FREE,
    # ── bookkeeping and host state: free AND never device evidence ──
    "mobile_report_result": _BOOKKEEPING,
    "mobile_mark_step": _BOOKKEEPING,
    "mobile_note_anomaly": _BOOKKEEPING,
    "mobile_get_action_log": _BOOKKEEPING,
    "mobile_workspace_info": _BOOKKEEPING,
    "mobile_get_otp": _BOOKKEEPING,           # an SMS service, not the device
    "mobile_get_otp_number": _BOOKKEEPING,
    "mobile_boot_emulator": _BOOKKEEPING,
    "mobile_boot_simulator": _BOOKKEEPING,
    "mobile_list_avds": _BOOKKEEPING,
    "mobile_list_simulators": _BOOKKEEPING,
    "mobile_list_available_devices": _BOOKKEEPING,
}

# Families, declared — never inferred. qg_ is exempt everywhere: charging cleanup
# would let it eat an agent's budget.
MCP_FAMILY_RULES: tuple[tuple[str, McpRule], ...] = (
    ("qg_", _BOOKKEEPING),
)

# A `mobile_*` name with no entry (a server the table has never seen) is still charged
# one `other` and still device evidence — charging it is the conservative error, and it
# is what every unlisted tool cost before this table. It is never a read.
_UNKNOWN_MOBILE = McpRule(steps=(OTHER,))


def mcp_base_name(tool_name: str) -> str:
    return (tool_name or "").split("__")[-1].strip()


def mcp_rule(tool_name: str) -> McpRule | None:
    """The EXPLICIT rule for a tool, or None when nothing in the table names it.
    No fallback: this is what the surface test asks."""
    base = mcp_base_name(tool_name)
    if not base:
        return None
    if base in MCP_TOOL_RULES:
        return MCP_TOOL_RULES[base]
    for prefix, rule in MCP_FAMILY_RULES:
        if base.startswith(prefix):
            return rule
    return None


def mcp_effective_rule(tool_name: str) -> McpRule | None:
    """The rule the meter and the scorers apply: the explicit one, else the unknown-
    `mobile_*` default, else None (not a device tool at all)."""
    rule = mcp_rule(tool_name)
    if rule is not None:
        return rule
    return _UNKNOWN_MOBILE if "mobile_" in mcp_base_name(tool_name) else None


def mcp_is_device_evidence(tool_name: str) -> bool:
    rule = mcp_effective_rule(tool_name)
    return bool(rule and rule.device)


def mcp_echoes_argument(tool_name: str) -> bool:
    """Is this tool's REPLY its own argument handed back (text entry)? Such a reply is
    never device evidence for a quote, even though the call itself is device work."""
    rule = mcp_effective_rule(tool_name)
    return bool(rule and rule.echo)


def mcp_reads(tool_name: str) -> str | None:
    rule = mcp_effective_rule(tool_name)
    return rule.reads if rule else None


def _names(pred) -> tuple[str, ...]:
    return tuple(sorted(n for n, r in MCP_TOOL_RULES.items() if pred(r)))


# Derived views for the scorers — never hand-maintained lists.
MCP_CHARGED_TOOLS = _names(lambda r: bool(r.steps))
MCP_OBSERVATION_TOOLS = _names(lambda r: r.reads is not None)
MCP_SCREEN_READ_TOOLS = _names(lambda r: r.reads == SCREEN)
MCP_TAP_TOOLS = _names(lambda r: TAP in r.steps)
MCP_ECHO_TOOLS = _names(lambda r: r.echo)


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


def classify_mcp_all(tool_name: str) -> list[str]:
    """Every interaction one MCP tool call costs, in order ([] = not device work)."""
    rule = mcp_effective_rule(tool_name)
    return list(rule.steps) if rule else []


def classify_mcp(tool_name: str) -> str | None:
    """The first interaction of one MCP tool call, or None if it is not device work.
    Counting uses classify_mcp_all — `mobile_tap_and_observe` costs a tap AND a look."""
    kinds = classify_mcp_all(tool_name)
    return kinds[0] if kinds else None


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
        last = None
        for kind in classify_mcp_all(tool_name):
            last = self._append(kind)
        return last

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
