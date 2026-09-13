"""
THE POST-EDIT HOOK: what an agent harness calls after every edit, and what it prints.

    runboth hook <repo>

Diffs the working tree against HEAD, adjudicates every changed function, and prints ONE NOTE:
what changed, the input that proves it, and which callers downstream change too. Exit code 1 when
behaviour changed, so a harness can gate on it.

This is the actionable form. Every other command in this tree is something a person runs
deliberately; this is the thing that runs a hundred times a day inside Mistral Vibe, Claude Code,
Aider or Cline, and it has to earn its place in three lines of terminal output.

# The design constraints that follow from being a hook

**Silence when nothing changed.** A hook that prints on every edit gets muted within a day. It
says nothing at all when behaviour is unchanged, and the exit code carries that.

**The downstream note is the whole point.** "You changed `_get`" is something the agent already
knows; it just wrote the diff. "You changed `_get`, and `pluck` and `get` now behave differently
at this input" is information it does not have and cannot get from a diff. That is the line worth
interrupting for.

**Never says "safe".** It says what it checked and at what budget. A hook that prints SAFE trains
people to trust it, and the measured false-same rate is 12% at 20 inputs and 4% at 400.

**Fast or nothing.** An agent loop cannot wait 60 seconds. The budget defaults low and the caller
scan is bounded; a deeper check is a deliberate `runboth adjudicate` run.
"""

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from adjudicate import adjudicate_pair, functions_in, git, materialise  # noqa: E402
from sandbox import compare_in_sandbox  # noqa: E402


def working_tree_diff(repo):
    """Files changed in the working tree against HEAD, staged or not."""
    out = git(repo, "diff", "HEAD", "--name-only", "--", "*.py")
    return [l.strip() for l in (out or "").splitlines() if l.strip().endswith(".py")]


def callers_of(repo, root, name, limit=40):
    """Functions anywhere in the tree that call `name`. The call graph ENUMERATES; execution decides."""
    hits = []
    for f in sorted(Path(root).rglob("*.py")):
        try:
            src = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if name not in src:
            continue
        try:
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            continue
        for n in tree.body:
            if not isinstance(n, ast.FunctionDef) or n.name == name:
                continue
            for c in ast.walk(n):
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == name:
                    hits.append((str(f), n.name, src,
                                 [(a.arg, None) for a in n.args.args]))
                    break
        if len(hits) >= limit:
            break
    return hits


def run(repo, budget=80, downstream=True):
    files = working_tree_diff(repo)
    if not files:
        return 0, []

    head_root = materialise(repo, "HEAD")
    notes = []
    for path in files:
        before_src = git(repo, "show", f"HEAD:{path}")
        try:
            after_src = (Path(repo) / path).read_text(encoding="utf-8")
        except OSError:
            continue
        if before_src is None:
            continue
        before = functions_in(before_src, path)
        after = functions_in(after_src, path)
        for q in sorted(set(before) & set(after)):
            rec = adjudicate_pair(q, before[q], after[q], budget,
                                  before_src, after_src, head_root, str(repo))
            if rec["verdict"] != "changed":
                continue
            name = q.split("::", 1)[-1].split(".")[-1]
            entry = {"function": q, "name": name, "witness": rec.get("witness"),
                     "reason": rec.get("reason"), "downstream": []}
            if downstream:
                for cf, cname, csrc, cparams in callers_of(repo, repo, name):
                    v = compare_in_sandbox(
                        # the caller's own source is unchanged; what differs is the module around
                        # it, so the two module versions ARE the before/after of its dependency
                        git(repo, "show", f"HEAD:{Path(cf).relative_to(repo)}") or csrc,
                        csrc, cname, cparams, max(budget // 2, 30),
                        {"cpu_seconds": 5, "wall_seconds": 15},
                        before_root=head_root, after_root=str(repo))
                    if v["verdict"] == "changed":
                        entry["downstream"].append((Path(cf).name, cname, v.get("witness")))
            notes.append(entry)
    return (1 if notes else 0), notes


def report(notes, budget):
    if not notes:
        return
    n = len(notes)
    print(f"\n  runboth: {n} behaviour change{'s' if n != 1 else ''} in this edit\n")
    for e in notes:
        w = e["witness"]
        print(f"    {e['name']}")
        if w:
            print(f"      {e['name']}({', '.join(w['args'])})   "
                  f"{w['before']}  ->  {w['after']}")
        else:
            print(f"      {e['reason']}")
        if e["downstream"]:
            names = ", ".join(f"{c}" for _f, c, _w in e["downstream"])
            print(f"      downstream: {names} also change")
            for _f, c, dw in e["downstream"][:2]:
                if dw:
                    print(f"        {c}({', '.join(dw['args'])})   "
                          f"{dw['before']}  ->  {dw['after']}")
        print()
    print(f"  checked at budget {budget}. This is `sampled({budget})`, not a proof:")
    print("  it finds differences and can never prove their absence.\n")


def main():
    ap = argparse.ArgumentParser(prog="runboth hook")
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--budget", type=int, default=80)
    ap.add_argument("--no-downstream", action="store_true")
    ap.add_argument("--quiet-exit", action="store_true",
                    help="always exit 0; print the note but do not gate")
    args = ap.parse_args()
    code, notes = run(args.repo, args.budget, not args.no_downstream)
    report(notes, args.budget)
    sys.exit(0 if args.quiet_exit else code)


if __name__ == "__main__":
    main()
