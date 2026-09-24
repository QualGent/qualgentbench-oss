"""Saved-episode replay: every adb request an agent's transcript would send, run through
the meter's deny rules (`adb_meter.deny_reason`) device-free.

This is how a deny-rule change proves it refuses no legitimate QA request. It used to
glob the developer's `~/.qualgentbench/runs` and a hard-coded checkout path, so it
passed or skipped depending on whose machine ran it (QUA-2807). Now it runs against a
FIXTURE corpus that is part of the repository, and the developer's saved runs are an
opt-in second corpus:

    QGB_REPLAY_RUNS=~/.qualgentbench/runs:/path/to/checkout/runs uv run pytest tests/test_adb_meter.py

The fixture (`tests/fixtures/adb_replay/<task>/<run>/agent/transcript.txt`, the layout of
a runs dir) is two synthetic episodes on public apps, one claude-code stream-json and one
codex `exec --json`, carrying the request shapes the saved agents actually sent (tap,
text, keyevent, swipe, `uiautomator dump`/`cat`, exec-out screencap, pull/push, logcat,
dumpsys, am/pm/cmd/appops/content/settings, getevent...) and three the meter MUST refuse
(`FIXTURE_ILLEGITIMATE`). Use it through the `adb_replay_corpus` fixture (conftest):

    def test_my_rule_denies_no_legitimate_request(adb_replay_corpus):
        assert adb_replay_corpus.legitimate_newly_denied(new_rule, old=old_rule) == []

A new deny rule adds its request shape to the fixture when it touches a shape the fixture
does not carry yet; `test_the_fixture_corpus_carries_every_shape_the_saved_agents_sent`
lists the families it must keep.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "adb_replay"
TRANSCRIPT_GLOB = "*/*/agent/transcript.txt"
#: Opt-in: `os.pathsep`-separated runs dirs to replay beside the fixture. Unset = skipped.
SAVED_RUNS_ENV = "QGB_REPLAY_RUNS"

#: Every request in the fixture corpus that an agent must NOT get, and the deny reason
#: it must get. Every other request in the fixture is legitimate QA.
FIXTURE_ILLEGITIMATE: Mapping[str, str] = {
    "root:": "adb root",                                    # the QUA-2784 re-run's `adb root`
    "shell,v2,raw:su 0 cat /data/anr/anr_2026-09-16-10-02-38-207": "su",
    "exec:run-as com.futsch1.medtimer true": "run-as",
}


def adb_requests(transcript_line: str, services: bool = False):
    """The adb shell/exec service requests an agent's host command would send, parsed
    out of one transcript JSONL line (claude-code `Bash` tool_use and codex
    `command_execution`). Best-effort and conservative — used only to replay saved
    episodes against the meter's deny rules, never in production. With `services`,
    every other adb subcommand yields the request the real client sends for it too
    (wire shapes measured with adb 36.0.2, QUA-2795): `root:`, `reboot:<arg>`,
    `shell,v2,raw:remount`, `host:kill`, `sync:` for pull/push, `host:<sub>` else."""
    try:
        ev = json.loads(transcript_line)
    except (json.JSONDecodeError, TypeError):
        return
    host_cmds: list[str] = []
    if ev.get("type") == "assistant":
        for b in ev.get("message", {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash":
                c = (b.get("input") or {}).get("command")
                if c:
                    host_cmds.append(c)
    item = ev.get("item")
    if ev.get("type") == "item.completed" and isinstance(item, dict) \
            and item.get("type") == "command_execution":
        c = item.get("command", "")
        m = re.match(r"^/bin/(?:zsh|bash|sh) -lc (.*)$", c, re.S)
        if m:
            try:
                c = shlex.split(m.group(1))[0]
            except ValueError:
                pass
        host_cmds.append(c)

    op = re.compile(r"&&|\|\||[;\n|&()]")
    for cmd in host_cmds:
        if "adb" not in cmd:
            continue
        for seg in op.split(cmd):
            try:
                words = shlex.split(seg, posix=True)
            except ValueError:
                continue
            if "adb" not in words:
                continue
            rest = words[words.index("adb") + 1:]
            j = 0
            while j < len(rest) and rest[j].startswith("-"):
                j += 2 if rest[j] in ("-s", "-t", "-H", "-P", "-L") else 1
            if j >= len(rest):
                continue
            sub, args = rest[j], rest[j + 1:]
            if sub == "shell":
                k = 0
                while k < len(args) and args[k] in ("-T", "-t", "-tt", "-x", "-n", "-e"):
                    k += 1
                yield "shell,v2,raw:" + " ".join(args[k:])
            elif sub in ("exec-out", "exec-in"):
                yield "exec:" + " ".join(args)
            elif not services:
                continue
            elif sub in ("root", "unroot", "usb"):
                yield f"{sub}:"
            elif sub in ("reboot", "tcpip"):
                yield f"{sub}:" + (args[0] if args else "")
            elif sub.startswith("reboot-"):
                yield "reboot:" + sub[len("reboot-"):]
            elif sub in ("remount", "disable-verity", "enable-verity"):
                yield "shell,v2,raw:" + " ".join([sub, *args])
            elif sub == "kill-server":
                yield "host:kill"
            elif sub in ("pull", "push", "sync"):
                yield "sync:"
            elif sub in ("forward", "reverse"):
                # `adb forward A B` → `host:forward:A;B`; reverse rides on the device
                # (QUA-2797). Wire shapes measured with adb 36.0.2.
                pre = "host:" if sub == "forward" else "reverse:"
                pos = [a for a in args if not a.startswith("-")]
                if "--list" in args:
                    yield f"{pre}list-forward"
                elif "--remove-all" in args:
                    yield f"{pre}killforward-all"
                elif "--remove" in args:
                    yield f"{pre}killforward:" + (pos[0] if pos else "")
                elif len(pos) >= 2:
                    rebind = "norebind:" if "--no-rebind" in args else ""
                    yield f"{pre}forward:{rebind}{pos[0]};{pos[1]}"
            elif sub == "jdwp":
                yield "jdwp"
            elif sub == "track-jdwp":
                yield "track-jdwp"
            else:
                yield f"host:{sub}"


Rule = Callable[[str], object]


def _never(_request: str) -> None:
    return None


@dataclass(frozen=True)
class ReplayCorpus:
    """A set of runs dirs whose agent transcripts are replayed request by request.

    `illegitimate` is what is KNOWN about the corpus: exhaustive for the fixture
    (`exact`), empty for a developer's saved runs, where nobody has labelled every
    request. `min_requests` is the floor that proves the sweep read something."""

    name: str
    roots: tuple[Path, ...]
    illegitimate: Mapping[str, str] = field(default_factory=dict)
    exact: bool = False
    min_requests: int = 1

    def transcripts(self) -> list[Path]:
        return sorted(t for r in self.roots for t in r.glob(TRANSCRIPT_GLOB))

    def requests(self, services: bool = True) -> Iterator[tuple[Path, str]]:
        """(transcript, request) for every adb request, in transcript order."""
        for t in self.transcripts():
            for line in t.read_text(errors="replace").splitlines():
                for req in adb_requests(line, services=services):
                    yield t, req

    def newly_denied(self, new: Rule, old: Rule = _never, services: bool = True) -> list[str]:
        """Requests `new` refuses and `old` did not (truthy = refused, as `deny_reason`)."""
        return [r for _, r in self.requests(services) if new(r) and not old(r)]

    def legitimate_newly_denied(self, new: Rule, old: Rule = _never,
                                services: bool = True) -> list[str]:
        """`newly_denied` minus the requests known to be illegitimate: the list a deny-rule
        change must keep empty."""
        return [r for r in self.newly_denied(new, old, services) if r not in self.illegitimate]


def fixture_corpus() -> ReplayCorpus:
    return ReplayCorpus("fixture", (FIXTURE_DIR,), FIXTURE_ILLEGITIMATE, exact=True,
                        min_requests=40)


def saved_runs_corpus(environ: Mapping[str, str] = os.environ) -> ReplayCorpus | None:
    """The developer's own runs dirs, when `QGB_REPLAY_RUNS` names them; else None."""
    raw = environ.get(SAVED_RUNS_ENV, "").strip()
    if not raw:
        return None
    roots = tuple(Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip())
    return ReplayCorpus("saved-runs", roots)


def corpus_params() -> list:
    """`pytest.param`s for the `adb_replay_corpus` fixture: the fixture corpus always, the
    saved runs only when opted in. Read at conftest import, before `_isolate_env`."""
    saved = saved_runs_corpus()
    return [pytest.param(fixture_corpus(), id="fixture"),
            pytest.param(saved, id="saved-runs", marks=pytest.mark.skipif(
                saved is None, reason=f"set {SAVED_RUNS_ENV}=<runs dir>[{os.pathsep}...] to "
                                      "also replay saved episodes"))]
