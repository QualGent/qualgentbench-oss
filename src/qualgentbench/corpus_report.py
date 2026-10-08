"""`qualgent-bench corpus-report`: what the journey corpus IS at one corpus version, as data.

One JSON object per corpus version — counts, the defect-class mix against the plan's
targets, each app's journey APK hash — so a corpus can be described next to every
board that carries its `corpus_version`, without reading the YAML or the git history.

A pure reader of `test-cases/*.yaml` under a data root: no device, no network, no truth
file, no scorer. The default root is the packaged corpus; `--root` takes any tree with
the same layout, e.g. an older version exported with

    git archive <commit> src/qualgentbench/data | tar -x -C <dir>
    qualgent-bench corpus-report --json --root <dir>/src/qualgentbench/data

and `corpus_version` is the same hash `corpus.version_of` stamps on episodes (relative
paths and bytes only), so the export reproduces the number its boards carry.

The output is a function of the corpus bytes alone: no timestamp, no absolute path,
stable key order — the same tree gives byte-identical JSON. The mix is
`mix.report`'s arithmetic, not a copy of it.

The held-out split appears as three integers (cases, apps, seeded instances) when
QGB_HELDOUT_DIR names a split — never an app id, a case id, a defect id or a per-app
row, and never blended into the public numbers. Pointing `--root` at the held-out
directory itself is refused for the same reason (docs/heldout.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from qualgentbench import corpus, journey, mix

FORMAT = 1


class CorpusReportError(ValueError):
    """The root cannot be reported (no test-case files, or the held-out split)."""


def _docs(root: Path) -> dict[str, dict]:
    """{app id: parsed test-case file} for every `test-cases/<app>.yaml`, by app id."""
    out: dict[str, dict] = {}
    for path in sorted((root / "test-cases").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        out[path.stem] = doc if isinstance(doc, dict) else {}
    return out


def _cases(doc: dict) -> list[dict]:
    return [c for c in doc.get("test_cases") or [] if isinstance(c, dict)]


def app_counts(doc: dict) -> dict[str, Any]:
    """One app's row: cases, the ones a `bugs:` list seeds, seeded defect INSTANCES (one
    per `bugs:` entry, so a defect seeded on two cases counts twice and a two-bug case
    counts two — the catch-rate denominator's unit), declared defects, and the sha256
    of its journey APK (null when the file has no `apk:` block)."""
    cases = _cases(doc)
    bugs = [journey.case_bugs(c) for c in cases]
    apk = doc.get("apk")
    sha = apk.get("sha256") if isinstance(apk, dict) else None
    return {
        "cases": len(cases),
        "seeded": sum(1 for b in bugs if b),
        "instances": sum(len(b) for b in bugs),
        "defects": len(doc.get("defects") or []),
        "apk_sha256": str(sha) if sha else None,
    }


def heldout_totals() -> dict[str, int] | None:
    """`{cases, apps, instances}` over the held-out split, or None when QGB_HELDOUT_DIR
    is unset or holds no test-case file. Integers only — nothing that names an app."""
    d = corpus.heldout_dir()
    docs = _docs(d) if d and (d / "test-cases").is_dir() else {}
    if not docs:
        return None
    rows = [app_counts(doc) for doc in docs.values()]
    return {
        "cases": sum(r["cases"] for r in rows),
        "apps": len(rows),
        "instances": sum(r["instances"] for r in rows),
    }


def _is_heldout_root(root: Path) -> bool:
    candidates = [corpus.heldout_dir(), corpus.default_heldout_dir()]
    here = root.expanduser().resolve()
    return any(c is not None and c.expanduser().resolve() == here for c in candidates)


def build(root: Path | None = None) -> dict[str, Any]:
    """The report for the data root `root` (default: the packaged corpus)."""
    packaged = root is None or root.expanduser().resolve() == corpus.PACKAGED.resolve()
    root = corpus.PACKAGED if root is None else root.expanduser()
    if not packaged and _is_heldout_root(root):
        raise CorpusReportError(
            f"{root} is the held-out split; corpus-report publishes it only as totals "
            "(set QGB_HELDOUT_DIR and report the public corpus)")
    docs = _docs(root)
    if not docs:
        raise CorpusReportError(f"no test-case files under {root / 'test-cases'}")

    apps = {app_id: app_counts(doc) for app_id, doc in docs.items()}
    mix_rep = mix.report(root, {app_id: mix.defect_rows(doc) for app_id, doc in docs.items()})
    cases = sum(a["cases"] for a in apps.values())
    seeded = sum(a["seeded"] for a in apps.values())
    return {
        "format": FORMAT,
        "corpus_version": mix_rep["corpus_version"],
        "generated_from": "packaged" if packaged else "root",
        "public": {
            "cases": cases,
            "seeded_cases": seeded,
            "seeded_instances": sum(a["instances"] for a in apps.values()),
            "defects": sum(a["defects"] for a in apps.values()),
            "clean_only_cases": cases - seeded,
            "apps": apps,
        },
        "mix": {"corpus": mix_rep["corpus"], "apps": mix_rep["apps"]},
        "heldout": heldout_totals(),
        "targets": [{"bucket": name, "classes": list(classes), "target": target}
                    for name, classes, target in mix.BUCKETS],
    }


def dumps(report: dict[str, Any]) -> str:
    """The canonical serialisation: what `--json` prints and `--out` writes."""
    return json.dumps(report, indent=2) + "\n"
