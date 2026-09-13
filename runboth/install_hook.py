"""
MAKE IT UNFORGETTABLE: install runboth as a git hook, so no agent can skip it.

The demo where Mistral Vibe called this check over MCP and refused a bad edit is real, and it has
one weakness that matters more than anything else in this repository:

    THE AGENT ONLY CALLED IT BECAUSE IT WAS TOLD TO.

A tool an agent may call is a tool an agent will forget. Worse, the agents most likely to forget
are the ones moving fastest, which is exactly the population this exists for. Politeness is not a
control.

# The enforcement point is git, not the agent

A `pre-commit` hook fires on every commit, from every agent, from every harness, from a human
typing `git commit`, whether or not anyone remembers runboth exists. It needs no MCP, no plugin API,
no vendor cooperation, and it works identically for Claude Code, Vibe, Cursor, Aider and a person.

That is the difference between a capability and a gate.

# What the hook does, and what it deliberately does not

It adjudicates the STAGED changes. If behaviour changed, it prints the witness and exits non-zero,
which stops the commit. The developer or agent then either fixes it or passes `--no-verify`,
having been shown exactly what changed and on what input.

It does NOT block on abstentions. A function that could not be checked is not evidence of a
problem, and a gate that fires on "I could not tell" gets removed within a day. Abstentions are
printed and the commit proceeds.

It does NOT block when nothing changed. Silence is the normal case and it must stay silent, or it
will be disabled by whoever is annoyed by it, which is the same disease as a flaky control.

# On making it Rust

Worth doing, and it is an OPTIMISATION rather than the mechanism. The gate is the hook; Rust makes
the gate cheap enough that nobody wants to remove it. The current Python path costs ~1.5s for 113
functions, which is already inside the tolerance of a commit, and a native binary would take it to
the point where it is unnoticeable even on a large staged diff.
"""

import argparse
import stat
import subprocess
import sys
from pathlib import Path

def _render(py_dir):
    """The hook text with both paths baked in, POSIX-slashed so bash does not eat backslashes.

    `C:\\runboth\\py` inside a double-quoted bash string is a live escape sequence, which is the
    second thing that would have broken this hook on Windows. Git Bash accepts forward slashes
    everywhere, so both paths are normalised on the way in.
    """
    return (HOOK
            .replace("__RUNBOTH_PY__", str(py_dir).replace("\\", "/"))
            .replace("__RUNBOTH_PYBIN__", sys.executable.replace("\\", "/")))


HOOK = '''#!/usr/bin/env bash
# runboth: behaviour gate. Installed by `runboth install-hook`.
#
# Blocks a commit whose STAGED changes alter observable behaviour, and prints the input that
# proves it. Does not block on abstentions or on no change. Bypass with `git commit --no-verify`,
# which is deliberate: a gate with no override gets deleted instead of overridden.
set -uo pipefail

# THE ENGINE IS FOUND AT RUN TIME, NOT BAKED IN AT INSTALL TIME.
#
# This used to be a single hardcoded absolute path, and renaming the project folder on
# 2026-09-12 silently broke every hook already installed: each one still pointed at a directory
# that no longer existed. A behaviour gate that stops working without saying so is precisely
# the failure this tool exists to catch, so it should not ship it.
#
# Resolution order, first hit wins:
#   1. RUNBOTH_ENGINE, if someone wants to pin it
#   2. the INSTALLED `runboth` package, which survives the checkout moving anywhere
#   3. the path recorded at install time, which covers a plain checkout with no pip install
# If none resolve, it says so loudly and fails CLOSED rather than waving the commit through.
RUNBOTH_PY="${RUNBOTH_ENGINE:-}"
BUDGET="${RUNBOTH_BUDGET:-80}"

[ -n "${RUNBOTH_SKIP:-}" ] && exit 0

# FIND AN INTERPRETER INSTEAD OF ASSUMING `python3`. On Windows (Git Bash, which is where every
# git hook runs there) `python3` does not exist, so the hook died with "command not found" on
# EVERY commit. It exited 127, so it failed CLOSED and blocked the commit, which is the safe
# direction and still useless: the developer sees an interpreter error instead of a verdict,
# and this project's own rule is that a gate which cries wolf gets uninstalled within a day.
# The installer bakes in the interpreter that installed it, and the fallbacks cover a venv
# activated later. Found 2026-09-12 on the first live test of the hook on Windows.
RUNBOTH_PYBIN="${RUNBOTH_PYTHON:-__RUNBOTH_PYBIN__}"
if ! command -v "$RUNBOTH_PYBIN" >/dev/null 2>&1 && [ ! -x "$RUNBOTH_PYBIN" ]; then
  for c in python3 python py; do
    if command -v "$c" >/dev/null 2>&1; then RUNBOTH_PYBIN="$c"; break; fi
  done
fi
if ! command -v "$RUNBOTH_PYBIN" >/dev/null 2>&1 && [ ! -x "$RUNBOTH_PYBIN" ]; then
  echo "runboth: no Python interpreter found; behaviour gate did NOT run." >&2
  echo "runboth: set RUNBOTH_PYTHON=/path/to/python, or uninstall with 'runboth install-hook --uninstall'." >&2
  exit 1
fi

# Ask the interpreter where the installed package lives. Costs one interpreter start (~50ms)
# and buys immunity to the project ever moving again.
if [ -z "$RUNBOTH_PY" ]; then
  RUNBOTH_PY="$("$RUNBOTH_PYBIN" -c 'import runboth,sys; sys.stdout.write(runboth.engine_dir())' 2>/dev/null)"
fi
if [ -z "$RUNBOTH_PY" ] || [ ! -f "$RUNBOTH_PY/precommit.py" ]; then
  RUNBOTH_PY="__RUNBOTH_PY__"                      # recorded at install time, the last resort
fi
if [ ! -f "$RUNBOTH_PY/precommit.py" ]; then
  echo "runboth: cannot find the engine; the behaviour gate did NOT run." >&2
  echo "runboth: looked for an installed 'runboth' package and at $RUNBOTH_PY" >&2
  echo "runboth: fix with 'pip install runboth', or set RUNBOTH_ENGINE=/path/to/py," >&2
  echo "runboth: or remove this gate with 'runboth install-hook --uninstall'." >&2
  exit 1
fi

# $1 is the commit message file. `commit-msg` receives it; `pre-commit` does not, and
# .git/COMMIT_EDITMSG still holds the PREVIOUS message at pre-commit time (verified
# 2026-09-12), which is why this gate lives on commit-msg. The message is what lets a
# declared change through without an argument.
"$RUNBOTH_PYBIN" "$RUNBOTH_PY/precommit.py" --repo "$(git rev-parse --show-toplevel)" \
    --budget "$BUDGET" ${1:+--message-file "$1"}
exit $?
'''


def install(repo, py_dir):
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-dir"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, f"{repo} is not a git repository"
    gitdir = Path(repo) / r.stdout.strip() if not Path(r.stdout.strip()).is_absolute() \
        else Path(r.stdout.strip())
    hooks = gitdir / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    # THE GATE LIVES ON `commit-msg`, NOT `pre-commit`, since 2026-09-12. Both can abort a
    # commit, but only commit-msg is handed the message, and the message is what tells the gate
    # a change was INTENTIONAL. Blocking work someone did on purpose is the fastest way to get
    # uninstalled, so reconciling against the message is worth the move.
    #
    # Any runboth pre-commit hook from before that change is removed here, otherwise a repo ends
    # up gated twice and the pre-commit copy blocks declared changes the commit-msg copy would
    # have waved through. Someone else's pre-commit hook is left strictly alone.
    old = hooks / "pre-commit"
    if old.exists() and "runboth: behaviour gate" in old.read_text(encoding="utf-8",
                                                                errors="ignore"):
        old.unlink()

    target = hooks / "commit-msg"
    if target.exists():
        existing = target.read_text(encoding="utf-8", errors="ignore")
        if "runboth: behaviour gate" not in existing:
            # NEVER clobber someone's existing hook. A tool that silently replaces a commit-msg
            # hook has destroyed a team's lint or secret-scan step, and will be uninstalled with
            # prejudice. Chain instead, and say so.
            backup = hooks / "commit-msg.before-runboth"
            backup.write_text(existing, encoding="utf-8")
            body = _render(py_dir)
            body = body.replace('set -uo pipefail\n',
                                'set -uo pipefail\n\n'
                                '# chained: the previous hook runs first and its failure still blocks\n'
                                f'"{backup}" "$@" || exit $?\n')
            target.write_text(body, encoding="utf-8")
            target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            return target, f"existing hook preserved at {backup.name} and chained"
    target.write_text(_render(py_dir), encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return target, ""


def main():
    ap = argparse.ArgumentParser(prog="runboth install-hook")
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()
    py_dir = Path(__file__).resolve().parent

    if args.uninstall:
        r = subprocess.run(["git", "-C", args.repo, "rev-parse", "--git-dir"],
                           capture_output=True, text=True)
        # Both names, because the gate moved from pre-commit to commit-msg on 2026-09-12 and an
        # uninstall that only knew the new name would leave the old one silently gating.
        hooks_dir = Path(args.repo) / r.stdout.strip() / "hooks"
        removed = []
        for name in ("commit-msg", "pre-commit"):
            h = hooks_dir / name
            if h.exists() and "runboth" in h.read_text(encoding="utf-8", errors="ignore"):
                h.unlink()
                removed.append(str(h))
                restore = hooks_dir / f"{name}.before-runboth"
                if restore.exists():
                    h.write_text(restore.read_text(encoding="utf-8"), encoding="utf-8")
                    h.chmod(h.stat().st_mode | stat.S_IEXEC)
                    restore.unlink()
                    print(f"  restored your original {name}")
        print("\n".join(f"  removed {p}" for p in removed) or "  no runboth hook installed")
        return 0

    target, note = install(args.repo, py_dir)
    if target is None:
        print(f"  {note}")
        return 1
    print(f"  installed {target}")
    if note:
        print(f"  {note}")
    print("\n  Every commit in this repository is now adjudicated, by any agent or human,")
    print("  whether or not they know runboth exists. It blocks only on a behaviour CHANGE,")
    print("  prints the witness input, and never blocks on an abstention or on silence.")
    print("\n  Bypass: git commit --no-verify   |   Disable once: RUNBOTH_SKIP=1 git commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
