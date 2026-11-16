#!/usr/bin/env python3
"""One device-free gate to run before any derive: the app's text lints and the split guard.

Deriving a journey case costs emulator time (`derive_journey.py`, minutes per case, more
at `--repeat 3`). Each of the mistakes below is a text mistake that the derive cannot fix
and that a lint catches in seconds, so this runs the three device-free gates for the
app(s) being authored, in order, and stops at the first that fails:

  1. `scripts/lint_journey_cases.py --app <apps>`  witness/brief leaks, oracles, routes,
                                                   defect classes, side-bug references
  2. `scripts/lint_create_briefs.py --app <apps>`  every case's `brief:` is neutral
  3. `scripts/holdout.py verify`                   no held-out app or case id in the
                                                   public tree (exits 0 with no split)

    uv run python scripts/pre_derive_check.py --app myapp
    uv run python scripts/pre_derive_check.py --app myapp,otherapp --keep-going

Exit 0 when every gate passes, else the failing gate's exit code (1 for a lint failure).
`--keep-going` runs every gate and reports each instead of stopping at the first.
Each gate runs as its own process with this interpreter, so its output and exit code are
exactly what running it by hand prints. An app id with no test-case file (public or in
`QGB_HELDOUT_DIR`) is refused before any gate runs.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from qualgentbench import journey


def gates(app_ids: list[str]) -> list[tuple[str, list[str]]]:
    """(name, argv) for each gate, in the order they run."""
    apps = ",".join(app_ids)
    return [
        ("lint_journey_cases", [str(SCRIPTS / "lint_journey_cases.py"), "--app", apps]),
        ("lint_create_briefs", [str(SCRIPTS / "lint_create_briefs.py"), "--app", apps]),
        ("holdout verify", [str(SCRIPTS / "holdout.py"), "verify"]),
    ]


def run_gate(argv: list[str]) -> int:
    return subprocess.run([sys.executable, *argv], cwd=REPO_ROOT, check=False).returncode


def main(argv: list[str] | None = None, *, runner=run_gate) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--app", required=True, help="comma-separated app ids being authored")
    ap.add_argument("--keep-going", action="store_true",
                    help="run every gate even after one fails")
    args = ap.parse_args(argv)
    app_ids = [a.strip() for a in args.app.split(",") if a.strip()]
    if not app_ids:
        print("FAIL: --app names no app")
        return 2
    missing = [a for a in app_ids if not journey.has_cases(a)]
    if missing:
        print(f"FAIL: no test-case file for {', '.join(missing)} "
              "(public data/test-cases/ or $QGB_HELDOUT_DIR/test-cases/)")
        return 2

    failed: list[tuple[str, int]] = []
    for name, gate_argv in gates(app_ids):
        print(f"── {name} ──", flush=True)
        code = runner(gate_argv)
        if code != 0:
            failed.append((name, code))
            if not args.keep_going:
                break
        print(flush=True)
    if failed:
        print(f"PRE-DERIVE FAIL: {', '.join(f'{n} (exit {c})' for n, c in failed)} — "
              "fix before spending device time")
        return failed[0][1]
    print(f"PRE-DERIVE PASS: {', '.join(app_ids)} — every device-free gate is green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
