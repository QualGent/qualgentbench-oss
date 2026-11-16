"""Per-app corpus quotas: how far each app is from the cases and defects planned for it.

A corpus grown by many app authors in parallel drifts from its plan one app at a time;
the global mix (`mix.report`) only shows the drift once it is large. This module reads a
QUOTA FILE — always an explicit path, never a file the repository ships — and reports,
per app and in total, the actual case and defect counts against the quota.

    format: 1                 # optional
    cases: 151                # optional: the case target for the whole reported root
    apps:
      myapp:
        cases: 12             # the app's case target
        defects:              # optional: declared-defect targets per mix bucket
          crash: 3            # (a bucket name from mix.BUCKETS, or a class from
          display/content: 3  #  journey.DEFECT_CLASSES — a class counts toward its bucket)
          persistence: 1

YAML or JSON (JSON is YAML). Defects are counted the way `mix` counts them: every
DECLARED `defects:` entry, by bucket; `on_a_case` says how many some case seeds.

The quota file is an input, not corpus data: it does not enter `corpus_version`. The
held-out split's quotas name held-out apps, so they must never live in the repository
(docs/heldout.md). Two guards hold that line: `check_location` refuses a quota file
inside the repository tree when the held-out split is reported, and `check_public`
refuses a quota file that names a held-out app when the PUBLIC corpus is reported (its
rows would print the held-out app's id into a report meant to be shared).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from qualgentbench import corpus, journey, mix

FORMAT = 1


class QuotaError(ValueError):
    """The quota file is missing, malformed, or may not be read for this report."""


def _bucket_key(key: str) -> str:
    for name, classes, _ in mix.BUCKETS:
        if key == name or key in classes:
            return name
    raise QuotaError(f"unknown defect bucket or class {key!r} (buckets: "
                     f"{', '.join(n for n, _, _ in mix.BUCKETS)}; classes: "
                     f"{', '.join(journey.DEFECT_CLASSES)})")


def _count(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QuotaError(f"{where} must be a non-negative integer, got {value!r}")
    return value


def parse(doc: Any, *, source: str = "quotas") -> dict[str, Any]:
    """The normalised quota document: `{cases: int|None, apps: {app: {cases, defects}}}`
    with every defect key folded into its mix bucket (bucket order, zero-filled)."""
    if not isinstance(doc, dict):
        raise QuotaError(f"{source}: expected a mapping with an `apps:` block")
    fmt = doc.get("format", FORMAT)
    if fmt != FORMAT:
        raise QuotaError(f"{source}: format {fmt!r} is not supported (expected {FORMAT})")
    unknown = sorted(set(doc) - {"format", "cases", "apps"})
    if unknown:
        raise QuotaError(f"{source}: unknown top-level key(s) {', '.join(unknown)}")
    raw_apps = doc.get("apps")
    if not isinstance(raw_apps, dict) or not raw_apps:
        raise QuotaError(f"{source}: `apps:` must be a non-empty mapping of app id to quota")
    total = doc.get("cases")
    apps: dict[str, dict[str, Any]] = {}
    for app_id, q in raw_apps.items():
        where = f"{source}: apps.{app_id}"
        if not isinstance(q, dict):
            raise QuotaError(f"{where}: expected a mapping with `cases:` and/or `defects:`")
        extra = sorted(set(q) - {"cases", "defects"})
        if extra:
            raise QuotaError(f"{where}: unknown key(s) {', '.join(extra)}")
        defects = {name: 0 for name, _, _ in mix.BUCKETS}
        raw_defects = q.get("defects") or {}
        if not isinstance(raw_defects, dict):
            raise QuotaError(f"{where}.defects: expected a mapping of bucket or class to count")
        for key, n in raw_defects.items():
            defects[_bucket_key(str(key))] += _count(n, f"{where}.defects.{key}")
        apps[str(app_id)] = {
            "cases": None if q.get("cases") is None else _count(q["cases"], f"{where}.cases"),
            "defects": defects if raw_defects else None,
        }
    return {"cases": None if total is None else _count(total, f"{source}: cases"),
            "apps": apps}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def in_repo(path: Path, repo_root: Path | None = None) -> bool:
    """True when `path` lies in the repository tree — outside the gitignored default
    held-out directory, which is the one place under the repo root a split may sit."""
    repo_root = repo_root or corpus.REPO_ROOT
    return _inside(path, repo_root) and not _inside(path, corpus.default_heldout_dir(repo_root))


def check_location(path: Path, *, heldout: bool, repo_root: Path | None = None) -> None:
    """Held-out quotas are never read from inside the repository."""
    if heldout and in_repo(path, repo_root):
        raise QuotaError(
            f"{path} is inside the repository; held-out quotas live outside it "
            "(docs/heldout.md) — move the file out and pass its path")


def heldout_apps() -> set[str]:
    """App ids with a test-case file in the configured or default held-out directory."""
    out: set[str] = set()
    for d in (corpus.heldout_dir(), corpus.default_heldout_dir()):
        out.update(corpus.apps_in(d))
    return out


def check_public(quotas: dict[str, Any]) -> None:
    """A public report never carries a held-out app's row. The error names no app."""
    leaked = set(quotas["apps"]) & heldout_apps()
    if leaked:
        raise QuotaError(
            f"the quota file names {len(leaked)} held-out app(s); report them with "
            "`scripts/mix_report.py --root \"$QGB_HELDOUT_DIR\" --quotas <file outside the "
            "repo>`, never in the public report")


def load(path: Path, *, heldout: bool = False, repo_root: Path | None = None) -> dict[str, Any]:
    """Read and parse a quota file. `heldout` says the report is of the held-out split."""
    path = path.expanduser()
    check_location(path, heldout=heldout, repo_root=repo_root)
    if not path.is_file():
        raise QuotaError(f"no quota file at {path}")
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise QuotaError(f"{path}: not YAML/JSON ({exc})") from exc
    quotas = parse(doc, source=path.name)
    if not heldout:
        check_public(quotas)
    return quotas


def _bucket_counts(rows: list[dict]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for name, classes, _ in mix.BUCKETS:
        mine = [r for r in rows if r["class"] in classes]
        out[name] = {"n": len(mine), "on_a_case": sum(r["seeded"] for r in mine)}
    return out


def _delta(actual: int, target: int | None) -> int | None:
    return None if target is None else actual - target


def _row(cases: int, rows: list[dict], quota: dict[str, Any]) -> dict[str, Any]:
    counts = _bucket_counts(rows)
    targets = quota["defects"]
    buckets = [{"bucket": name,
                "n": counts[name]["n"],
                "on_a_case": counts[name]["on_a_case"],
                "target": None if targets is None else targets[name],
                "delta": None if targets is None else counts[name]["n"] - targets[name]}
               for name, _, _ in mix.BUCKETS]
    defect_target = None if targets is None else sum(targets.values())
    return {
        "cases": cases,
        "cases_target": quota["cases"],
        "cases_delta": _delta(cases, quota["cases"]),
        "defects": len(rows),
        "defects_target": defect_target,
        "defects_delta": _delta(len(rows), defect_target),
        "buckets": buckets,
    }


def report(quotas: dict[str, Any], cases: dict[str, int],
           rows: dict[str, list[dict]]) -> dict[str, Any]:
    """Per-app and total actual-vs-quota. `cases` and `rows` are the reported root's
    `{app: case count}` and `{app: mix.defect_rows}`; a quota app with no test-case file
    yet reports zeros (not started), a corpus app with no quota is listed by id."""
    apps = {app_id: _row(cases.get(app_id, 0), rows.get(app_id, []), q)
            for app_id, q in quotas["apps"].items()}
    # Totals: the case target is the file's `cases:` when set, else the sum of the app
    # targets; the case actual is the WHOLE root (an unquota'd app still counts toward a
    # corpus-wide target). Bucket totals are over the quota'd apps only, where both sides
    # of the delta are defined.
    app_case_targets = [q["cases"] for q in quotas["apps"].values() if q["cases"] is not None]
    case_target = quotas["cases"] if quotas["cases"] is not None else (
        sum(app_case_targets) if app_case_targets else None)
    all_cases = sum(cases.values())
    bucket_targets = {name: sum((q["defects"] or {}).get(name, 0)
                                for q in quotas["apps"].values())
                      for name, _, _ in mix.BUCKETS}
    with_defect_quota = [a for a, q in quotas["apps"].items() if q["defects"] is not None]
    quota_rows = [r for a in with_defect_quota for r in rows.get(a, [])]
    counts = _bucket_counts(quota_rows)
    totals_buckets = [{"bucket": name, "n": counts[name]["n"],
                       "on_a_case": counts[name]["on_a_case"],
                       "target": bucket_targets[name],
                       "delta": counts[name]["n"] - bucket_targets[name]}
                      for name, _, _ in mix.BUCKETS] if with_defect_quota else []
    return {
        "apps": apps,
        "unquoted": sorted(set(cases) - set(quotas["apps"])),
        "total": {
            "cases": all_cases,
            "cases_target": case_target,
            "cases_delta": _delta(all_cases, case_target),
            "buckets": totals_buckets,
        },
    }


def _fmt(n: int | None, signed: bool = False) -> str:
    if n is None:
        return "-"
    return f"{n:+d}" if signed else str(n)


def render(rep: dict[str, Any]) -> list[str]:
    """The quota report as text lines (what `mix_report.py --quotas` and the text form of
    `corpus-report --quotas` print)."""
    header = (f"  {'app':20s} {'cases':>5s} {'target':>6s} {'delta':>6s}  "
              f"{'defects':>7s} {'target':>6s} {'delta':>6s}  bucket deltas (n/target)")
    lines = [header]
    for app_id, r in rep["apps"].items():
        deltas = ", ".join(f"{b['bucket']} {b['n']}/{b['target']}"
                           for b in r["buckets"] if b["target"] or b["n"]) or "-"
        if r["defects_target"] is None:
            deltas = "no defect quota"
        lines.append(f"  {app_id:20s} {r['cases']:5d} {_fmt(r['cases_target']):>6s} "
                     f"{_fmt(r['cases_delta'], True):>6s}  {r['defects']:7d} "
                     f"{_fmt(r['defects_target']):>6s} {_fmt(r['defects_delta'], True):>6s}  "
                     f"{deltas}")
    t = rep["total"]
    lines.append(f"  {'TOTAL':20s} {t['cases']:5d} {_fmt(t['cases_target']):>6s} "
                 f"{_fmt(t['cases_delta'], True):>6s}")
    for b in t["buckets"]:
        lines.append(f"    {b['bucket']:18s} {b['n']:3d} of {b['target']:3d} "
                     f"({b['delta']:+d}), {b['on_a_case']} on a case")
    if rep["unquoted"]:
        lines.append(f"  no quota: {', '.join(rep['unquoted'])}")
    return lines
