# Result caching design

Goal: on a re-run, skip files that haven't changed since they last passed, so
repeated `nib` invocations are near-instant on an unchanged tree. We follow
**Black's** approach because it solves the exact same problem (a per-file tool
asking "is this file unchanged since I last checked it?") with minimal
machinery.

Inspiration / reference implementation:
<https://raw.githubusercontent.com/psf/black/main/src/black/cache.py>

## Design

### Two-level key

1. **Cache file path** encodes the global invalidators, so a change there throws
   away the whole cache at once (no per-entry work):
   ```
   .cacao_nib_cache/<nib_version>/cache.<ruleset_hash>.pickle
   ```
   - `<nib_version>` in the path → a new release auto-invalidates everything.
   - `<ruleset_hash>` = stable hash of the enabled rule set + relevant config.
     Changing which rules run invalidates the cache.

2. **Per-file entry** detects content changes cheaply:
   ```python
   class FileData(NamedTuple):
       st_mtime: float
       st_size: int
       hash: str  # sha256 hex of contents
   ```

### `is_changed` (mirrors Black exactly)

```
not in cache         -> changed
size differs         -> changed
mtime matches        -> UNCHANGED   (fast path, no file read)
mtime differs        -> read + sha256; changed only if hash differs
```

The hash is the fallback that makes mtime-based caching safe: git checkouts and
branch switches rewrite mtimes without changing content, so the hash recheck
turns those spurious mtime misses back into hits instead of re-linting.

### What we store as the value

Phase 1: just membership — "this file was clean at this `FileData`." A file is
in the cache if it passed with zero diagnostics. Files with diagnostics are
**not** cached, so they're always re-checked (and re-reported) until fixed.

Phase 2 (only when `--fix` lands): store replayable diagnostics/edits per file
so a cache hit can still emit `--fix` output without re-parsing.

### Store format

A single pickle (highest protocol), one dict `{abspath: FileData}`, loaded in
one read and written **atomically** (write temp file, `os.replace`).

### On permissions

We do **not** store file permissions. File mode is orthogonal to lint output:
`chmod` doesn't change the AST, and editing content doesn't change the mode.
`(mtime, size, hash-fallback)` fully covers detecting content changes.

## Pitfalls to avoid (learned from Black)

Black hit both of these in production. They're cheap to sidestep if you design
for them up front:

1. **Don't key on mtime alone.** mtime-only logic gave Black false-positive
   cache hits ([black#875](https://github.com/psf/black/issues/875)) — a file
   looked unchanged when it wasn't. This is exactly why the SHA256 hash fallback
   above is mandatory, not optional.
2. **Don't stat via `pathlib` in the per-file hot loop.** `pathlib` made Black's
   cache lookups a real bottleneck ([black#1950](https://github.com/psf/black/issues/1950)).
   Use `os.stat` on plain `str` paths in `is_changed`.

## Integration (engine ← parallel ← cli)

All cache-specific logic lives in a new module, `src/nib/cache.py`: `FileData`,
`is_changed`, cache-path resolution, and the pickle load/atomic-dump. The
existing modules just call into it — keep `cache.py` self-contained so the
caching layer can be tested and swapped in isolation.

- **cli**: resolve cache path from version + ruleset hash; load once at startup;
  add `--no-cache` (skip load+store) and `--cache-dir` / `NIB_CACHE_DIR`.
- **engine/parallel**: before dispatching a file, `is_changed()`; skip clean
  unchanged files. After a worker returns zero diagnostics, record its
  `FileData`. Collect results and write the cache once at the end.
- Cache the `_max_workers()` result in-process (`functools.cache`) — that's a
  separate concern from the on-disk cache; it does not belong in the file cache.

## Cross-file rules (later)

Per-file `(mtime, size, hash)` is unsound for rules whose result depends on
other files (the project index — see `project_cross_file_rules.md`). When those
land, fold a hash of the relevant cross-file inputs into `<ruleset_hash>` so a
dependency change invalidates dependents, rather than building a real dependency
graph. Punt until cross-file rules actually exist.

## Scope

~40 lines in a new `src/nib/cache.py`: a dict, pickle load/atomic-dump, and an
`os.stat` mtime/size compare with a lazy sha256 fallback.
