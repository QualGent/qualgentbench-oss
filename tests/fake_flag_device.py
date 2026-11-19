"""A fake `episode_runner._adb` for the flags write (QUA-3546): `run-as <pkg> ...`
commands run for real under `sh` in a temp directory that stands in for the app's
data dir, so the write and its read-back see the same file. Everything else answers
rc 0 and no output, unless the test scripts transport drops."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


class FakeFlagDevice:
    def __init__(self, data_dir: Path, *, offline: int = 0, comes_back: bool = True,
                 reply: str = "adb: device offline\n"):
        self.data_dir = data_dir
        self.offline = offline          # how many run-as commands drop before one runs
        self.comes_back = comes_back
        self.reply = reply
        self.calls: list[str] = []

    @property
    def flags(self) -> Path:
        return self.data_dir / "files" / "qgb_flags.txt"

    def sent(self, needle: str) -> int:
        return sum(needle in c for c in self.calls)

    async def __call__(self, *args):
        cmd = " ".join(args)
        self.calls.append(cmd)
        if args[-1] == "get-state":
            return (0, "device\n") if self.comes_back else (1, "error: device offline\n")
        shell = args[-1]
        if len(args) >= 3 and args[-2] == "shell" and shell.startswith("run-as "):
            if self.offline > 0:
                self.offline -= 1
                return 1, self.reply
            argv = shlex.split(shell)[2:]           # drop `run-as <pkg>`
            proc = subprocess.run(argv, cwd=self.data_dir, capture_output=True, text=True,  # noqa: ASYNC221 - a test fake
                                  check=False)
            return proc.returncode, proc.stdout + proc.stderr
        return 0, ""
