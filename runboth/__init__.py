"""runboth: behaviour adjudication for AI-written code.

This directory is a flat set of modules that import one another by bare name. When it is
installed as the `runboth` package, this file puts the package directory itself on sys.path so
those imports resolve to the installed copies. It is the same thing `cli.py` already does when
run from a checkout, made unconditional so `from runboth.cli import main` works too.
"""
import sys as _sys
from pathlib import Path as _Path

_here = str(_Path(__file__).resolve().parent)
if _here not in _sys.path:
    _sys.path.insert(0, _here)

__all__ = ["main", "engine_dir"]


def engine_dir():
    """Where the engine modules live, wherever this package was installed or checked out.

    The git hook uses this to locate `precommit.py` at RUN time instead of having an absolute
    path baked into it at INSTALL time. Moving the project used to silently break every hook
    already installed, which is exactly the class of failure this tool exists to catch.
    """
    return _here


def main():
    from cli import main as _main
    return _main()
