"""`qualgent-bench memory lint|render|snapshot|metrics|health`."""

from __future__ import annotations

import json
from pathlib import Path

import click

from . import format as fmt
from .lint import default_terms, lint_file, lint_tree
from .render import MODES, render
from .snapshot import memory_files, snapshot_hash, unit_files


@click.group("memory")
def memory() -> None:
    """Agent memory files: lint, render, snapshot hash."""


@memory.command("lint")
@click.argument("paths", nargs=-1, required=True,
                type=click.Path(exists=True, path_type=Path))
def lint_cmd(paths: tuple[Path, ...]) -> None:
    """Lint memory files, or every memory file under a memory root. Exit 1 on any
    issue."""
    terms = default_terms()
    issues = []
    for p in paths:
        issues += lint_tree(p, terms=terms) if p.is_dir() else lint_file(p, terms=terms)
    for i in issues:
        click.echo(str(i))
    n = len(paths) if not any(p.is_dir() for p in paths) else sum(
        len(memory_files(p)) if p.is_dir() else 1 for p in paths)
    click.echo(f"{len(issues)} issue(s) in {n} file(s)")
    raise SystemExit(1 if issues else 0)


@memory.command("render")
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--app", "app_key", required=True)
@click.option("--org", "org_key", default=None, help="Default: the org the app is filed under.")
@click.option("--budget", "mode", type=click.Choice(MODES), default="small")
@click.option("--changes", is_flag=True, help="Include <app>.changes.md (diff arm).")
@click.option("--json", "as_json", is_flag=True, help="Print metrics with the text.")
def render_cmd(root: Path, app_key: str, org_key: str | None, mode: str, changes: bool,
               as_json: bool) -> None:
    """Print the <qualgent_memory> block for one app."""
    org_key = org_key or fmt.find_org(root, app_key)
    r = render(root, org_key, app_key, mode, include_changes=changes)
    if as_json:
        click.echo(json.dumps({**r.metrics(), "text": r.text, "included": r.included,
                               "dropped": r.dropped}, indent=2))
    else:
        click.echo(r.text, nl=False)


@memory.command("snapshot")
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--app", "app_key", default=None, help="Hash only this app's rendered files.")
@click.option("--org", "org_key", default=None)
@click.option("--changes", is_flag=True)
def snapshot_cmd(root: Path, app_key: str | None, org_key: str | None,
                 changes: bool) -> None:
    """Print the snapshot hash of a memory root (or of one app's rendered files)."""
    files = None
    if app_key:
        files = unit_files(org_key or fmt.find_org(root, app_key), app_key,
                           include_changes=changes)
    click.echo(snapshot_hash(root, files))


@memory.command("metrics")
@click.argument("runs_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--run", "run_ids", multiple=True, help="Only these run ids (repeatable).")
@click.option("--by-app", is_flag=True, help="One row per (condition, stage, app).")
@click.option("--curation-log", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="Curator log (JSON lines / JSON / YAML) for "
                                 "proposals_accepted; see memory.metrics.curation_counts.")
@click.option("--json", "as_json", is_flag=True)
def metrics_cmd(runs_dir: Path, run_ids: tuple[str, ...], by_app: bool,
                curation_log: Path | None, as_json: bool) -> None:
    """Memory-experiment rows per (condition, stage) over saved journey results:
    steps_over_ref, completion, false alarms, recall, trap harm, attribution."""
    from ..leaderboard import load_results
    from .metrics import aggregate, dedupe_staged, load_curation_log

    results = load_results(runs_dir)
    if run_ids:
        results = [r for r in results if r.run_id in set(run_ids)]
    rows = aggregate(dedupe_staged(results), by_app=by_app,
                     curation=load_curation_log(curation_log) if curation_log else None)
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    cols = ("condition", "stage", *(("app",) if by_app else ()), "episodes", "completion",
            "steps_over_ref_mean", "false_alarm_rate", "recall", "trap_harm_rate",
            "trap_base_rate", "pair_recall")
    click.echo("\t".join(cols))
    for r in rows:
        click.echo("\t".join("—" if r.get(c) is None else str(r.get(c)) for c in cols))


@memory.command("health")
@click.argument("store", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True)
def health_cmd(store: Path, as_json: bool) -> None:
    """Memory health per (app, arm, rep, stage) from a snapshot store's stage pointers:
    entries, size, growth, invalidations, contradictions, lint failures, drops."""
    from .metrics import store_health

    rows = store_health(store)
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    cols = ("app", "arm", "rep", "stage", "entries", "tokens", "growth",
            "invalidation_rate", "contradiction_rate", "lint_failures")
    click.echo("\t".join(cols))
    for r in rows:
        click.echo("\t".join("—" if r.get(c) is None else str(r.get(c)) for c in cols)
                   + "\tdropped=" + json.dumps(r.get("dropped")))
