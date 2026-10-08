"""QualGentBench — benchmark for evaluating coding agents on mobile QA tasks."""

from importlib.metadata import PackageNotFoundError, version

try:
    # The dist metadata, so the module constant cannot drift from pyproject.toml again
    # (it read 0.1.0 while the package shipped 0.2.0; QUA-2917). The fallback is for a
    # source tree on sys.path with no installed dist; tests/test_version.py pins both.
    __version__ = version("qualgentbench")
except PackageNotFoundError:  # pragma: no cover - only without an installed dist
    __version__ = "0.2.0"
