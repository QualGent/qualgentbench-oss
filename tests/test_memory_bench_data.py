"""QUA-3487: the hand-written bench memory under data/memory/ stays usable.

Every public journey app has exactly one org + app file, the whole tree passes the lint
against the real corpus terms, the default (`small`) render keeps every entry, past bugs
stay empty, and the files sit outside the corpus hash.
"""

from __future__ import annotations

import pytest

from qualgentbench import corpus
from qualgentbench.memory import format as fmt
from qualgentbench.memory import lint
from qualgentbench.memory import render as rnd

ROOT = corpus.PACKAGED / "memory"
APPS = corpus.public_apps()


def test_tree_passes_lint() -> None:
    issues = lint.lint_tree(ROOT)
    assert not issues, "\n".join(map(str, issues))


@pytest.mark.parametrize("app", APPS)
def test_app_small_render_keeps_every_entry(app: str) -> None:
    org = fmt.find_org(ROOT, app)
    assert fmt.org_path(ROOT, org).is_file()
    r = rnd.render(ROOT, org, app, rnd.SMALL)
    assert r.dropped == []
    assert 0 < r.tokens <= rnd.BUDGETS[rnd.SMALL]


@pytest.mark.parametrize("app", APPS)
def test_app_file_has_no_past_bugs(app: str) -> None:
    doc = fmt.load(fmt.app_path(ROOT, fmt.find_org(ROOT, app), app))
    assert [e for e in doc.entries() if e.type == "bug"] == []


def test_memory_is_outside_the_corpus_hash() -> None:
    assert not any(g.startswith("memory") for g in corpus.CORPUS_GLOBS)
