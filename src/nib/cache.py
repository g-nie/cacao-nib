"""On-disk result cache: replay diagnostics for files unchanged since last run.

Much inspired by Black's cache (https://github.com/psf/black/blob/main/src/black/cache.py)

Each entry pairs a file's `FileData` (the staleness key) with the diagnostics it
emitted last time. On an unchanged re-run the findings are identical, so we replay
the stored diagnostics instead of re-parsing the file. A clean file just stores an
empty diagnostic list.
"""

import contextlib
import functools
import hashlib
import os
import pickle
import tempfile
from importlib import metadata
from pathlib import Path
from typing import NamedTuple, Self

CACHE_DIR_ENV = "NIB_CACHE_DIR"
DEFAULT_CACHE_DIR = ".cacao_nib_cache"


# One emitted diagnostic, post-noqa: (lineno, col, code, message). Enough to
# re-print the exact `path:line:col` line on a cache hit without re-parsing.
Diag = tuple[int, int, str, str]

# A noqa-filtered deferred finding: a `Diag` plus the dotted module whose
# reachability gates it and the polarity it fires on. Its verdict is re-resolved
# every run (against the project's imported set), so caching it can't go stale
# when some *other* file's imports change — that's what makes a cross-file run
# cacheable. Print it via its first four fields; re-resolve via the last two.
DeferredDiag = tuple[int, int, str, str, str, bool]


class FileData(NamedTuple):
    st_mtime: float
    st_size: int
    hash: str  # sha256 hex of the file's bytes


# Cache value: the staleness key, the immediate diagnostics to replay on a hit,
# the file's import targets, and its (unresolved) deferred findings.
Entry = tuple[FileData, tuple[Diag, ...], tuple[str, ...], tuple[DeferredDiag, ...]]
Cache = dict[str, Entry]


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
    cache, so while iterating on plugin rule logic, pass `--no-cache`. `nib`'s
    own version is baked into the cache path too, so a new release starts from a
    fresh cache.
    """
    parts = sorted(
        f"{c.__module__}.{c.__qualname__}:{c.code}:{c.group}" for c in rule_classes
    )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def cache_file(
    ruleset: str, cache_dir: str | None = None, root: Path | None = None
) -> Path:
    """Resolve the cache file path. The nib version sits in the path so a new
    release throws the whole cache away at once (no per-entry invalidation).
    `cache_dir` (the `--cache-dir` flag) wins over `NIB_CACHE_DIR`, which wins
    over the default `.cacao_nib_cache` under `root` (the project root, so a run
    from a subdirectory reuses the project's single cache instead of creating a
    separate cache dir in each subdirectory it runs from). `root` defaults to
    the current directory."""
    base = Path(
        cache_dir
        or os.environ.get(CACHE_DIR_ENV)
        or (root or Path.cwd()) / DEFAULT_CACHE_DIR
    )
    return base / _nib_version() / f"cache.{ruleset}.pickle"


def load(path: Path) -> Cache:
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


def is_changed(path: str, cache: Cache) -> bool:
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
    file_data = old[0]
    st = os.stat(path)
    if st.st_size != file_data.st_size:
        return True
    if st.st_mtime == file_data.st_mtime:
        return False
    return _hash(path) != file_data.hash


def lookup(
    path: str, cache: Cache
) -> tuple[tuple[Diag, ...], tuple[str, ...], tuple[DeferredDiag, ...]] | None:
    """A cache hit returns `(diags, targets, deferred)` to replay/re-resolve
    (empty tuples for a file that linted clean); a miss or a changed file returns
    `None`, meaning the caller must actually check it."""
    if is_changed(path, cache):
        return None
    entry = cache[os.path.abspath(path)]
    return entry[1], entry[2], entry[3]


def record(
    path: str,
    cache: Cache,
    diags: tuple[Diag, ...],
    targets: tuple[str, ...],
    deferred: tuple[DeferredDiag, ...],
) -> None:
    """Store `path`'s current `FileData` alongside the immediate diagnostics it
    emitted, its import targets, and its (unresolved) deferred findings, so an
    unchanged re-run can replay them and re-resolve the deferred verdicts."""
    st = os.stat(path)
    cache[os.path.abspath(path)] = (
        FileData(st.st_mtime, st.st_size, _hash(path)),
        tuple(diags),
        tuple(targets),
        tuple(deferred),
    )


def _ensure_gitignore(cache_root: Path) -> None:
    """Drop a `*` .gitignore at the cache root.
    Idempotent, only written if absent."""
    gitignore = cache_root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("# Automatically created by nib.\n*\n")


def write(path: Path, cache: Cache) -> None:
    """Save the cache atomically: dump to a temp file in the same directory,
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


class Session:
    """The result cache for a single run: resolve+load on `open`, split files
    into replayable hits and to-check misses, collect each checked file's
    diagnostics, and persist on `flush`.

    A disabled session (`--no-cache`) carries a `None` path and does nothing —
    `partition` treats every file as a miss, `record`/`flush` are no-ops.
    """

    def __init__(self, path: Path | None):
        self._path = path
        self._data: Cache = load(path) if path is not None else {}

    @classmethod
    def open(
        cls,
        rule_classes,
        cache_dir: str | None,
        *,
        enabled: bool,
        root: Path | None = None,
    ) -> Self:
        path = (
            cache_file(ruleset_hash(rule_classes), cache_dir, root) if enabled else None
        )
        return cls(path)

    def partition(
        self, files: list[Path]
    ) -> tuple[
        dict[str, tuple[tuple[Diag, ...], tuple[str, ...], tuple[DeferredDiag, ...]]],
        list[Path],
    ]:
        """Split `files` into `({file_str: (diags, targets, deferred)}, [misses])`.
        A hit carries its import targets and deferred findings too, so the run can
        re-resolve cross-file verdicts without re-reading the file. With caching
        off, every file is a miss."""
        if self._path is None:
            return {}, files
        hits: dict[
            str, tuple[tuple[Diag, ...], tuple[str, ...], tuple[DeferredDiag, ...]]
        ] = {}
        misses: list[Path] = []
        for f in files:
            hit = lookup(str(f), self._data)
            if hit is None:
                misses.append(f)
            else:
                hits[str(f)] = hit
        return hits, misses

    def record(
        self,
        file_str: str,
        diags: tuple[Diag, ...],
        targets: tuple[str, ...],
        deferred: tuple[DeferredDiag, ...],
    ) -> None:
        """Remember a just-checked file's diagnostics, import targets, and deferred
        findings for next run."""
        if self._path is not None:
            record(file_str, self._data, diags, targets, deferred)

    def flush(self) -> None:
        """Persist the cache to disk (no-op when disabled)."""
        if self._path is not None:
            write(self._path, self._data)
