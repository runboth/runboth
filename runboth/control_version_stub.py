"""Controls for the generated-`_version.py` stub in materialise().

The stub exists because setuptools_scm and friends write `pkg/_version.py` at BUILD time
and gitignore it, so `git archive` never carries it, the package import raises
ModuleNotFoundError, and every function in it abstains for a reason that has nothing to do
with its behaviour. Measured 2026-09-14: humanize 20 of 23 functions abstained on exactly
that, and urllib3 could not be imported at all.

A stub that fires too eagerly is worse than no stub, because it changes behaviour that was
never broken. dateutil guards the import and falls back to 'unknown'; stubbing it silently
turned that into '0.0.0'. So HALF of these controls must come back False.

Run: python runboth/control_version_stub.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adjudicate import _needs_version_stub, _stub_generated_version  # noqa: E402

CASES = [
    # (name, source, must_stub)
    ("bare from-import must stub",
     "from ._version import version as __version__\n", True),
    ("bare plain import must stub",
     "import _version\n__version__ = _version.version\n", True),
    ("dotted package import must stub",
     "from mypkg._version import version\n", True),
    # These must NOT stub: the module already works without the file.
    ("guarded by ImportError must NOT stub",
     "try:\n    from ._version import version as __version__\n"
     "except ImportError:\n    __version__ = 'unknown'\n", False),
    ("guarded by Exception must NOT stub",
     "try:\n    from ._version import version\nexcept Exception:\n    version = '0'\n", False),
    ("bare except must NOT stub",
     "try:\n    from ._version import version\nexcept:\n    version = '0'\n", False),
    ("no mention at all must NOT stub",
     "def f(x):\n    return x + 1\n", False),
    ("mentions the word in a string only must NOT stub",
     "MSG = 'see _version for details'\n", False),
    ("syntax error must NOT stub",
     "def f(:\n", False),
]


def run_controls():
    ok = True
    print("  VERSION-STUB CONTROLS: half of these MUST come back False\n")
    for name, src, want in CASES:
        got = _needs_version_stub(src)
        good = got == want
        ok &= good
        print("  %-4s %-46s expected %-5s got %-5s"
              % ("PASS" if good else "FAIL", name, want, got))

    # End to end: a real tree, stubbed once and not the second time.
    d = tempfile.mkdtemp(prefix="runboth_stubctl_")
    pkg = os.path.join(d, "mypkg")
    os.makedirs(pkg)
    with open(os.path.join(pkg, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write("from ._version import version as __version__\n")
    _stub_generated_version(d)
    made = os.path.exists(os.path.join(pkg, "_version.py"))
    print("  %-4s %-46s expected %-5s got %-5s"
          % ("PASS" if made else "FAIL", "unguarded package gets a _version.py", True, made))
    ok &= made

    d2 = tempfile.mkdtemp(prefix="runboth_stubctl_")
    pkg2 = os.path.join(d2, "safepkg")
    os.makedirs(pkg2)
    with open(os.path.join(pkg2, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write("try:\n    from ._version import version as __version__\n"
                 "except ImportError:\n    __version__ = 'unknown'\n")
    _stub_generated_version(d2)
    made2 = os.path.exists(os.path.join(pkg2, "_version.py"))
    print("  %-4s %-46s expected %-5s got %-5s"
          % ("PASS" if not made2 else "FAIL", "guarded package is left alone", False, made2))
    ok &= not made2

    # The escape hatch must actually disable it.
    d3 = tempfile.mkdtemp(prefix="runboth_stubctl_")
    pkg3 = os.path.join(d3, "offpkg")
    os.makedirs(pkg3)
    with open(os.path.join(pkg3, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write("from ._version import version as __version__\n")
    os.environ["RUNBOTH_NO_VERSION_STUB"] = "1"
    try:
        _stub_generated_version(d3)
    finally:
        os.environ.pop("RUNBOTH_NO_VERSION_STUB", None)
    made3 = os.path.exists(os.path.join(pkg3, "_version.py"))
    print("  %-4s %-46s expected %-5s got %-5s"
          % ("PASS" if not made3 else "FAIL", "RUNBOTH_NO_VERSION_STUB disables it", False, made3))
    ok &= not made3

    print("\n  VERSION-STUB CONTROLS: %s" % ("PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    sys.exit(0 if run_controls() else 1)
