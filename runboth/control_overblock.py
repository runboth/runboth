"""CONTROL: prove that over-blocking in the sandbox produces a FALSE `no_change`.

    python runboth/control_overblock.py        # expects: defect -> no_change, fixed -> changed

Every other sandbox control asks whether the sandbox stops something. This asks whether it
stops TOO MUCH, and it is the dangerous direction, because over-blocking never announces
itself. The module fails to import, or the function raises the SAME way in both versions,
the keys compare equal, and the verdict is `no_change` over a measurement that never reached
the code under test.

Found 2026-09-12, twice in one day, both in this project's own hardening:

  * `subprocess.Popen` was replaced with a FUNCTION, so any module subclassing it failed to
    import. On Windows `asyncio.windows_utils` does exactly that, hypothesis imports asyncio,
    and 309 attrs plus 242 requests functions abstained blaming the repository.
  * the write guard was installed before `tempfile.gettempdir()` had ever run, and gettempdir
    probes candidate directories by CREATING a file in each, so it raised
    `FileNotFoundError: No usable temporary directory found` in any module that called it at
    import time. 15 click test modules.

The two functions below differ for real: one returns `len(root) + n`, the other adds one. With
the sandbox over-blocking, both raise inside `gettempdir()` before reaching the difference,
compare equal, and come back `no_change`. That verdict is a lie and nothing about it looks
wrong. The three `MUST still work` cases in `sandbox.run_controls` catch the CAUSE on every
selftest; this script demonstrates the CONSEQUENCE, which is the part worth remembering.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

BEFORE = ("import tempfile\n\n\n"
          "def f(n):\n"
          "    root = tempfile.gettempdir()\n"
          "    return len(root) + n\n")
AFTER = ("import tempfile\n\n\n"
         "def f(n):\n"
         "    root = tempfile.gettempdir()\n"
         "    return len(root) + n + 1\n")


def _verdict():
    import importlib
    import sandbox
    importlib.reload(sandbox)
    v = sandbox.compare_in_sandbox(BEFORE, AFTER, "f", [("n", None)], budget=3,
                                   limits={"cpu_seconds": 25, "wall_seconds": 60})
    return v["verdict"], v.get("witness")


def main():
    os.environ["RUNBOTH_CONTROL_NO_TEMPDIR_PRERESOLVE"] = "1"
    broken, _ = _verdict()
    os.environ.pop("RUNBOTH_CONTROL_NO_TEMPDIR_PRERESOLVE", None)
    fixed, witness = _verdict()

    ok = broken == "no_change" and fixed == "changed" and witness is not None
    print(f"  over-blocking sandbox: {broken:<12} (must be no_change: the FALSE SAME)")
    print(f"  fixed sandbox:         {fixed:<12} (must be changed, with a witness)")
    if witness:
        print(f"     witness at {', '.join(witness['args'])}: "
              f"{witness['before']} -> {witness['after']}")
    print(f"\n  {'PASS' if ok else 'FAIL'}: over-blocking is demonstrably a false-same source")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
