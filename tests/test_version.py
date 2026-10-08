"""`qualgentbench.__version__` is the shipped version (QUA-2917).

It once read a hard-coded 0.1.0 while pyproject.toml and `checkpoint.package_version()`
(what plan.json records) said 0.2.0, so every view manifest named the wrong version.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import qualgentbench
from qualgentbench import checkpoint

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_matches_pyproject_and_the_package_version():
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    assert qualgentbench.__version__ == declared == checkpoint.package_version()


def test_the_fallback_is_the_declared_version():
    # The literal in __init__.py is used only without an installed dist; keep it current.
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    src = (Path(qualgentbench.__file__)).read_text()
    assert f'__version__ = "{declared}"' in src
