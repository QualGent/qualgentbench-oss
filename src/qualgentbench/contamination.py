"""Detect an episode that read the benchmark's own answer key. A tripwire, not the
fix (isolation is the fix). HARD hits (repo, sibling app source, another episode's
directory or the run's harness-side index, a DevLoop-MCP artifact the server did not
hand this agent, transcript canary, the flag nonce, an adb server bypass, adbd left
rooted) void the episode; SOFT hits (own session logs, scratch dirs, adbd primed for
root, a cwd the model could not follow) are recorded only.

Paths are resolved the way the agent's shell would have resolved them (QUA-2815):
relative paths, `..`, `$PWD`/`$OLDPWD`/`~`/`$HOME`, `cd`/`pushd`/`popd` chains within a
command and — for claude-code, whose Bash cwd persists — across calls, codex's per-call
`workdir`, and symlinks the agent created (read back on disk). See `_Reach`.

Blind spot: instruction files an agent loads at START-UP (CLAUDE.md / AGENTS.md on
its cwd's ancestor chain) never appear in a tool call. That is prevented, not
detected: `config.runs_dir_problems`, checked by `run` and by every episode."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
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

# adb server-selection (QUA-2804, corrected QUA-2814). The meter guards the agent's adb
# only through its environment: `run_episode` points ANDROID_ADB_SERVER_PORT at the meter
# in `agent_env`. An agent that re-selects the server reaches the real adb server directly,
# where nothing is metered, charged or denied. The meter cannot refuse what never reaches
# it, so the bypass is a HARD contamination hit read off the agent's own command text.
#
# Three ways to re-select, all detected here:
#  (1) adb's own GLOBAL options `-P <port>` / `-H <host>` / `-L <socket>`, in every form
#      adb parses: spaced (`-P 5037`) or ATTACHED (`-P5037`, `-Htcp:…`). These are only
#      global when they come BEFORE the subcommand — a `-H`/`-L`/`-P` after `shell`,
#      `logcat`, `exec-out`, … belongs to the DEVICE-SIDE tool (`top -H`, `logcat -L`,
#      `ls -L`, `grep -H`, `find -L`) and must NOT match (QUA-2814: the old rule matched
#      anywhere on the line and voided honest thread dumps in run 20260923-174028-afd5).
#  (2) an env override of the server vars: an ASSIGNMENT
#      (`ANDROID_ADB_SERVER_PORT=…`/`_ADDRESS=`/`_HOST=`/`ADB_SERVER_SOCKET=`), or REMOVING
#      the meter's var so adb falls back to the default 5037 (`unset …`, `env -u …`).
#  (3) a direct connection to the adb server port where detectable — `nc … 5037`,
#      `/dev/tcp/<host>/5037`, or python/curl to :5037.
# Reading the meter port (`echo $ANDROID_ADB_SERVER_PORT`) is not an override.
_ADB_GLOBAL_SELECT_OPT = re.compile(r"^-[HPL]")     # -H/-P/-L, spaced or attached
_ADB_GLOBAL_VALUE_OPT = frozenset({"-s", "-t"})     # take a value that is not a subcommand
_ADB_SERVER_VARS = r"(?:ANDROID_ADB_SERVER_(?:PORT|ADDRESS|HOST)|ADB_SERVER_SOCKET)"
_ADB_SERVER_ENV = re.compile(
    # an ASSIGNMENT of a server var, or REMOVING the meter's var (unset / env -u) so adb
    # falls back to the default 5037. Reading it (`echo $VAR`) has no `=`/unset and is safe.
    r"(?<![\w./-])" + _ADB_SERVER_VARS + r"="
    + r"|(?<![\w-])unset\s+(?:-\S+\s+)*" + _ADB_SERVER_VARS
    + r"|(?<![\w-])env\s+(?:-\S+\s+)*-u\s*=?\s*" + _ADB_SERVER_VARS)
_ADB_RAW_SOCKET = re.compile(
    r"/dev/tcp/[^/\s]+/5037(?![\d])"                               # bash /dev/tcp pseudo-file
    r"|(?<![\w.])(?:nc|ncat|netcat|socat|telnet)\b[^\n;&|]*?(?<![\w.:])5037(?![\d])"
    r"|(?<![\w.])(?:curl|wget)\b[^\n;&|]*?:5037(?![\d])"
    r"|(?<![\w.:])5037\s*\)\s*\)")                                 # socket.create_connection((h, 5037))
_SHELL_OP = re.compile(r"&&|\|\||[;\n|&()]")


def _adb_global_selects_server(words: list[str]) -> bool:
    """Whether an adb invocation's GLOBAL options (those before the subcommand) select a
    different server. `words` are the tokens after the `adb` executable."""
    i = 0
    while i < len(words):
        w = words[i]
        if not w.startswith("-"):
            return False                 # the subcommand — device-side flags are its own
        if _ADB_GLOBAL_SELECT_OPT.match(w):
            return True
        i += 2 if w in _ADB_GLOBAL_VALUE_OPT else 1
    return False


def _adb_server_bypass(text: str) -> bool:
    """Whether `text` re-selects the adb server around the meter (QUA-2814)."""
    if _ADB_SERVER_ENV.search(text) or _ADB_RAW_SOCKET.search(text):
        return True
    for seg in _SHELL_OP.split(text):
        try:
            words = shlex.split(seg, posix=True)
        except ValueError:
            words = seg.split()
        adb = next((k for k, w in enumerate(words)
                    if w == "adb" or w.rsplit("/", 1)[-1] == "adb"), None)
        if adb is not None and _adb_global_selects_server(words[adb + 1:]):
            return True
    return False


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


# ── the agent's reach, resolved (QUA-2815) ────────────────────────────────────
#
# The absolute-token scan above sees `/…/runs/<case>/<other>/result.json` and nothing
# else. An agent whose cwd is `<runs>/<case>/<own>/workspace` reaches the sibling arm's
# verdict with `cat ../../*/result.json` and the run's arm index with `cat
# ../../../_runs/*/episodes/<own id>.json` — no absolute path in either. `_Reach` models
# the agent's shell well enough to resolve those: it tokenises each command (quote-aware),
# follows `cd`/`pushd`/`popd`/subshells/`sh -c`/`eval`, expands `$PWD`/`$OLDPWD`/`$(pwd)`
# and `~`/`$HOME` (to EVERY home the agent may have had: codex's is inside its episode),
# and joins every path-shaped word to the cwd it would have run in. Device-side text
# (after `adb shell`/`exec-out`) is never a host path. claude-code's Bash keeps its cwd
# between calls, and says so when it resets one ("Shell cwd was reset to …"); codex runs
# every command in a fresh shell at `--cd`, or at the call's `workdir`, which only the
# session rollout records. A cwd the model cannot follow (`cd "$(…)"`, `cd $VAR`) is a
# SOFT `unresolved_cwd`: relative paths after it are not guessed at. The arm-label
# tripwire in `scan` backs that up for the reads that print what they read.
# TODO(QUA-2815 follow-up): this is detection. A read after an unresolvable cwd whose
# output shows no label (`grep -c`, `wc`), or a program that computes a path itself, is
# still invisible. The wall is to deny the agent the runs tree (in the image: runs root
# and case dirs 0711 root-owned, `_runs/` 0700, finished episodes chowned back) — an
# OS-sandbox decision this ticket left out of scope.

_SHELL_PUNCT = "();<>|&\n"
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish"})
# Words that run the NEXT word as the command (their own options are skipped).
_WRAPPERS = frozenset({"sudo", "env", "command", "builtin", "exec", "time", "nohup", "nice",
                       "timeout", "stdbuf", "xargs", "then", "do", "else", "elif", "if",
                       "while", "until", "!", "{", "}", "done", "fi"})
# Commands whose arguments are never read as files (a redirect still is).
_NON_READERS = frozenset({"echo", "printf", ":", "true", "false", "sleep", "exit", "return",
                          "export", "unset", "set", "local", "declare", "alias", "type",
                          "which", "hash", "read", "wait", "kill", "test", "["})
# Readers that descend into a directory's CONTENTS. Over an ancestor of the runs root they
# read every episode's files without naming one (`grep -r task_id ~`).
_ALWAYS_RECURSIVE = frozenset({"rg", "ag", "ack", "rsync"})
_RECURSE_FLAG = re.compile(r"^-[A-Za-z]*[rR][A-Za-z]*$|^--recursive$|^--dereference-recursive$")
_FLAG_RECURSIVE = frozenset({"grep", "egrep", "fgrep", "zgrep", "ugrep", "cp", "scp", "zip"})
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete"})
_PWD_SUBST = re.compile(r"\$\(\s*pwd(?:\s+-[LP])?\s*\)|`\s*pwd(?:\s+-[LP])?\s*`")
_VAR_REF = re.compile(r"\$\{(PWD|OLDPWD|HOME)\}|\$(PWD|OLDPWD|HOME)(?![A-Za-z0-9_])")
# A relative path embedded in a longer word — code passed to `python -c`, `node -e`.
_EMBEDDED_REL = re.compile(r"(?<![\w.~$/-])\.\.(?:/[^\s'\"`;|&()<>,\]}]*)?")
_GLOB_CHARS = re.compile(r"[*?\[]")
_CWD_RESET = re.compile(r"Shell cwd was reset to (/[^\n\r\"]+)")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _shell_tokens(text: str) -> list[str]:
    """Quote-aware words and operator runs. `#` is left to the segment walker: shlex's
    commenter would cut `a#b` in half."""
    lex = shlex.shlex(text, posix=True, punctuation_chars=_SHELL_PUNCT)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:                       # unbalanced quote: fall back to a plain split
        return re.findall(r"[^\s();<>|&]+|[();<>|&\n]+", text)


def _is_punct(tok: str) -> bool:
    return bool(tok) and all(c in _SHELL_PUNCT for c in tok)


class _Reach:
    """Where one episode's tool calls reached on the host, as absolute paths.

    `paths(event)` returns `(path, how)` pairs: `how` is `plain` (classify as any
    other path), `recursive` (a reader descended into it — only an ANCESTOR of the runs
    root matters) or `unresolved` (a cwd change the model could not follow)."""

    def __init__(self, workspace: str, run_dir: str, home: str):
        self.ws = workspace
        self.run_dir = run_dir
        self.real_home = home
        codex_home = os.path.join(run_dir, "codex_home", "home")
        self.codex_home = codex_home if os.path.isdir(codex_home) else None
        # Every HOME the agent may have run under: the operator's (claude-code on a
        # host), codex's episode-local one, and the episode dir (the image's user drop).
        self.homes = [h for h in (home, self.codex_home, run_dir) if h]
        self.cwd: str | None = workspace
        self.old: str | None = None
        self.dirs: list[str | None] = []
        self.primary_home = home
        self.out: list[tuple[str, str]] = []

    # ── events ──
    def paths(self, event) -> list[tuple[str, str]]:
        self.out = []
        name = getattr(event, "name", "") or ""
        inp = getattr(event, "input", None)
        if not isinstance(inp, dict) or getattr(event, "server", "") or _is_mcp_name(name):
            return []                        # an MCP server resolves its own paths
        codex = name == "command_execution"
        if codex:
            # A fresh shell at `--cd` for every command.
            self.cwd, self.old, self.dirs = self.ws, None, []
            self.primary_home = self.codex_home or self.real_home
        else:
            self.primary_home = self.real_home
        shell_text = None
        for key in ("command", "cmd", "script"):
            val = inp.get(key)
            if isinstance(val, str) and val:
                shell_text = val
                break
        if shell_text is not None:
            self.shell(shell_text)
            if not codex:
                reset = _CWD_RESET.search(getattr(event, "result_text", "") or "")
                if reset:
                    self.cwd, self.dirs = os.path.normpath(reset.group(1).strip()), []
        else:
            self.tool(name, inp)
        return self.out

    def tool(self, name: str, inp: dict) -> None:
        """claude-code's file tools (Read/Write/Edit/Glob/Grep/LS/NotebookEdit): a
        relative path is relative to the session's cwd."""
        base = self.cwd or self.ws
        root = inp.get("path") if isinstance(inp.get("path"), str) else None
        if root:
            self.word(root, base=base, recursive=name == "Grep")
        elif name == "Grep":
            self.word(".", base=base, recursive=True)   # no path: it searches the cwd
        for key in ("file_path", "notebook_path"):
            val = inp.get(key)
            if isinstance(val, str) and val:
                self.word(val, base=base)
        pattern = inp.get("pattern")
        if name == "Glob" and isinstance(pattern, str) and pattern:
            glob_base = self._join(base, self._expand_one(root)) if root else base
            if glob_base:
                self.word(pattern, base=glob_base)
        if name == "Write" and isinstance(inp.get("content"), str):
            for tok in _EMBEDDED_REL.findall(inp["content"]):
                self.word(tok, base=base)

    # ── shell ──
    def shell(self, text: str, depth: int = 0) -> None:
        if depth > 6:
            return
        toks = _shell_tokens(_PWD_SUBST.sub("${PWD}", text))
        seg: list[str] = []
        redirects: list[str] = []
        frames: list[tuple] = []
        redirect_next = False
        whole = text
        for tok in toks:
            if _is_punct(tok):
                if "<" in tok or ">" in tok:
                    redirect_next = True
                    if "(" in tok:           # process substitution `<(…)`
                        self.segment(seg, redirects, whole, depth)
                        seg, redirects = [], []
                        frames.append((self.cwd, self.old, list(self.dirs)))
                    continue
                self.segment(seg, redirects, whole, depth)
                seg, redirects = [], []
                for ch in tok:
                    if ch == "(":
                        frames.append((self.cwd, self.old, list(self.dirs)))
                    elif ch == ")" and frames:
                        self.cwd, self.old, self.dirs = frames.pop()
                continue
            if redirect_next:
                redirect_next = False
                if not tok.isdigit() and tok != "-":
                    redirects.append(tok)
                continue
            seg.append(tok)
        self.segment(seg, redirects, whole, depth)
        while frames:                        # an unclosed `(` still leaves no cwd behind
            self.cwd, self.old, self.dirs = frames.pop()

    def segment(self, words: list[str], redirects: list[str], whole: str, depth: int) -> None:
        for r in redirects:
            self.word(r)
        if "#" in "".join(w[:1] for w in words):
            words = words[:next(i for i, w in enumerate(words) if w.startswith("#"))]
        # Leading assignments: their values may be paths (`F=../../x/result.json`).
        while words and _ASSIGN.match(words[0]):
            self.word(words[0].split("=", 1)[1])
            words = words[1:]
        while words:
            cmd = words[0].rsplit("/", 1)[-1]
            if cmd in _WRAPPERS:
                words = words[1:]
                if cmd == "env":
                    while words and (words[0].startswith("-") or _ASSIGN.match(words[0])):
                        words = words[2:] if words[0] in ("-u", "-C", "-S") else words[1:]
                elif cmd in ("sudo", "timeout", "nice", "stdbuf", "xargs"):
                    while words and words[0].startswith("-"):
                        words = words[1:]
                    if cmd == "timeout" and words:
                        words = words[1:]    # the duration
                continue
            break
        if not words:
            return
        cmd, args = words[0].rsplit("/", 1)[-1], words[1:]
        if cmd in ("cd", "pushd", "popd"):
            self.cd(cmd, args)
            return
        if cmd == "adb":
            self.adb(args)
            return
        if cmd in _SHELLS:
            script = next((args[i + 1] for i, a in enumerate(args[:-1])
                           if a.startswith("-") and not a.startswith("--") and "c" in a), None)
            if script is not None:
                saved = (self.cwd, self.old, list(self.dirs))
                self.shell(script, depth + 1)
                self.cwd, self.old, self.dirs = saved
                return
        if cmd == "eval":
            self.shell(" ".join(args), depth + 1)
            return
        if cmd in _NON_READERS:
            return
        recursive = (cmd in _ALWAYS_RECURSIVE
                     or (cmd in _FLAG_RECURSIVE and any(_RECURSE_FLAG.match(a) for a in args))
                     or (cmd == "tar")
                     or (cmd == "find" and (any(a in _FIND_EXEC for a in args)
                                            or re.search(r"\|\s*xargs\b", whole) is not None)))
        paths = 0
        for a in args:
            if a.startswith("-"):
                if a.startswith("--") and "=" in a:
                    self.word(a.split("=", 1)[1], recursive=recursive)
                continue
            if self.word(a, recursive=recursive):
                paths += 1
            for tok in _EMBEDDED_REL.findall(a):
                if tok != a:
                    self.word(tok)
        if recursive and not paths and cmd in ("rg", "ag", "ack", "grep", "egrep", "fgrep"):
            self.word(".", recursive=True)   # no path: they search the cwd

    def adb(self, args: list[str]) -> None:
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] in ("-s", "-t", "-H", "-P", "-L") else 1
        if i >= len(args):
            return
        sub, rest = args[i], [a for a in args[i + 1:] if not a.startswith("-")]
        if sub == "push":
            host = rest[:-1]
        elif sub == "pull":
            host = rest[1:]
        elif sub in ("install", "install-multiple", "sideload"):
            host = rest
        else:                                # shell / exec-out / logcat …: device side
            host = []
        for a in host:
            self.word(a)

    def cd(self, cmd: str, args: list[str]) -> None:
        args = [a for a in args if not (a.startswith("-") and a != "-" and not a[1:].isdigit())]
        if cmd == "popd":
            self.old, self.cwd = self.cwd, (self.dirs.pop() if self.dirs else None)
            if self.cwd is None:
                self._unresolved("popd")
            return
        if cmd == "pushd" and not args:
            self._unresolved("pushd")
            self.cwd = None
            return
        target = args[0] if args else "~"
        for form in self._expand(target):    # the cd target itself is reached
            self._emit(form, "plain")
        if target == "-":
            new = self.old
        else:
            new = self._join(self.cwd, self._expand_one(target, primary=True))
        if cmd == "pushd":
            self.dirs.append(self.cwd)
        if new is None:
            self._unresolved(f"{cmd} {target}")
        self.old, self.cwd = self.cwd, new

    # ── words ──
    def word(self, w: str, *, base: str | None = None, recursive: bool = False) -> bool:
        """Emit `w` as a host path if it is path-shaped; True when it was."""
        if not w:
            return False
        if not (w.startswith(("/", "~", "$")) or "/" in w or w in (".", "..")
                or _GLOB_CHARS.search(w)):
            return False
        base = self.cwd if base is None else base
        forms = self._expand(w)
        if not forms:
            return False
        emitted = False
        for form in forms:
            path = self._join(base, form)
            if path is None:
                if not form.startswith("/"):
                    self._unresolved(w)
                continue
            self._emit(path, "plain")
            if recursive:
                self._emit(path, "recursive")
            emitted = True
        return emitted

    def _expand(self, w: str) -> list[str]:
        """`w` with `$PWD`/`$OLDPWD`/`$HOME`/`~` substituted: one form per home. Empty
        when something else is still unexpanded (`$VAR`, a command substitution)."""
        forms = []
        for home in self.homes:
            s = w
            if s == "~" or s.startswith("~/"):
                s = home + s[1:]
            missing = False

            def sub(m, home=home):
                nonlocal missing
                var = m.group(1) or m.group(2)
                val = {"PWD": self.cwd, "OLDPWD": self.old, "HOME": home}[var]
                if val is None:
                    missing = True
                    return ""
                return val

            s = _VAR_REF.sub(sub, s)
            if missing or "$" in s or "`" in s:
                continue
            if s not in forms:
                forms.append(s)
            if w == s:                       # nothing home-dependent: one form will do
                break
        return forms

    def _expand_one(self, w: str | None, primary: bool = False) -> str | None:
        if w is None:
            return None
        forms = self._expand(w)
        if not forms:
            return None
        if primary and (w == "~" or w.startswith("~/") or "HOME" in w):
            for f in forms:
                if f.startswith(self.primary_home):
                    return f
        return forms[0]

    @staticmethod
    def _join(base: str | None, w: str | None) -> str | None:
        if w is None:
            return None
        if w.startswith("/"):
            return re.sub(r"^/{2,}", "/", os.path.normpath(w))
        if base is None:
            return None
        return os.path.normpath(os.path.join(base, w))

    def _emit(self, path: str, how: str) -> None:
        if (path, how) not in self.out:
            self.out.append((path, how))
        if how == "plain":
            # Symlinks the agent made: resolve on disk. Lexical `x/..` hides them.
            real = os.path.realpath(path)
            if real != path and (real, "real") not in self.out:
                self.out.append((real, "real"))

    def _unresolved(self, what: str) -> None:
        if (what, "unresolved") not in self.out:
            self.out.append((what, "unresolved"))

    # ── codex's per-call workdir ──
    def rollout_paths(self, call: tuple[str, str | None]) -> list[tuple[str, str]]:
        """One rollout shell call: a fresh shell at `--cd`, or at its `workdir`."""
        cmd, wd = call
        self.out = []
        self.cwd, self.old, self.dirs = self.ws, None, []
        self.primary_home = self.codex_home or self.real_home
        if wd:
            forms = self._expand(wd)
            for form in forms:
                path = self._join(self.ws, form)
                if path:
                    self._emit(path, "plain")
            self.cwd = self._join(self.ws, forms[0]) if forms else None
            if self.cwd is None:
                self._unresolved(f"workdir {wd}")
        if cmd and self.cwd is not None:
            self.shell(cmd)
        return self.out

    def rollout_calls(self) -> list[tuple[str, str | None]]:
        """(command, workdir) of every shell call in the episode's codex rollouts."""
        root = os.path.join(self.run_dir, "codex_home", "sessions")
        calls: list[tuple[str, str | None]] = []
        if not os.path.isdir(root):
            return calls
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.startswith("rollout-") and f.endswith(".jsonl"):
                    calls += _rollout_shell_calls(os.path.join(dirpath, f))
        return calls


_CALL_TYPES = frozenset({"function_call", "custom_tool_call", "local_shell_call"})
_WORKDIR_IN_CODE = re.compile(r"""workdir["']?\s*[:=]\s*["']([^"']+)["']""")


def _rollout_shell_calls(path: str) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    try:
        with open(path, errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return out
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        payload = obj.get("payload") if isinstance(obj, dict) else None
        payload = payload if isinstance(payload, dict) else obj
        if not isinstance(payload, dict) or payload.get("type") not in _CALL_TYPES:
            continue
        args = payload.get("arguments") or payload.get("input") or payload.get("action")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                # Code-mode (`exec`): JS that calls the shell tool — take its workdirs.
                for wd in _WORKDIR_IN_CODE.findall(args):
                    out.append(("", wd))
                continue
        if not isinstance(args, dict):
            continue
        cmd = args.get("cmd") or args.get("command") or ""
        if isinstance(cmd, list):
            cmd = shlex.join(str(c) for c in cmd)
        wd = args.get("workdir") or args.get("working_directory")
        out.append((str(cmd), str(wd) if isinstance(wd, str) and wd else None))
    return out


def _glob_under(path: str, root: str) -> bool:
    """`_under`, where a glob component of `path` may stand for the root's component
    (`/Users/*/.qualgentbench/runs/x`, `~/.qualgentbench/*/…`). Used for HITS only —
    an exemption is never granted to a pattern."""
    if _under(path, root):
        return True
    if not _GLOB_CHARS.search(path):
        return False
    p, r = path.split("/"), os.path.normpath(root).split("/")
    return len(p) >= len(r) and all(fnmatch.fnmatchcase(a, b) or a == b
                                    for a, b in zip(r, p))


def _ancestor_of(path: str, root: str) -> bool:
    """`path` is a directory strictly above `root` (a recursive read of it reads root)."""
    path = path.rstrip("/") or "/"
    return path != root and (root.startswith(path.rstrip("/") + "/") or path == "/")


# A journey arm label: `<case>~seeded` / `<case>~clean`. After QUA-2806 nothing an agent
# may legitimately read carries one — only the harness-side index and other episodes'
# result.json / plan / schedule do.
_ARM_LABEL = re.compile(r"(?<![\w~-])[a-z0-9][a-z0-9._-]*~(?:seeded|clean)(?![\w-])")
_OLD_LAYOUT = re.compile(r"~(?:seeded|clean)(?:/|$)")


def scan(
    parser,
    workspace: str | Path | None,
    repo_root: str | Path | None = None,
    home: str | None = None,
    devloop_roots: list[str] | None = None,
    nonce: str | None = None,
    adbd_at_end: dict | None = None,
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
    anything under it stays readable.

    `adbd_at_end` is `provenance.adbd_at_end` (QUA-2795), read over the harness's own
    adb right after the agent exited. `rooted: true` means adbd ran as root when the
    agent stopped: a privilege change got past the meter, so the agent may have had a
    root shell and read the app's sandbox (the seeded flags among it). That is a HARD
    hit, `adbd_rooted` (QUA-2806) — the same void as the nonce, read off the device
    instead of the transcript. `root_primed` (the property set, adbd not restarted) gave
    no root shell yet and is recorded as a SOFT hit."""
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

    if isinstance(adbd_at_end, dict):
        if adbd_at_end.get("rooted") is True:
            report.hard.append({"kind": "adbd_rooted", "tool": "",
                                "detail": f"adbd uid {adbd_at_end.get('uid')} after the agent exited"})
        elif adbd_at_end.get("root_primed") is True:
            report.soft.append({"kind": "adbd_root_primed", "tool": "",
                                "detail": "service.adb.root=1 after the agent exited"})

    # QUA-2815: resolve what the agent's shell and file tools reached, relative paths
    # included, against a modelled cwd (`_Reach`); compare against every symlink form.
    reach = _Reach(ws_s, run_dir_s, home_s) if ws_s and run_dir_s else None
    own_forms = _root_forms([run_dir_s]) if run_dir_s else []
    runs_forms = _root_forms([runs_root_s]) if runs_root_s else []
    # Before QUA-2806 the cwd itself named the arm (`<case>~seeded/…`), so every echoed
    # path carried a label; the arm-label tripwire is for blinded episodes only.
    blinded = bool(ws_s) and not _OLD_LAYOUT.search(ws_s)
    work = [(e, None) for e in events]
    if reach is not None:
        work += [(None, call) for call in reach.rollout_calls()]

    for event, call in work:
        if event is None:
            # A codex shell call read back from its session rollout, with its workdir.
            name = "exec_command"
            for path, how in reach.rollout_paths(call):
                _classify_reach(report, seen_hard, seen_soft, name, path, how,
                                own_forms, runs_forms)
            continue
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
            if _adb_server_bypass(text):
                key = ("adb_server_bypass", name)
                if key not in seen_hard:
                    seen_hard.add(key)
                    report.hard.append({"kind": "adb_server_bypass", "tool": name,
                                        "detail": "adb server selected around the meter"})
                break

        # An arm label in a tool RESULT (QUA-2815): the harness-side index and every
        # other journey episode's result.json name `<case>~seeded|~clean`, and after
        # QUA-2806 nothing the agent may legitimately read does. Catches the reads the
        # path model cannot follow (a cwd built by command substitution, a program that
        # opens files itself) whenever they print what they read. The label itself is
        # never recorded.
        if blinded and _ARM_LABEL.search(getattr(event, "result_text", "") or ""):
            key = ("other_episode", "\0arm-label")
            if key not in seen_hard:
                seen_hard.add(key)
                report.hard.append({"kind": "other_episode", "tool": name,
                                    "detail": "a journey arm label appeared in a tool result"})

        cands = [(_expand(raw, home_s), "plain") for raw in _candidate_paths(event)]
        extra = reach.paths(event) if reach is not None else []
        for path, how in extra:
            if how != "plain":
                _classify_reach(report, seen_hard, seen_soft, name, path, how,
                                own_forms, runs_forms)
        cands += [(p, h) for p, h in extra if h == "plain"]
        for path, _how in cands:
            if not path.startswith("/"):
                continue
            if any(_under(path, r) for r in _DEVICE_ROOTS):
                continue
            if any(_under(path, r) for r in own_forms):
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
            if any(_glob_under(path, r) for r in runs_forms):
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


def _classify_reach(report: Contamination, seen_hard: set, seen_soft: set, name: str,
                    path: str, how: str, own_forms: list[str], runs_forms: list[str]) -> None:
    """The `_Reach` outcomes that are not an ordinary path. `plain` paths are classified
    by `scan`'s own loop (rollout calls come here whole and are forwarded)."""
    if how == "unresolved":
        key = ("unresolved_cwd", path)
        if key not in seen_soft:
            seen_soft.add(key)
            report.soft.append({"kind": "unresolved_cwd", "tool": name, "detail": path})
        return
    if how == "recursive":
        # A reader that descends into an ANCESTOR of the runs root reads every episode.
        hit = any(_ancestor_of(path, r) for r in runs_forms)
    else:
        # `plain` (from a rollout call) or `real` (a symlink resolved on disk): a hit
        # when it lands in the runs root outside this episode.
        hit = (any(_glob_under(path, r) for r in runs_forms)
               and not any(_under(path, r) for r in own_forms))
    if hit:
        key = ("other_episode", path)
        if key not in seen_hard:
            seen_hard.add(key)
            report.hard.append({"kind": "other_episode", "tool": name,
                                "detail": path if how != "recursive"
                                else f"recursive read over {path}"})


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
