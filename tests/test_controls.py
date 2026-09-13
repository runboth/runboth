"""The control suites, wrapped so CI can run them and a failure names which one broke.

    pytest tests/ -v

These are not new tests. They are the existing control suites, which are the real safety net:
every one contains cases that MUST fail, because a suite where everything passes proves nothing.
Sixteen defects in this project's history were caught by exactly these and not one produced an
error on its own.

The wrapper exists so CI gets a proper exit code and a named failure instead of a wall of text.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "runboth"
LEDGER = ROOT / "ledger"


def run(cmd, cwd, timeout=1800, env=None):
    import os
    e = {**os.environ, **(env or {})}
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout, env=e)


def test_engine_controls():
    """Comparison, inter-agent, sandbox, methods. Half the cases must come back `changed`."""
    r = run([sys.executable, str(PY / "cli.py"), "selftest"], PY)
    assert "ALL SUITES PASS" in r.stdout, r.stdout[-3000:] + r.stderr[-1500:]


def test_over_blocking_is_a_false_same_source():
    """The sandbox must not block so much that two different functions look identical.

    This one is worth its own test: it is the only control that demonstrates the tool LYING
    rather than merely giving up, and the class it guards has appeared three times.
    """
    r = run([sys.executable, str(PY / "control_overblock.py")], PY)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-1000:]
    assert "PASS" in r.stdout


@pytest.mark.skipif(not (LEDGER / "uvc" / "cli.py").exists(),
                    reason="the UVC ledger is not part of the published package")
def test_ledger_controls():
    """uvc's own suite, on BOTH executors, including the incremental-cache staleness case.

    The ledger is developed alongside the engine but is NOT shipped with it: nothing in
    `runboth/` imports it, and it is kept out of the public tree. This test therefore skips
    rather than fails when it is absent, which is what a clone sees.
    """
    r = run([sys.executable, "-m", "uvc.cli", "selftest"], LEDGER)
    assert "SELFTEST PASSED" in r.stdout, r.stdout[-3000:] + r.stderr[-1500:]


def test_dotted_import_layouts():
    """Package-relative imports must resolve under every common repository layout."""
    r = run([sys.executable, str(PY / "control_dotted_imports.py")], PY)
    assert "layouts correct" in r.stdout, r.stdout[-1500:]


def test_constructor_refusals_stay_refusals():
    """Constructor planning must keep refusing what it cannot honestly build."""
    r = run([sys.executable, str(PY / "control_constructors.py")], PY)
    assert "correct" in r.stdout, r.stdout[-1500:]


@pytest.mark.parametrize("name", ["adjudicate", "sandbox", "methods", "fixtures", "precommit",
                                  "install_hook", "blast", "cli", "mcp_server", "pr_report"])
def test_every_module_imports(name):
    """Cheap guard against a syntax error or a bad import reaching a release.

    Worth having because most of this codebase is only exercised through subprocesses, where an
    import error surfaces as an abstention with a reason rather than as a crash, which is
    exactly the shape of failure that hides.
    """
    r = run([sys.executable, "-c", f"import sys; sys.path.insert(0, r'{PY}'); import {name}"], PY)
    assert r.returncode == 0, r.stderr[-1500:]


def test_package_entry_point_resolves():
    """`runboth.engine_dir()` is the seam the git hook depends on. If it moves, hooks break."""
    r = run([sys.executable, "-c",
             "import sys; sys.path.insert(0, r'%s'); import __init__ as p; "
             "print(p.engine_dir())" % PY], PY)
    assert r.returncode == 0, r.stderr[-800:]
    assert (Path(r.stdout.strip()) / "precommit.py").exists(), r.stdout
