"""Detect an episode that read the benchmark's own answer key. A tripwire, not the
fix (isolation is the fix). HARD hits (repo, sibling app source, another episode's
directory, a DevLoop-MCP artifact the server did not hand this agent, transcript
canary) void the episode; SOFT hits (own session logs, scratch dirs) are recorded only.

Blind spot: instruction files an agent loads at START-UP (CLAUDE.md / AGENTS.md on
its cwd's ancestor chain) never appear in a tool call. That is prevented, not
detected: `config.runs_dir_problems`, checked by `run` and by every episode."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Embedded as a comment in every spec YAML. A targeted grep may miss the comment
# line, so the path tripwire is the primary detector; the canary corroborates.
CANARY = "QGB-CANARY-7f3a91c4e85b2d06"

# Device paths inside adb commands — not host filesystem access at all.
_DEVICE_ROOTS = ("/sdcard", "/storage", "/data/", "/system", "/mnt", "/proc", "/dev",
                 "/vendor", "/product", "/apex", "/acct", "/config")

# Host locations an episode may legitimately touch: its toolchain and scratch space.
_TOOLCHAIN_ROOTS = ("/usr", "/bin", "/sbin", "/opt", "/etc", "/Library", "/System",
                    "/Applications", "/nix", "/lib", "/var/db", "/private/var/db")
_SCRATCH_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")

# The agent's own session transcript — self-inspection, not answer access, so SOFT.
_SESSION_ROOTS = ("/.claude", "/.codex", "/.config/claude", "/.cache/claude")

# A path-shaped token that is not a URL tail or a flag value. Deliberately
# permissive — over-matches are filtered by the root checks; a missed path is a leak.
_PATH_TOKEN = re.compile(r"(?<![\w:=])(?:\$HOME|~|/)[A-Za-z0-9._+\-/$~]{3,}")

_PATH_KEYS = ("file_path", "path", "notebook_path", "pattern", "glob", "cwd")

# adb server-selection options (QUA-2804). The meter guards the agent's adb only
# through its environment: `run_episode` points ANDROID_ADB_SERVER_PORT at the meter
# in `agent_env`. An agent that re-selects the server — `adb -P <port>`, `-H <host>`,
# `-L <socket>`, or its own `ANDROID_ADB_SERVER_PORT=`/`_ADDRESS=`/`_HOST=` /
# `ADB_SERVER_SOCKET=` assignment — reaches the real adb server directly, where nothing
# is metered, charged or denied. The meter cannot refuse what never reaches it, so the
# bypass is a HARD contamination hit read off the agent's own command text: any such
# command went around the meter. Reading the meter port (`echo $ANDROID_ADB_SERVER_PORT`)
# is not an override and is not matched — only a `-P/-H/-L` flag to adb or an env
# ASSIGNMENT of one of these variables.
_ADB_SERVER_SELECT = re.compile(
    r"(?<![\w-])adb\b[^\n;&|]*?\s-(?:P|H|L)(?:\s|=|$)"
    r"|(?<![\w./-])(?:ANDROID_ADB_SERVER_(?:PORT|ADDRESS|HOST)|ADB_SERVER_SOCKET)=")


def devloop_default_roots(home: str | None = None) -> list[str]:
    """Where a DevLoop-MCP server writes client artifacts when nothing overrides it
    (QUA-2800): its run/session root under the system temp dir (screenshots,
    diffs, traces, profiles, per-session baselines, screen recordings), its stdio
    baseline store `~/.devloop-mcp`, and its trajectory spool `~/.devloop`. A server
    started with DEVLOOP_ARTIFACT_DIR / DEVLOOP_BASELINE_DIR elsewhere reports its
    real roots at `GET /devloop/sessions`; `run_episode` adds those."""
    home_s = home or str(Path.home())
    temps = {tempfile.gettempdir(), "/tmp"}
    roots = [os.path.join(t, "devloop-mcp") for t in temps]
    roots += [os.path.join(home_s, ".devloop-mcp"), os.path.join(home_s, ".devloop")]
    return _root_forms(roots)


def _root_forms(roots) -> list[str]:
    """Each root as written and symlink-resolved, plus its /private twin: macOS's
    /tmp and /var are symlinks into /private, and a transcript may use either."""
    out: list[str] = []
    for root in roots or []:
        if not root:
            continue
        base = re.sub(r"^/{2,}", "/", os.path.normpath(os.path.abspath(str(root))))
        forms = {base, os.path.realpath(base)}
        for form in list(forms):
            if form.startswith("/private/"):
                forms.add(form[len("/private"):])
            elif form.startswith(("/tmp/", "/var/")) or form in ("/tmp", "/var"):
                forms.add("/private" + form)
        for form in sorted(forms):
            if form != os.path.sep and form not in out:
                out.append(form)
    return out


@dataclass
class Contamination:
    contaminated: bool = False
    hard: list[dict] = field(default_factory=list)
    soft: list[dict] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return sorted({h["kind"] for h in self.hard})

    def as_metrics(self) -> dict:
        """Flat, JSON-safe fields for result.json. Hits are capped — the record
        exists for human review, not to store the transcript."""
        return {
            "contaminated": self.contaminated,
            "contamination_reasons": self.reasons,
            "contamination_hits": self.hard[:20],
            "out_of_workspace": [s["kind"] for s in self.soft[:20]],
        }


def _expand(token: str, home: str) -> str:
    if token.startswith("$HOME"):
        token = home + token[5:]
    elif token.startswith("~"):
        token = home + token[1:]
    # Not resolve(): it walks parent symlinks even for missing paths, turning /tmp
    # into /private/tmp. And normpath preserves a leading `//`, which lets
    # double-slash device paths escape the device-root check.
    return re.sub(r"^/{2,}", "/", os.path.normpath(token))


def _under(path: str, root: str) -> bool:
    root = os.path.normpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def _candidate_paths(event) -> list[str]:
    """Host paths an event references, from both the structured tool input and any
    shell command inside it — either alone misses cases."""
    out: list[str] = []
    inp = getattr(event, "input", None)
    if not isinstance(inp, dict):
        return out
    for key in _PATH_KEYS:
        val = inp.get(key)
        if isinstance(val, str) and val:
            out.append(val)
    for key in ("command", "cmd", "script", "content"):
        val = inp.get(key)
        if isinstance(val, str) and val:
            out.extend(_PATH_TOKEN.findall(val))
    return out


def _command_texts(event) -> list[str]:
    """Shell/command text an event carries (a Bash tool_use, a codex command_execution).
    Where the agent's own adb invocation is visible."""
    out: list[str] = []
    inp = getattr(event, "input", None)
    if isinstance(inp, dict):
        for key in ("command", "cmd", "script", "content"):
            val = inp.get(key)
            if isinstance(val, str) and val:
                out.append(val)
    return out


def scan(
    parser,
    workspace: str | Path | None,
    repo_root: str | Path | None = None,
    home: str | None = None,
    devloop_roots: list[str] | None = None,
    nonce: str | None = None,
) -> Contamination:
    """Classify an episode's filesystem reach. `workspace` is the episode's own
    directory; `repo_root`'s parent is sensitive too — that is where the app
    source checkouts live.

    `devloop_roots` (default: `devloop_default_roots()`) are the directories a
    DevLoop-MCP server writes client artifacts under. One server serves every
    episode (and every lane), so anything there that the server did not hand THIS
    agent in one of its own MCP tool results is another episode's screenshot,
    baseline, trace or recording: `devloop_artifacts`, a hard hit (QUA-2800), with
    the same consequence as reading another episode's directory (QUA-2778). A
    path the server handed the agent (a baseline, a diff image, a recording) and
    anything under it stays readable."""
    repo = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent.parent
    repo_s = os.path.normpath(str(repo))
    # In a container the repo sits at /app: its parent is the filesystem root,
    # under which EVERY absolute path lies. There are no sibling checkouts there —
    # a degenerate sibling root must disable the rule, not swallow the world.
    siblings_s = os.path.normpath(str(repo.parent))
    if siblings_s == os.path.sep:
        siblings_s = None
    # Absolute, always: transcript paths are absolute, so a relative workspace
    # would never match the exemption below and clean episodes would be voided.
    ws_s = os.path.abspath(str(workspace)) if workspace else None
    # The run directory sits inside the repo but holds no answer — exempt it,
    # or a stray `ls` of the episode's own cwd trips `benchmark_repo`.
    run_dir_s = os.path.dirname(ws_s) if ws_s else None
    home_s = os.path.normpath(home or str(Path.home()))
    # The runs root (<runs>/<task>/<run>/workspace → <runs>) holds every OTHER
    # episode's transcript, findings and verdict. While runs lived under the repo
    # `benchmark_repo` covered it; since QUA-2778 they live outside the tree
    # (~/.qualgentbench/runs), so it needs a rule of its own. Disabled when the
    # layout does not yield a real directory (`/`, the home dir itself, or a bare
    # scratch root, where it would swallow every scratch file the agent writes).
    runs_root_s = None
    if ws_s and os.path.basename(ws_s) == "workspace":
        cand = os.path.dirname(os.path.dirname(run_dir_s))
        too_wide = {os.path.sep, home_s, os.path.dirname(home_s),
                    *(os.path.normpath(r) for r in _SCRATCH_ROOTS)}
        if cand not in too_wide:
            runs_root_s = cand

    dl_roots = _root_forms(devloop_default_roots(home_s) if devloop_roots is None
                           else devloop_roots)
    events = list(parser.events())
    handed = _handed_devloop_paths(events, dl_roots, home_s)

    report = Contamination()
    seen_hard: set[tuple[str, str]] = set()
    seen_soft: set[tuple[str, str]] = set()

    for event in events:
        name = getattr(event, "name", "") or ""

        # The canary only proves contamination in a tool RESULT — matching the
        # input would fire on an agent that merely searched for the string.
        if CANARY in (getattr(event, "result_text", "") or ""):
            key = ("canary", name)
            if key not in seen_hard:
                seen_hard.add(key)
                report.hard.append({"kind": "canary", "tool": name,
                                    "detail": "spec canary appeared in a tool result"})

        # The per-episode nonce (QUA-2804): a random value written into this episode's
        # `qgb_flags.txt` and its `.qgb` markers. It can only reach the agent by the
        # agent reading the app's private sandbox — the very access the meter refuses —
        # so its appearance in a tool RESULT (or a file the agent wrote) voids the
        # episode, the same mechanism and consequence as the spec canary. The nonce
        # VALUE is never recorded — the detail is a fixed string — so nothing the scorer
        # or reporting publishes carries it.
        # Unlike the canary, the nonce is secret and random: an agent can only hold it
        # by having read the sandbox, so matching the INPUT too (a Write/Edit whose
        # content carries it — "any file the agent wrote") raises no false positive.
        if nonce and (nonce in (getattr(event, "result_text", "") or "")
                      or any(nonce in t for t in _command_texts(event))):
            key = ("flag_nonce", name)
            if key not in seen_hard:
                seen_hard.add(key)
                report.hard.append({"kind": "flag_nonce", "tool": name,
                                    "detail": "episode flag nonce appeared in agent-visible output"})

        # An adb server-selection option in the agent's OWN command text (QUA-2804):
        # the command reached the real adb server, around the meter. Read off the input
        # (the agent typed it), not a result. A HARD hit — the episode was unmetered.
        for text in _command_texts(event):
            if _ADB_SERVER_SELECT.search(text):
                key = ("adb_server_bypass", name)
                if key not in seen_hard:
                    seen_hard.add(key)
                    report.hard.append({"kind": "adb_server_bypass", "tool": name,
                                        "detail": "adb server-selection option bypasses the meter"})
                break

        for raw in _candidate_paths(event):
            path = _expand(raw, home_s)
            if not path.startswith("/"):
                continue
            if any(_under(path, r) for r in _DEVICE_ROOTS):
                continue
            if run_dir_s and _under(path, run_dir_s):
                continue
            if any(_under(path, r) for r in dl_roots):
                if any(_under(path, h) for h in handed):
                    continue
                key = ("devloop_artifacts", path)
                if key not in seen_hard:
                    seen_hard.add(key)
                    report.hard.append({"kind": "devloop_artifacts", "tool": name,
                                        "detail": path})
                continue
            if any(_under(path, r) for r in _TOOLCHAIN_ROOTS):
                continue

            # Another episode first: its own claude_home/.codex session logs are that
            # episode's answers, not this one's transcript.
            if runs_root_s and _under(path, runs_root_s):
                kind = "other_episode"
            # Session logs next: never an answer source, and the sibling catch-all
            # below could otherwise void an episode for reading its own transcript.
            elif any(seg in path for seg in _SESSION_ROOTS):
                kind = "session_log"
            elif _under(path, repo_s):
                kind = "benchmark_repo"
            elif siblings_s and _under(path, siblings_s):
                kind = "app_source_checkout"
            elif any(_under(path, r) for r in _SCRATCH_ROOTS):
                continue
            elif _under(path, home_s) and path.count("/") >= 2:
                # Catch-all for the developer's own filesystem. The segment floor
                # drops path-shaped regex artefacts (resource ids, XPath fragments).
                kind = "outside_workspace"
            else:
                continue

            if kind in ("benchmark_repo", "app_source_checkout", "other_episode"):
                key = (kind, path)
                if key not in seen_hard:
                    seen_hard.add(key)
                    report.hard.append({"kind": kind, "tool": name, "detail": path})
            else:
                key = (kind, path)
                if key not in seen_soft:
                    seen_soft.add(key)
                    report.soft.append({"kind": kind, "tool": name, "detail": path})

    report.contaminated = bool(report.hard)
    return report


def _handed_devloop_paths(events, roots: list[str], home: str) -> list[str]:
    """Paths under a DevLoop root that the MCP server itself put in this agent's
    tool results. A root itself (or anything above one) is never an exemption:
    only a path strictly inside one, so an error message naming the whole temp
    root cannot open every other episode's files."""
    handed: list[str] = []
    for event in events:
        if not getattr(event, "server", "") and not _is_mcp_name(getattr(event, "name", "")):
            continue
        for raw in _PATH_TOKEN.findall(getattr(event, "result_text", "") or ""):
            path = _expand(raw, home)
            if not any(_under(path, r) and path != os.path.normpath(r) for r in roots):
                continue
            # A path inside a server session's own root (`.../sessions/<32 hex>/...`)
            # hands the agent that whole root: it is this agent's session, and
            # everything in it is its own (its baselines dir beside a saved one).
            own = _OWN_SESSION_ROOT.match(path)
            for form in _root_forms([own.group(1) if own else path]):
                if form not in handed:
                    handed.append(form)
    return handed


_OWN_SESSION_ROOT = re.compile(r"^(.*/sessions/[0-9a-f]{32})(?:/|$)")


def _is_mcp_name(name: str) -> bool:
    from .interactions import MCP_TOOL_RULES

    return name in MCP_TOOL_RULES
