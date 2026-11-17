"""`qualgent-bench memory lint|render|snapshot|proposals|curate|metrics|health|tracks`."""

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
@click.option("--curation-log", "curation_logs", multiple=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="A `memory curate` operation log (repeatable, one per unit), for "
                   "proposals_accepted; see memory.metrics.curation_counts.")
@click.option("--json", "as_json", is_flag=True)
def metrics_cmd(runs_dir: Path, run_ids: tuple[str, ...], by_app: bool,
                curation_logs: tuple[Path, ...], as_json: bool) -> None:
    """Memory-experiment rows per (condition, stage) over saved journey results:
    steps_over_ref, completion, false alarms, recall, trap harm, attribution."""
    from ..leaderboard import load_results
    from .metrics import aggregate, dedupe_staged, load_curation_log

    results = load_results(runs_dir)
    if run_ids:
        results = [r for r in results if r.run_id in set(run_ids)]
    rows = aggregate(dedupe_staged(results), by_app=by_app,
                     curation=load_curation_log(*curation_logs) if curation_logs else None)
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


@memory.command("proposals")
@click.argument("paths", nargs=-1, required=True, type=click.Path(path_type=Path))
def proposals_cmd(paths: tuple[Path, ...]) -> None:
    """Validate memory_proposals.yaml files. Exit 1 on any error (a missing file is
    reported, not an error: an agent may propose nothing)."""
    from . import proposals as props

    bad = 0
    for p in paths:
        pf = props.load(p)
        if pf.missing:
            click.echo(f"{p}: missing (no proposals)")
            continue
        for e in pf.errors:
            click.echo(f"{p}: {e}")
        bad += len(pf.errors)
        click.echo(f"{p}: {len(pf.proposals)} valid proposal(s), {len(pf.errors)} error(s)")
    raise SystemExit(1 if bad else 0)


def _load_episodes(path: Path) -> list:
    """`--episodes` JSON: a list of {id, proposals, metrics, case?, verified?}.
    `proposals` is a path; `metrics` is a path to a result.json (its `metrics` block is
    read) or an inline dict. `verified` overrides the outcome read from the metrics.
    Relative paths resolve against the JSON file's directory."""
    from . import proposals as props
    from .curator import Episode, verified_outcome

    base = path.parent
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise click.UsageError(f"{path}: expected a JSON list of episodes")
    out = []
    for r in rows:
        metrics = r.get("metrics")
        if isinstance(metrics, str):
            doc = json.loads((base / metrics).read_text(encoding="utf-8"))
            metrics = doc.get("metrics", doc) if isinstance(doc, dict) else None
        ok, why = verified_outcome(metrics)
        if "verified" in r:
            ok, why = bool(r["verified"]), "set in the episodes file"
        out.append(Episode(id=str(r["id"]), verified=ok, verified_reason=why,
                           proposals=props.load(base / r["proposals"]),
                           case=r.get("case") or (metrics or {}).get("case_id")))
    return out


@memory.command("curate")
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--out", "out_root", required=True, type=click.Path(path_type=Path),
              help="New memory root for M_{k+1} (absent or empty).")
@click.option("--app", "app_key", required=True)
@click.option("--org", "org_key", default=None)
@click.option("--stage", required=True, help="The stage the proposals came from.")
@click.option("--date", required=True, help="YYYY-MM-DD for confirmed/invalidated.")
@click.option("--episodes", "episodes_path", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--model", default=None, help="Curator model id (default $QGB_CURATOR_MODEL).")
@click.option("--cache", "cache_dir", default=None, type=click.Path(path_type=Path),
              help="Cache of model answers; a re-run with the same inputs reuses them.")
@click.option("--log", "log_path", default=None, type=click.Path(path_type=Path),
              help="Operation log (default <out>.curator.json).")
@click.option("--exclude-case", "exclude", multiple=True,
              help="Skip episodes of this case (leakage rule). Repeatable.")
def curate_cmd(root: Path, out_root: Path, app_key: str, org_key: str | None, stage: str,
               date: str, episodes_path: Path, model: str | None, cache_dir: Path | None,
               log_path: Path | None, exclude: tuple[str, ...]) -> None:
    """Curate one unit's stage proposals into a new memory root and print its hash."""
    from .curator import CuratorError, curate, model_from_env

    org_key = org_key or fmt.find_org(root, app_key)
    episodes = _load_episodes(episodes_path)
    try:
        m = model_from_env(cache_dir=cache_dir, model=model)
    except CuratorError:
        m = None    # fine when the gate decides everything; curate() says so otherwise
    log_path = log_path or out_root.with_name(out_root.name + ".curator.json")
    try:
        r = curate(root, out_root, org_key=org_key, app_key=app_key, episodes=episodes,
                   model=m, stage=stage, date=date, exclude_cases=exclude,
                   log_path=log_path)
    except CuratorError as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps({"snapshot": r.snapshot, "parent": r.parent,
                           "carried_forward": r.carried_forward, "counts": r.counts,
                           "log": str(log_path)}, indent=2))


@memory.command("tracks")
@click.argument("apps", nargs=-1)
@click.option("--json", "as_json", is_flag=True, help="Print each track's plan as JSON.")
@click.option("--flags", "flags_stage", default=None, metavar="STAGE",
              help="Print the stage's flag ids (defects, then drift), one per line, for "
                   "ONE app: the Qualock build's flag set.")
def tracks_cmd(apps: tuple[str, ...], as_json: bool, flags_stage: str | None) -> None:
    """Validate memory update tracks, pairs and traps (data/memory/tracks, traps).
    Exit 1 on any problem."""
    from . import tracks as trk

    if flags_stage:
        if len(apps) != 1:
            raise click.UsageError("--flags needs exactly one app")
        click.echo(trk.load_track(apps[0]).stage_flags(flags_stage).text(), nl=False)
        return
    problems = trk.validate_all()
    if apps:
        problems = {a: p for a, p in problems.items() if a in apps}
    if as_json:
        click.echo(json.dumps({a: {**trk.summary(trk.load_track(a)), "problems": p}
                               for a, p in problems.items()}, indent=2))
    else:
        for a, ps in problems.items():
            t = trk.load_track(a)
            click.echo(f"{a}: {len(t.stages)} stages, {len(t.orderings)} orderings, "
                       f"{len(t.pairs)} pairs, {len(t.traps)} traps"
                       + (f", {len(ps)} problem(s)" if ps else ", ok"))
            for p in ps:
                click.echo(f"  {p}")
    raise SystemExit(1 if any(problems.values()) else 0)
