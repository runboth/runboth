"""
THE RUNTIME HOOK: adjudication that arrives while the agent is still holding the edit.

The git gate in `install_hook.py` is correct and it is LATE. It fires at commit, which is minutes
and often dozens of edits after the mistake, at the moment a human is trying to finish. By then
the agent has moved on and the context that would let it fix the problem cheaply is gone.

Every serious agent harness fires a hook on the file-write tool itself:

    Claude Code   PostToolUse on Edit|Write         settings.json
    Vibe          same shape, MCP or hook config
    Cursor        afterEdit
    any other     wrap the editor

That is the real runtime interception. No agent has to remember anything, and the result lands in
the model's own context automatically, seconds after the edit, while it can still act on it.

# THE SEMANTICS ARE NOT THE COMMIT GATE'S, AND GETTING THIS WRONG MAKES IT USELESS

At commit time a behaviour change is suspicious. At EDIT time a behaviour change is usually THE
ENTIRE POINT: the agent was asked to change behaviour and it did. A hook that fires "behaviour
changed!" after every intentional edit is pure noise and will be turned off in an hour.

So this reports something the agent genuinely does not know. The agent knows what it MEANT to
change. It does not know what ELSE moved:

    COLLATERAL   functions in the file it did not touch, whose behaviour changed anyway
    BLAST RADIUS callers elsewhere in the repo that now behave differently

That second one is the whole value. An agent edits `_get`, and `get_in` and `pluck` change with
it, in a file it never opened, and nothing in its context will ever tell it so. RunBoth runs both
versions and can say exactly which callers moved and on what input.

# IT REPORTS. IT DOES NOT BLOCK.

Blocking belongs at the commit, where a human is present and the change is complete. Blocking an
agent mid-edit on an intentional change teaches it to route around the hook, and an agent that
routes around your gate is worse than no gate, because now you believe you have one.

# EVERY WAY THIS COULD BECOME ANNOYING IS HANDLED BY BEING SILENT

Not a Python file, not in git, no HEAD, no changed function, unparseable mid-edit source, nothing
downstream: all silent, exit 0, no output. A syntax error in particular is the COMMON case, since
an agent writing a file in two passes has a broken tree in between, and a hook that scolds it for
that will be removed the same day.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

BUDGET = int(os.environ.get("RUNBOTH_EDIT_BUDGET", "40"))
DEADLINE = float(os.environ.get("RUNBOTH_EDIT_DEADLINE", "12"))   # seconds; a hook must not stall
MAX_CALLERS = 25


def _git(repo, *a):
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _repo_of(path):
    r = subprocess.run(["git", "-C", str(Path(path).parent), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def analyse(file_path, budget=BUDGET, deadline=DEADLINE):
    """Return (collateral, blast, note) for one just-edited file, or (None, None, reason)."""

    p = Path(file_path)
    if p.suffix != ".py" or not p.exists():
        return None, None, "not a python file"
    repo = _repo_of(p)
    if not repo:
        return None, None, "not in a git repository"
    try:
        rel = str(p.resolve().relative_to(Path(repo).resolve())).replace("\\", "/")
    except ValueError:
        return None, None, "outside the repository"

    before_src = _git(repo, "show", f"HEAD:{rel}")
    if before_src is None:
        return None, None, "new file, nothing to compare against"
    after_src = p.read_text(encoding="utf-8", errors="ignore")
    if before_src == after_src:
        return None, None, "identical to HEAD"

    # ONE ENTRY POINT for what is one question. `blast_from_edit` adjudicates the edited functions
    # first (their witnesses seed the walk) and then walks outward, which is exactly what this was
    # doing by hand in three stages. Two copies of a traversal drift, and the copy that drifts is
    # the one nobody is looking at.
    from blast import blast_from_edit
    hits, stats, meta, err = blast_from_edit(repo, rel, budget, deadline)
    if err:
        return None, None, err
    direct_names = {d["function"].split("::", 1)[-1] for d in meta.get("direct", [])}
    edited_names = set(meta.get("edited", []))

    # COLLATERAL is what moved in this file WITHOUT being touched. The agent knows what it edited;
    # this is the part of its own file it has no reason to look at.
    collateral = [{"function": h["function"], "witness": h.get("witness")}
                  for h in (hits or [])
                  if h["file"] == rel and h["function"].split("::", 1)[-1] not in edited_names]
    blast = [{"caller": h["function"], "file": h["file"], "witness": h.get("witness"),
              "depth": h["depth"], "is_test": h["is_test"]}
             for h in (hits or []) if h["file"] != rel]
    from blast import render as _brender
    return collateral, blast, _brender(hits or [], stats or {}) if stats else \
        f"{len(direct_names)} edited function(s) changed behaviour"


def render(collateral, blast, note):
    """The message the model reads. Short, specific, and it never says 'safe'."""
    if not collateral and not blast:
        return None
    lines = []
    if collateral:
        lines.append("runboth: functions you did NOT edit changed behaviour in this file:")
        for c in collateral[:6]:
            w = c.get("witness")
            n = c["function"].split("::", 1)[-1]
            lines.append(f"  {n}" + (f"   at {', '.join(w['args'])}:  {w['before']} -> {w['after']}"
                                     if w else ""))
    if blast:
        tests = [b for b in blast if b.get("is_test")]
        if tests:
            # THE MOST ACTIONABLE LINE THIS PRODUCES. Not "something may be affected" but "this
            # test is about to fail", named before the suite has been run.
            lines.append(f"runboth: {len(tests)} test(s) will fail, named without running the suite:")
            for b in tests[:6]:
                lines.append(f"  {b['caller'].split('::', 1)[-1]}  ({b['file']})")
        rest = [b for b in blast if not b.get("is_test")]
        if rest:
            lines.append(f"runboth: {len(rest)} function(s) elsewhere in the repo now behave "
                         f"differently because of this edit:")
            for b in sorted(rest, key=lambda x: x.get("depth", 9))[:6]:
                w = b.get("witness")
                lines.append(f"  {b['caller'].split('::', 1)[-1]}  ({b['file']})"
                             + (f"   at {', '.join(w['args'])}:  {w['before']} -> {w['after']}"
                                if w else ""))
    lines.append(f"Differential execution at budget {BUDGET}. Evidence, not proof. "
                 "If this blast radius is intended, carry on.")
    return "\n".join(lines)


def main():
    """Claude Code PostToolUse contract: JSON on stdin, optional additionalContext on stdout."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return 0
    fp = (payload.get("tool_input") or {}).get("file_path")
    if not fp:
        return 0
    try:
        collateral, blast, note = analyse(fp)
    except Exception:
        # A hook that raises poisons the agent's turn. Any failure here is silence.
        return 0
    msg = render(collateral or [], blast or [], note)
    if msg:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext": msg}}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "--stdin":
        c, b, n = analyse(sys.argv[1])
        print(f"note: {n}")
        print(render(c or [], b or [], n) or "  (nothing to report)")
        sys.exit(0)
    sys.exit(main())
