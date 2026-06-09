"""Pytest fixtures shared across the test suite."""

import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

import nib
import nib.cli
from nib.cli import main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def run_cli(monkeypatch, capsys, tmp_path_factory):
    """Invoke `nib.cli.main()` in-process, returning a `CompletedProcess`-shaped
    namespace. Snapshots `Rule._registry`, `sys.path`, and `sys.modules` so each
    test starts with a clean rule registry and freshly-imported plugins."""
    # Start each CLI test with an empty registry — other test modules define
    # Rule subclasses at import time, and `_validate_registry` would reject
    # any leaked rule that lacks a `code`.
    registry_snapshot = list(nib.Rule._registry)
    nib.Rule._registry.clear()
    path_snapshot = list(sys.path)
    modules_snapshot = set(sys.modules)
    monkeypatch.setenv("NO_COLOR", "1")  # plain output
    # Isolate the on-disk result cache per test, off in its own temp dir — keeps
    # the repo clean (tests run from PROJECT_ROOT too) and tests independent.
    monkeypatch.setenv("NIB_CACHE_DIR", str(tmp_path_factory.mktemp("nib_cache")))

    def run(*args: str, cwd: Path = PROJECT_ROOT) -> SimpleNamespace:
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(sys, "argv", ["nib", *args])
        nib.cli._find_config.cache_clear()
        with warnings.catch_warnings():
            warnings.resetwarnings()
            warnings.simplefilter("always")
            try:
                rc = main()
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
        out, err = capsys.readouterr()
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    yield run

    nib.Rule._registry[:] = registry_snapshot
    sys.path[:] = path_snapshot
    for m in set(sys.modules) - modules_snapshot:
        del sys.modules[m]
    nib.cli._find_config.cache_clear()
