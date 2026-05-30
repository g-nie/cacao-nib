"""On-disk result cache: skip files unchanged since they last linted clean.

Much inspired by Black's cache (https://github.com/psf/black/blob/main/src/black/cache.py)
"""

import contextlib
import functools
import hashlib
import os
import pickle
import tempfile
from importlib import metadata
from pathlib import Path
from typing import NamedTuple

CACHE_DIR_ENV = "NIB_CACHE_DIR"
DEFAULT_CACHE_DIR = ".cacao_nib_cache"


class FileData(NamedTuple):
    st_mtime: float
    st_size: int
    hash: str  # sha256 hex of the file's bytes


@functools.cache
def _nib_version() -> str:
    try:
        return metadata.version("cacao-nib")
    except metadata.PackageNotFoundError:
        return "unknown"


def ruleset_hash(rule_classes) -> str:
    """Stable short hash of *which* rules run, so a different enabled set (a
    different `--select`/`--ignore`, or a different plugin list) lands in a
    different cache file and the two never mix.

    Keyed on rule identity (module, qualname, code, group), not rule *source*:
    editing a rule's body without changing its identity won't invalidate the
    cache. `nib`'s own version is baked into the cache path (a new release →
    fresh cache); while iterating on plugin rule logic, pass `--no-cache`.
    """
    parts = sorted(
        f"{c.__module__}.{c.__qualname__}:{c.code}:{c.group}" for c in rule_classes
    )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def cache_file(ruleset: str, cache_dir: str | None = None) -> Path:
    """Resolve the cache file path. The nib version sits in the path so a new
    release throws the whole cache away at once (no per-entry invalidation).
    `cache_dir` (the `--cache-dir` flag) wins over `$NIB_CACHE_DIR`, which wins
    over the default `./.cacao_nib_cache`."""
    base = Path(cache_dir or os.environ.get(CACHE_DIR_ENV) or DEFAULT_CACHE_DIR)
    return base / _nib_version() / f"cache.{ruleset}.pickle"


def load(path: Path) -> dict[str, FileData]:
    """Load the cache, or return an empty one if it's missing or unreadable.
    A corrupt or stale-format pickle is treated as a cold cache, not an error."""
    try:
        with path.open("rb") as f:
            data = pickle.load(f)
    except OSError, pickle.UnpicklingError, EOFError, AttributeError, ImportError:
        return {}
    return data if isinstance(data, dict) else {}


def _hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def is_changed(path: str, cache: dict[str, FileData]) -> bool:
    """Whether `path` differs from its cached `FileData`. A matching mtime is the
    fast path (no read at all); on an mtime miss we sha256 the contents, so a git
    checkout or branch switch that rewrites mtimes without touching the bytes
    still counts as unchanged rather than triggering a needless re-lint.

    Uses `os.stat` on the plain str path, not `pathlib` — pathlib in this
    per-file loop was a measured bottleneck for Black (black#1950).
    """
    old = cache.get(os.path.abspath(path))
    if old is None:
        return True
    st = os.stat(path)
    if st.st_size != old.st_size:
        return True
    if st.st_mtime == old.st_mtime:
        return False
    return _hash(path) != old.hash


def record(path: str, cache: dict[str, FileData]) -> None:
    """Store `path`'s current `FileData`. Call only for files that linted clean —
    a file in the cache means "passed with zero diagnostics at this content"."""
    st = os.stat(path)
    cache[os.path.abspath(path)] = FileData(st.st_mtime, st.st_size, _hash(path))


def _ensure_gitignore(cache_root: Path) -> None:
    """Drop a `*` .gitignore at the cache root.
    Idempotent, only written if absent."""
    gitignore = cache_root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("# Automatically created by nib.\n*\n")


def write(path: Path, cache: dict[str, FileData]) -> None:
    """Atomically persist the cache: dump to a temp file in the same directory,
    then `os.replace` it into place, so a crash mid-write can't leave a
    half-written pickle that `load` would have to recover from."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_gitignore(path.parent.parent)  # cache root: <base>/<version>/cache.*
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
