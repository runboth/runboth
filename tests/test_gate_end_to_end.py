"""End-to-end tests of the commit gate, in throwaway git repositories.

    pytest tests/test_gate_end_to_end.py -v

Every one of these is a case that broke in real use on 2026-09-12 and is now pinned:

  * a genuine behaviour change must BLOCK
  * an equivalent rewrite must stay SILENT (the half that stops it being uninstalled)
  * a message claiming "behaviour unchanged" over a real change must block and SAY SO
  * a message naming the function must let it through
  * a cross-file caller must be reported even though its source never changed
  * a file in a language the gate cannot check must be NAMED, never silently passed
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "runboth" / "cli.py"

BEFORE = "def rate(units):\n    if units >= 100:\n        return 0.1\n    return 0.0\n"
AFTER_DIFFERENT = "def rate(units):\n    if units > 100:\n        return 0.1\n    return 0.0\n"
AFTER_SAME = ("def rate(units):\n"
              "    threshold = 100\n"
              "    return 0.1 if units >= threshold else 0.0\n")
CALLER = ("from pkg.rates import rate\n\n\n"
          "def total(unit_price, units):\n"
          "    return round(unit_price * units * (1 - rate(units)), 2)\n")


def git(repo, *a, check=True):
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)}: {r.stderr}")
    return r


class Result:
    """git forwards HOOK output to stderr, not stdout.

    Asserting on `.stdout` alone passes vacuously for anything the gate prints, which is how the
    first version of this file "passed" while checking nothing. `.out` is both streams joined,
    and every assertion below uses it deliberately.
    """

    def __init__(self, cp):
        self.returncode = cp.returncode
        self.out = (cp.stdout or "") + (cp.stderr or "")


def commit(repo, msg, env=None):
    git(repo, "add", "-A")
    full = {**os.environ, **(env or {})}
    return Result(subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", msg],
        capture_output=True, text=True, timeout=900, env=full))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (r / "pkg" / "rates.py").write_text(BEFORE, encoding="utf-8")
    (r / "pkg" / "invoice.py").write_text(CALLER, encoding="utf-8")
    git(r, "init", "-q", ".")
    commit(r, "base")
    out = subprocess.run([sys.executable, str(CLI), "install-hook", str(r)],
                         capture_output=True, text=True, timeout=300)
    assert (r / ".git" / "hooks" / "commit-msg").exists(), (out.stdout or '') + (out.stderr or '')
    return r


def test_real_change_blocks(repo):
    (repo / "pkg" / "rates.py").write_text(AFTER_DIFFERENT, encoding="utf-8")
    r = commit(repo, "chore: tidy the rates module")
    assert r.returncode != 0, "a real behaviour change must block"
    assert "BLOCKED" in r.out
    assert "rate(100)" in r.out, r.out


def test_equivalent_rewrite_stays_silent(repo):
    """The half that decides whether anyone keeps it installed."""
    (repo / "pkg" / "rates.py").write_text(AFTER_SAME, encoding="utf-8")
    r = commit(repo, "chore: name the threshold")
    assert r.returncode == 0, f"an equivalent rewrite must commit:\n{r.out}"
    assert "BLOCKED" not in r.out


def test_false_no_change_claim_is_named(repo):
    (repo / "pkg" / "rates.py").write_text(AFTER_DIFFERENT, encoding="utf-8")
    r = commit(repo, "rate: pure refactor, behaviour unchanged")
    assert r.returncode != 0
    assert "says the behaviour did not change" in r.out, r.out


def test_declared_change_is_allowed(repo):
    (repo / "pkg" / "rates.py").write_text(AFTER_DIFFERENT, encoding="utf-8")
    r = commit(repo, "rate: move the volume break to strictly-greater-than")
    assert r.returncode == 0, f"a declared change must commit:\n{r.out}"
    assert "declared change" in r.out


def test_cross_file_caller_is_reported(repo):
    """`total` lives in another file and its source never changes. Nothing else finds this."""
    (repo / "pkg" / "rates.py").write_text(AFTER_DIFFERENT, encoding="utf-8")
    r = commit(repo, "chore: tidy")
    assert r.returncode != 0
    assert "did NOT touch" in r.out, r.out
    assert "total" in r.out, r.out


def test_unchecked_language_is_named_not_ignored(repo):
    """Silence must never cover a file the gate did not look at."""
    (repo / "web").mkdir()
    (repo / "web" / "cart.ts").write_text(
        "export function discount(u: number): number { return u >= 100 ? 0.1 : 0; }\n",
        encoding="utf-8")
    r = commit(repo, "web: add a discount helper")
    assert r.returncode == 0, r.out
    assert "NOT checked" in r.out, r.out
    assert "TypeScript" in r.out, r.out


def test_docs_only_commit_is_fast_and_quiet(repo):
    (repo / "README.md").write_text("# hi\n", encoding="utf-8")
    r = commit(repo, "docs: readme")
    assert r.returncode == 0
    assert "BLOCKED" not in r.out


def test_test_only_commit_is_fast_and_names_what_it_skipped(repo):
    """Staging only test changes must not hang the terminal, and must not pass silently either.

    Measured 2026-09-12: three of eight more-itertools commits spent over 700 seconds and returned
    nothing, and every one touched only `tests/test_more.py`. A gate that freezes a commit for ten
    minutes is uninstalled the same day. Staging test edits between two real commits is the most
    ordinary thing a developer does.
    """
    (repo / "tests").mkdir()
    (repo / "tests" / "test_rates.py").write_text(
        "from pkg.rates import rate\n\n\n"
        "def test_rate():\n"
        "    assert rate(100) == 0.1\n",
        encoding="utf-8")
    r = commit(repo, "tests: cover the volume break")
    assert r.returncode == 0, f"a test-only commit must go through:\n{r.out}"
    assert "BLOCKED" not in r.out
    assert "NOT checked" in r.out, f"the skipped file must be NAMED, not silently dropped:\n{r.out}"
    assert "test_rates.py" in r.out, r.out


def test_noxfile_is_not_adjudicated(repo):
    """Task runners import dev-only dependencies and abstain en masse. 51 of 55 abstentions on
    pypa/packaging were `No module named 'nox'` from a single `noxfile.py`."""
    (repo / "noxfile.py").write_text(
        "import nox\n\n\n"
        "@nox.session\n"
        "def tests(session):\n"
        "    session.run('pytest')\n",
        encoding="utf-8")
    r = commit(repo, "add a nox session")
    assert r.returncode == 0, r.out
    assert "noxfile.py" in r.out, f"a skipped build file must still be named:\n{r.out}"


def test_skip_env_bypasses(repo):
    (repo / "pkg" / "rates.py").write_text(AFTER_DIFFERENT, encoding="utf-8")
    git(repo, "add", "-A")
    r = subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-m", "chore: tidy"],
                       capture_output=True, text=True, timeout=900,
                       env={**os.environ, "RUNBOTH_SKIP": "1"})
    assert r.returncode == 0, "RUNBOTH_SKIP must always let a commit through"
