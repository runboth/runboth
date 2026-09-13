"""The tool must not report differences that the tool itself created.

    pytest tests/test_measurement_artifacts.py -v

The first test here pins a real defect, found on 2026-09-12 by the CI job that runs RunBoth on
RunBoth, on its very first run. It presented as success, like every other defect this project has
had. The root cause is worth naming: measuring a function requires putting it somewhere and
running it, and the somewhere leaks into the answer. Two versions are executed out of two
different temporary trees, because that is the only way to have both on disk at once, so a
function whose output embeds its own location differs between the runs no matter what its code
does. In a repository of developer tooling that is not a rare shape.

The second test is a standing guard on a property, not a fix for a defect. See its docstring: a
"defect" recorded alongside the first one turned out to be a mistake in how the output was being
read, and the guard written for it was reverted.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "runboth" / "cli.py"
PR_REPORT = ROOT / "runboth" / "pr_report.py"
MARKER = "<!-- runboth-report -->"

# `where` never changes. It reports its own directory, which is different in every materialised
# tree by construction. `rate` is the real edit that gives the commit something to find.
BEFORE = '''from pathlib import Path


def where():
    return "root=" + Path(__file__).resolve().parent.as_posix()


def rate(units):
    if units >= 100:
        return 0.1
    return 0.0
'''

AFTER = BEFORE.replace("if units >= 100:", "if units > 100:")


def git(repo, *a):
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(a)}: {r.stderr}"
    return r.stdout.strip()


def commit(repo, msg):
    git(repo, "add", "-A")
    cp = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", msg],
        capture_output=True, text=True, timeout=900)
    return cp.returncode, (cp.stdout or "") + (cp.stderr or "")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (r / "pkg" / "loc.py").write_text(BEFORE, encoding="utf-8")
    git(r, "init", "-q", ".")
    commit(r, "base")
    return r


def test_self_locating_function_is_not_a_false_positive(repo):
    """A function that returns its own path, with unchanged source, must not be called changed.

    Before the fix this reported `where` as changed on every commit that touched its file, with a
    witness whose two sides differed only in a temp directory name the tool had just invented.
    That is the tool lying, not the tool giving up, which is the one failure this project cannot
    afford: it appeared in RunBoth's own dogfood report as the first thing a visitor would read.
    """
    (repo / "pkg" / "loc.py").write_text(AFTER, encoding="utf-8")
    out = subprocess.run([sys.executable, str(CLI), "install-hook", str(repo)],
                         capture_output=True, text=True, timeout=300)
    assert (repo / ".git" / "hooks" / "commit-msg").exists(), out.stdout + out.stderr

    code, text = commit(repo, "chore: tidy")
    assert code != 0, f"the real change to rate must still block:\n{text}"
    assert "rate" in text, text
    assert "where" not in text, (
        "`where` was reported, and its source never changed. The difference is the "
        f"materialisation directory, which the measurement created:\n{text}")


def test_paths_are_stripped_in_both_spellings():
    """A root recorded in one spelling must still be stripped when observed in another.

    Found 2026-09-12 by this project's own CI, on the GitHub Windows runner and nowhere else.
    That runner's user is `runneradmin`, which exceeds eight characters and therefore carries a
    `RUNNER~1` 8.3 alias; `mkdtemp` handed back one spelling while `Path.resolve()` inside the
    measured function produced the other, so no substring matched and the tool reported a
    function whose source had not changed. The developer's own username is `info`, which has no
    alias, so it passed locally every time.

    This pins the general property rather than the Windows specifics: separator style, escaping,
    and the resolved form of a root all have to be stripped.
    """
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "runboth"))
    from sandbox import _strip_measurement_paths

    root = r"C:\Users\runneradmin\AppData\Local\Temp\runboth_tree_HEAD_abc"
    observed = [
        "root=C:/Users/runneradmin/AppData/Local/Temp/runboth_tree_HEAD_abc/pkg",   # as_posix
        r"root=C:\Users\runneradmin\AppData\Local\Temp\runboth_tree_HEAD_abc\pkg",  # native
        r"root=C:\\Users\\runneradmin\\AppData\\Local\\Temp\\runboth_tree_HEAD_abc",  # repr'd
    ]
    out = _strip_measurement_paths(observed, [root])
    for got in out:
        assert "runboth_tree_HEAD_abc" not in got, got
        assert "<tree>" in got, got

    keep = _strip_measurement_paths(["root=/srv/app/pkg"], [root])
    assert keep == ["root=/srv/app/pkg"], "an unrelated path must be left alone"


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names are a Windows filesystem thing")
def test_short_name_root_strips_a_resolved_long_path():
    """THE actual runner failure, reproduced rather than described.

    `mkdtemp` can hand back a path containing an 8.3 alias while `Path.resolve()` inside the
    measured function expands it to the long form. Two spellings of one directory, no substring
    match, and the tool reports a function whose source never changed.

    This needs a REAL directory, because Windows only has a short name for a path that exists.
    An earlier version of this test used a made-up path and passed with the fix removed, which
    is the vacuous-assertion trap this project has now fallen into three times.
    """
    import ctypes
    import shutil
    import tempfile
    from ctypes import wintypes

    long_dir = tempfile.mkdtemp(prefix="runboth_tree_longname_probe_")
    try:
        GetShort = ctypes.windll.kernel32.GetShortPathNameW
        GetShort.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        buf = ctypes.create_unicode_buffer(1024)
        if not GetShort(long_dir, buf, 1024) or buf.value == long_dir:
            pytest.skip("this filesystem has 8.3 alias creation disabled")
        short_dir = buf.value

        sys.path.insert(0, str(ROOT / "runboth"))
        from sandbox import _strip_measurement_paths

        # THE PRODUCTION DIRECTION, and only this one. The harness records whatever spelling it
        # was handed for the tree; the measured function calls `Path(__file__).resolve()` and
        # emits the resolved spelling. So: record one form, observe that same directory's
        # resolved form.
        #
        # An earlier version of this test recorded the fully-short form and observed a MIXED one
        # (short user component, long tail), which is a combination nothing in the product
        # produces. It failed on the runner for that reason alone, while the real gate test it
        # was written to protect had already gone green.
        recorded = short_dir
        observed_dir = str(Path(recorded).resolve())
        if observed_dir == recorded:
            pytest.skip("no short/long divergence to exercise on this filesystem")

        observed = ["root=" + observed_dir.replace("\\", "/") + "/pkg"]
        out = _strip_measurement_paths(observed, [recorded])
        assert "<tree>" in out[0], (
            "a root recorded in one spelling must still strip its own resolved form:\n"
            f"  recorded: {recorded}\n  observed: {out[0]}")
        assert "runboth_tree_longname_probe_" not in out[0], out[0]
    finally:
        shutil.rmtree(long_dir, ignore_errors=True)


def test_only_the_report_reaches_stdout(repo):
    """stdout is a machine-read artifact: the Action captures it with `> report.md`.

    This test exists as a standing guard, NOT because a leak was found. A second "defect" was
    briefly recorded here on 2026-09-12 claiming that a child process wrote the engine's control
    suite into the middle of the report. That was wrong, and the error was mine rather than the
    product's: the command used to look at the report merged the two streams with `2>&1`, so
    output that had always been on stderr appeared inside it. `pr_report` forwards the child's
    stderr to stderr deliberately. A guard written for that imaginary leak was reverted.

    It survives as a test because the property is worth pinning cheaply, and because the way the
    mistake was caught is the rule that caught it: the guard was disabled and the test still
    passed, which is the definition of a vacuous assertion.
    """
    (repo / "pkg" / "loc.py").write_text(AFTER, encoding="utf-8")
    commit(repo, "change rate")
    head = git(repo, "rev-parse", "HEAD")
    base = git(repo, "rev-parse", "HEAD~1")

    cp = subprocess.run(
        [sys.executable, str(PR_REPORT), "--repo", str(repo),
         "--base", base, "--head", head, "--budget", "12"],
        capture_output=True, text=True, timeout=1800,
        env={**os.environ, "RUNBOTH_WORKERS": "2"})

    assert cp.returncode == 0, cp.stderr[-1500:]
    assert cp.stdout.lstrip().startswith(MARKER), (
        f"stdout must open with the report marker and carry nothing else:\n{cp.stdout[:400]}")
