"""`qualgent-bench memory lint|render|snapshot`."""

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
