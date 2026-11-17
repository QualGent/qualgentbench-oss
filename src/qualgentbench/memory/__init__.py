"""Cross-run agent memory: the file format, renderer, lint and snapshots.

Org and app memory are Markdown files under a memory root:

    org/<org_key>.md                      org layer (Company, Users, What matters to them, Terminology)
    app/<org_key>/<app_key>.md            app layer (Navigation, Login and setup, Flaky or slow areas,
                                          Known accepted behavior, Past bugs, Notes)
    app/<org_key>/<app_key>.changes.md    optional, generated "what changed since the last build"

- `format`: parse / edit / write a file byte-for-byte (`load`, `loads`, `MemoryFile`).
- `render`: the `<qualgent_memory>` prompt block within a budget (`render`, `BUDGETS`).
- `lint`: the content and structure rules every file and curator output must pass.
- `snapshot`: content-addressed snapshots per stage and unit (`freeze`, `open_stage`).
"""

# `render` stays the submodule (`memory.render.render(...)`); the function is not
# re-exported here, because the package attribute would shadow the module.
from . import format, lint, render, snapshot
from .format import (
                     APP,
                     ORG,
                     Entry,
                     FormatError,
                     MemoryFile,
                     Section,
                     app_path,
                     changes_path,
                     find_org,
                     load,
                     loads,
                     new_file,
                     org_path,
)
from .lint import CorpusTerms, LintIssue, lint_file, lint_text, lint_tree
from .render import BUDGETS, MODES, Rendered, estimate_tokens, place_workspace
from .snapshot import (
                     Snapshot,
                     SnapshotError,
                     freeze,
                     open_snapshot,
                     open_stage,
                     pack,
                     snapshot_hash,
                     unit_files,
                     unpack,
)

__all__ = [
                     "APP",
                     "BUDGETS",
                     "MODES",
                     "ORG",
                     "CorpusTerms",
                     "Entry",
                     "FormatError",
                     "LintIssue",
                     "MemoryFile",
                     "Rendered",
                     "Section",
                     "Snapshot",
                     "SnapshotError",
                     "app_path",
                     "changes_path",
                     "estimate_tokens",
                     "find_org",
                     "format",
                     "freeze",
                     "lint",
                     "lint_file",
                     "lint_text",
                     "lint_tree",
                     "load",
                     "loads",
                     "new_file",
                     "open_snapshot",
                     "open_stage",
                     "org_path",
                     "pack",
                     "place_workspace",
                     "render",
                     "snapshot",
                     "snapshot_hash",
                     "unit_files",
                     "unpack",
]
