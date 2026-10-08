"""The journey corpus's defect-class mix against the plan's buckets, as data.

A pure reader of `test-cases/*.yaml` under a data root: no device, no network, no
truth file, no scorer. It counts DECLARED defects (every `defects:` entry — the unit the
class mix is stated in); `on_a_case` counts the ones some case's `bugs:` switches on.

The one bucket table. `scripts/mix_report.py` prints it and `qualgent-bench
corpus-report` publishes it; neither keeps its own copy. The buckets, their targets and
where they come from are in docs/defect-classes.md; the vocabulary is
`journey.DEFECT_CLASSES`, the one `lint_journey_cases.py` enforces.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from qualgentbench import corpus, journey

# The plan's buckets this corpus is balanced against, in the order the epic reports them:
# (bucket, the classes that fill it, target % of all defects). display/content is the
# plan's layout 9 + widget inventory 8 + content and format 7. The targets sum to 87:
# the plan's other 13% (stale display 6, compatibility 4, dead control 3) has no class in
# the vocabulary, so a corpus drawn only from these classes runs over on every bucket —
# arithmetic, not a gap to chase (docs/defect-classes.md).
BUCKETS: tuple[tuple[str, tuple[str, ...], float], ...] = (
    ("crash", ("crash",), 24.0),
    ("display/content", ("layout", "widget-inventory", "content-format"), 24.0),
    ("persistence", ("persistence",), 14.0),
    ("navigation", ("navigation",), 11.0),
    ("lifecycle", ("lifecycle",), 7.0),
    ("ordering", ("ordering",), 4.0),
    ("ANR/freeze", ("anr", "stuck"), 3.0),
)
UNCLASSIFIED = "unclassified"


def bucket_of(cls: str | None) -> str:
    for name, classes, _ in BUCKETS:
        if cls in classes:
            return name
    return UNCLASSIFIED


def defect_rows(doc: dict) -> list[dict]:
    """`[{id, class, seeded}]` for every `defects:` entry, in file order. `class` is the
    authored value when it is in the vocabulary and None otherwise (missing, misspelt);
    `seeded` says whether some case's `bugs:` switches the defect on."""
    seeded = {b["id"] for case in doc.get("test_cases") or [] for b in journey.case_bugs(case)}
    rows: list[dict] = []
    for d in doc.get("defects") or []:
        d = d if isinstance(d, dict) else {}
        did = str(d.get("id") or "?")
        raw = d.get("class")
        rows.append({"id": did,
                     "class": raw if raw in journey.DEFECT_CLASSES else None,
                     "seeded": did in seeded})
    return rows


def load(root: Path, app_ids: list[str] | None = None) -> dict[str, list[dict]]:
    """{app id: defect rows} for every `test-cases/<app>.yaml` under `root`."""
    out: dict[str, list[dict]] = {}
    for path in sorted((root / "test-cases").glob("*.yaml")):
        if app_ids and path.stem not in app_ids:
            continue
        out[path.stem] = defect_rows(yaml.safe_load(path.read_text()) or {})
    return out


def _pct(n: int, total: int) -> float:
    return 100.0 * n / total if total else 0.0


def tally(rows: list[dict]) -> dict:
    """The mix of one set of defect rows: per bucket the count, how many of those a case
    seeds, the share of ALL rows (unclassified ones included, so the shares are honest),
    the target and the delta in points."""
    total = len(rows)
    buckets = []
    for name, classes, target in BUCKETS:
        mine = [r for r in rows if r["class"] in classes]
        share = _pct(len(mine), total)
        per_class = {c: sum(r["class"] == c for r in mine) for c in classes}
        buckets.append({
            "bucket": name,
            "classes": {c: k for c, k in per_class.items() if k},
            "n": len(mine),
            "on_a_case": sum(r["seeded"] for r in mine),
            "share": share,
            "target": target,
            "delta": share - target,
        })
    return {
        "defects": total,
        "on_a_case": sum(r["seeded"] for r in rows),
        "buckets": buckets,
        "unclassified": [r["id"] for r in rows if r["class"] is None],
        "unseeded": [r["id"] for r in rows if not r["seeded"]],
        "target_total": sum(t for _, _, t in BUCKETS),
    }


def report(root: Path, apps: dict[str, list[dict]]) -> dict:
    """Everything the report prints, as data (`--json`)."""
    return {
        "root": str(root),
        "corpus_version": corpus.version_of(root),
        "corpus": tally([r for rows in apps.values() for r in rows]),
        "apps": {app_id: tally(rows) for app_id, rows in apps.items()},
    }
