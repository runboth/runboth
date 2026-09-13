"""The version differ: what actually changed between two RELEASED versions of a package.

    pytest tests/test_versions.py -v

Most of this file tests the parts that do not need the network, because a test suite that only
passes when PyPI is reachable is a test suite that goes red for reasons nobody can fix. The one
test that does install from PyPI is marked and skips when offline.

Why this entry point exists at all: it needs no repository, no git history and no source access,
which means it answers the question a CONSUMER has rather than the one an author has. A version
bump's diff is one line in a lockfile, so no code reviewer, human or otherwise, can evaluate it.
"""
import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runboth"))

import versions  # noqa: E402


def _online(host="pypi.org", port=443, timeout=4):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ offline parts

def test_private_names_are_not_reported():
    """A consumer upgrading cares about the public surface, not internals.

    Reporting every private helper would bury the one finding that matters, which is the same
    noise problem that made constructor amplification and benchmark helpers worth fixing.
    """
    assert versions.is_public("pkg/mod.py::take")
    assert versions.is_public("pkg/mod.py::Cache.get")
    assert versions.is_public("pkg/mod.py::__init__")        # dunder, not private
    assert not versions.is_public("pkg/mod.py::_helper")
    assert not versions.is_public("pkg/mod.py::_Internal.get")
    assert not versions.is_public("pkg/mod.py::Cache._evict")


def test_package_files_skips_tests_and_build_files(tmp_path):
    """The same path filter the rest of the engine uses, so the two cannot drift."""
    sp = tmp_path / "site-packages"
    (sp / "mypkg").mkdir(parents=True)
    (sp / "mypkg" / "__init__.py").write_text("", encoding="utf-8")
    (sp / "mypkg" / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (sp / "mypkg" / "tests").mkdir()
    (sp / "mypkg" / "tests" / "test_core.py").write_text("", encoding="utf-8")
    (sp / "mypkg" / "conftest.py").write_text("", encoding="utf-8")

    files = versions.package_files(sp, "mypkg")
    assert "mypkg/core.py" in files
    assert not any("test" in f for f in files), files
    assert not any(f.endswith("conftest.py") for f in files), files


def test_a_hyphenated_package_maps_to_its_module_name(tmp_path):
    """`pip install python-slugify` gives you `slugify`, and `foo-bar` gives you `foo_bar`."""
    sp = tmp_path / "site-packages"
    (sp / "foo_bar").mkdir(parents=True)
    (sp / "foo_bar" / "__init__.py").write_text("", encoding="utf-8")
    (sp / "foo_bar" / "m.py").write_text("def g():\n    return 2\n", encoding="utf-8")

    assert "foo_bar/m.py" in versions.package_files(sp, "foo-bar")


def test_a_missing_package_yields_no_files_rather_than_raising(tmp_path):
    sp = tmp_path / "site-packages"
    sp.mkdir()
    assert versions.package_files(sp, "not-installed") == []


# ------------------------------------------------------------------ the real thing

@pytest.mark.skipif(os.environ.get("RUNBOTH_SKIP_NETWORK") == "1" or not _online(),
                    reason="needs PyPI")
def test_it_finds_a_real_undocumented_change_between_two_releases():
    """pydash 8.0.6 -> 8.1.0 changed the RETURN TYPE of `take` and `drop`.

    Found on 2026-09-13. The commit rewrote `take_while`/`drop_while` to build a list instead of
    slicing the input, and `take`/`drop` are one-line delegates to them, so their return type went
    from `str` to `list` for string input while their own source never changed a character. The
    changelog for 8.1.0 mentions `take_while` and `drop_while` only.

    This is the whole thesis in one assertion: a public function, a real released package, no diff
    anywhere for a reviewer to read, and the only way to see it is to run both.
    """
    records, err = versions.compare_versions("pydash", "8.0.6", "8.1.0",
                                             budget=30, limit=60, verbose=False)
    assert not err, err
    changed = {r["function"].split("::")[-1] for r in records
               if r["verdict"] == "changed" and r.get("witness")}
    assert "take" in changed or "drop" in changed, (
        f"expected take/drop among the changes, got: {sorted(changed)}")
