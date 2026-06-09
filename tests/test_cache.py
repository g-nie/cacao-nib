import os

from helpers import noqa_plugin, reported_codes

from nib import cache


def test_not_in_cache_is_changed(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    assert cache.is_changed(str(f), {}) is True


def test_recorded_then_untouched_is_unchanged(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    c: dict = {}
    cache.record(str(f), c, ())
    assert cache.is_changed(str(f), c) is False  # fast path: mtime matches


def test_size_differs_is_changed(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    c: dict = {}
    cache.record(str(f), c, ())
    f.write_text("x = 123456\n")  # longer → size mismatch, no read needed
    assert cache.is_changed(str(f), c) is True


def test_mtime_bumped_same_bytes_is_unchanged(tmp_path):
    # A git checkout rewrites mtime without touching content; the sha256
    # fallback turns that spurious mtime miss back into a hit.
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    c: dict = {}
    cache.record(str(f), c, ())
    st = os.stat(f)
    os.utime(f, (st.st_atime, st.st_mtime + 100))  # bump mtime, same bytes
    assert cache.is_changed(str(f), c) is False


def test_same_size_different_bytes_is_changed(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    c: dict = {}
    cache.record(str(f), c, ())
    st = os.stat(f)
    f.write_text("y = 2\n")  # identical length, different content
    os.utime(f, (st.st_atime, st.st_mtime + 100))  # force the hash path
    assert cache.is_changed(str(f), c) is True


# --- lookup (hit returns stored diags, miss returns None) ------------------


def test_lookup_miss_returns_none(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    assert cache.lookup(str(f), {}) is None


def test_lookup_hit_returns_stored_diags(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    c: dict = {}
    diags = ((1, 5, "X001", "no eval"),)
    cache.record(str(f), c, diags)
    assert cache.lookup(str(f), c) == diags


def test_lookup_clean_hit_returns_empty_not_none(tmp_path):
    # A clean file is a hit (replay nothing), distinct from a miss (re-check).
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    c: dict = {}
    cache.record(str(f), c, ())
    assert cache.lookup(str(f), c) == ()


def test_lookup_changed_file_returns_none(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    c: dict = {}
    cache.record(str(f), c, ((1, 1, "X", "m"),))
    f.write_text("x = 22\n")  # size differs → changed
    assert cache.lookup(str(f), c) is None


# --- load / write ----------------------------------------------------------


def test_write_then_load_round_trip(tmp_path):
    path = tmp_path / "nested" / "cache.pickle"  # parents created by write()
    c = {"/abs/a.py": (cache.FileData(1.5, 10, "deadbeef"), ((1, 2, "X001", "no"),))}
    cache.write(path, c)
    assert path.is_file()
    assert cache.load(path) == c


def test_write_drops_gitignore_at_cache_root(tmp_path):
    # write() gets <base>/<version>/cache.*.pickle; the ignore-all .gitignore
    # belongs at the cache root (<base>) so the whole tree stays uncommitted.
    path = cache.cache_file("h", cache_dir=str(tmp_path / "c"))
    cache.write(path, {})
    gitignore = tmp_path / "c" / ".gitignore"
    assert gitignore.read_text() == "# Automatically created by nib.\n*\n"


def test_load_missing_file_returns_empty(tmp_path):
    assert cache.load(tmp_path / "absent.pickle") == {}


def test_load_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "bad.pickle"
    path.write_bytes(b"this is not a pickle")
    assert cache.load(path) == {}  # treated as a cold cache, not an error


# --- ruleset hash / cache path ---------------------------------------------


class _R1:
    code = "X001"
    group = "X"


class _R2:
    code = "Y001"
    group = None


def test_ruleset_hash_is_order_independent():
    assert cache.ruleset_hash([_R1, _R2]) == cache.ruleset_hash([_R2, _R1])


def test_ruleset_hash_changes_with_enabled_set():
    assert cache.ruleset_hash([_R1]) != cache.ruleset_hash([_R1, _R2])


def test_cache_file_encodes_ruleset_and_version(monkeypatch, tmp_path):
    monkeypatch.delenv(cache.CACHE_DIR_ENV, raising=False)
    p = cache.cache_file("abc123", cache_dir=str(tmp_path))
    assert p.name == "cache.abc123.pickle"
    assert p.parent.parent == tmp_path  # <cache_dir>/<version>/cache.*.pickle


def test_cache_dir_env_is_used(monkeypatch, tmp_path):
    monkeypatch.setenv(cache.CACHE_DIR_ENV, str(tmp_path))
    assert str(cache.cache_file("h")).startswith(str(tmp_path))


def test_cache_dir_arg_overrides_env(monkeypatch, tmp_path):
    monkeypatch.setenv(cache.CACHE_DIR_ENV, str(tmp_path / "from_env"))
    p = cache.cache_file("h", cache_dir=str(tmp_path / "from_arg"))
    assert str(p).startswith(str(tmp_path / "from_arg"))


# --- end-to-end through the CLI --------------------------------------------


def test_unchanged_clean_file_is_skipped_on_rerun(run_cli, tmp_path):
    noqa_plugin(tmp_path)  # multirule fires on a Name with id == 'a'
    f = tmp_path / "x.py"
    f.write_text("z = 11\n")  # clean (no `a`)
    first = run_cli("check", ".", cwd=tmp_path)
    assert first.returncode == 0

    # Flip to dirty content of identical byte-length, then restore the mtime so
    # the cache sees the file as unchanged. The rerun must trust the cache and
    # skip it — proving the hit, since otherwise `a` would fire DEMO-style.
    st = os.stat(f)
    f.write_text("a = 11\n")  # same 7 bytes, now triggers the rule
    os.utime(f, (st.st_atime, st.st_mtime))
    cached_run = run_cli("check", ".", cwd=tmp_path)
    assert cached_run.returncode == 0
    assert reported_codes(cached_run.stdout) == set()


def test_dirty_file_diagnostics_are_replayed_from_cache(run_cli, tmp_path):
    noqa_plugin(tmp_path)
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")  # `a` fires all three rules → dirty, cached with diags
    first = run_cli("check", ".", cwd=tmp_path)
    assert first.returncode == 1
    assert reported_codes(first.stdout) == {"AAA001", "AAA002", "BBB001"}

    # Make it genuinely clean, but keep mtime/size so the cache sees it as
    # unchanged. The rerun must REPLAY the stored (dirty) diagnostics rather than
    # re-lint — otherwise it would now find nothing.
    st = os.stat(f)
    f.write_text("b = 1\n")  # same 6 bytes, now clean
    os.utime(f, (st.st_atime, st.st_mtime))
    replayed = run_cli("check", ".", cwd=tmp_path)
    assert replayed.returncode == 1
    assert reported_codes(replayed.stdout) == {"AAA001", "AAA002", "BBB001"}


def test_no_cache_rechecks_every_run(run_cli, tmp_path):
    noqa_plugin(tmp_path)
    f = tmp_path / "x.py"
    f.write_text("z = 11\n")
    assert run_cli("check", ".", "--no-cache", cwd=tmp_path).returncode == 0

    # Same trick, but with --no-cache the rerun ignores the (absent) cache and
    # actually re-lints, so the now-dirty file is reported.
    st = os.stat(f)
    f.write_text("a = 11\n")
    os.utime(f, (st.st_atime, st.st_mtime))
    rerun = run_cli("check", ".", "--no-cache", cwd=tmp_path)
    assert rerun.returncode == 1
    assert reported_codes(rerun.stdout) == {"AAA001", "AAA002", "BBB001"}
