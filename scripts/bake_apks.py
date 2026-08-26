#!/usr/bin/env python3
"""Download and verify every published benchmark APK into QGB_CACHE_DIR — the
Docker build step that makes the image carry the corpus. Exit 1 if any app with
an `apk:` block cannot be fetched or fails its sha256."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qualgentbench import bugs as bugmod  # noqa: E402
from qualgentbench.apps import fetch_seeded_apk  # noqa: E402


def main() -> int:
    failures = 0
    specs = bugmod.load_apps()
    for spec in specs:
        app_id = spec["app"]["id"]
        meta = spec.get("apk")
        if not meta:
            print(f"  -  {app_id:<14} no published APK (not hunt-ready; skipped)")
            continue
        try:
            path = fetch_seeded_apk(app_id, meta)
            print(f"  ok {app_id:<14} {path.stat().st_size // 1024:>7} KB  {path}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  !! {app_id:<14} {exc}")
    print(f"\n{len(specs) - failures} ready, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
