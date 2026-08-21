"""Detect an episode that read the benchmark's own answer key. A tripwire, not the
fix (isolation is the fix). HARD hits (repo, sibling app source, transcript canary)
void the episode; SOFT hits (own session logs, scratch dirs) are recorded only."""

from __future__ import annotations

import os
import re
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


def scan(
    parser,
    workspace: str | Path | None,
    repo_root: str | Path | None = None,
    home: str | None = None,
) -> Contamination:
    """Classify an episode's filesystem reach. `workspace` is the episode's own
    directory; `repo_root`'s parent is sensitive too — that is where the app
    source checkouts live."""
    repo = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent.parent
    repo_s = os.path.normpath(str(repo))
    siblings_s = os.path.normpath(str(repo.parent))
    # Absolute, always: transcript paths are absolute, so a relative workspace
    # would never match the exemption below and clean episodes would be voided.
    ws_s = os.path.abspath(str(workspace)) if workspace else None
    # The run directory sits inside the repo but holds no answer — exempt it,
    # or a stray `ls` of the episode's own cwd trips `benchmark_repo`.
    run_dir_s = os.path.dirname(ws_s) if ws_s else None
    home_s = os.path.normpath(home or str(Path.home()))

    report = Contamination()
    seen_hard: set[tuple[str, str]] = set()
    seen_soft: set[tuple[str, str]] = set()

    for event in parser.events():
        name = getattr(event, "name", "") or ""

        # The canary only proves contamination in a tool RESULT — matching the
        # input would fire on an agent that merely searched for the string.
        if CANARY in (getattr(event, "result_text", "") or ""):
            key = ("canary", name)
            if key not in seen_hard:
                seen_hard.add(key)
                report.hard.append({"kind": "canary", "tool": name,
                                    "detail": "spec canary appeared in a tool result"})

        for raw in _candidate_paths(event):
            path = _expand(raw, home_s)
            if not path.startswith("/"):
                continue
            if any(_under(path, r) for r in _DEVICE_ROOTS):
                continue
            if run_dir_s and _under(path, run_dir_s):
                continue
            if any(_under(path, r) for r in _TOOLCHAIN_ROOTS):
                continue

            # Session logs first: never an answer source, and the sibling catch-all
            # below could otherwise void an episode for reading its own transcript.
            if any(seg in path for seg in _SESSION_ROOTS):
                kind = "session_log"
            elif _under(path, repo_s):
                kind = "benchmark_repo"
            elif _under(path, siblings_s):
                kind = "app_source_checkout"
            elif any(_under(path, r) for r in _SCRATCH_ROOTS):
                continue
            elif _under(path, home_s) and path.count("/") >= 2:
                # Catch-all for the developer's own filesystem. The segment floor
                # drops path-shaped regex artefacts (resource ids, XPath fragments).
                kind = "outside_workspace"
            else:
                continue

            if kind in ("benchmark_repo", "app_source_checkout"):
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
