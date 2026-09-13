"""
THE MERGE CHECK, ON REAL GIT BRANCHES. The capability nobody has, wired to a repository.

`interagent.py` proved the mechanism on in-memory sources. This runs it on actual branches:

    runboth merge-check <repo> <base> <branch-a> <branch-b>

and answers the question no review tool asks, because review is defined per-PR:

    is there a function that is unchanged by A, unchanged by B, and CHANGED by both?

# How the merged tree is obtained without touching the working directory

`git merge-tree --write-tree` computes the merge of two branches and writes the resulting tree
into the object database, without a checkout, without an index, and without disturbing anything
the user has open. If the merge conflicts, that is reported as a conflict and not as a behaviour
result, because a tree that does not exist cannot be adjudicated.

That matters for production: a tool that checks out branches to do its job will eventually do it
in someone's dirty working tree, and losing a developer's uncommitted work would end the trial on
the spot.

# The four states

    base            the common ancestor
    base + A        branch A merged in
    base + B        branch B merged in
    base + A + B    both

A function whose behaviour is identical in the first three and different in the fourth is the
inter-agent regression. Both PRs pass review; the merge is wrong.
"""

import argparse
import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from adjudicate import git, materialise  # noqa: E402
from sandbox import compare_in_sandbox  # noqa: E402
from determinism import DETERMINISTIC, INPUT_MUTATING, classify  # noqa: E402
from interagent import build_with_deps  # noqa: E402
from engine import compare  # noqa: E402


def merge_tree(repo, a, b):
    """The tree of merging b into a, without a checkout. Returns (tree_sha, conflicted)."""
    out = git(repo, "merge-tree", "--write-tree", a, b)
    if out is None:
        # git older than 2.38 has no --write-tree; say so instead of guessing
        return None, "git merge-tree --write-tree unavailable (needs git >= 2.38)"
    lines = out.strip().splitlines()
    if not lines:
        return None, "merge-tree produced nothing"
    tree = lines[0].strip()
    conflicted = len(lines) > 1 and any(l.strip() for l in lines[1:])
    return tree, ("merge conflicts; a tree that does not exist cannot be adjudicated"
                  if conflicted else "")


def file_at_tree(repo, tree, path):
    return git(repo, "show", f"{tree}:{path}")


def module_functions(src):
    """Every top-level function in a module source, by name. Methods are out of scope for v1."""
    out = {}
    try:
        tree = ast.parse(src or "")
    except (SyntaxError, ValueError):
        return out
    for n in tree.body:
        if isinstance(n, ast.FunctionDef):
            try:
                out[n.name] = ast.unparse(n)
            except Exception:
                continue
    return out


def _verdict_sandboxed(name, mod_before, mod_after, root_before, root_after, budget):
    """Compare one function across two whole TREES, with its real imports.

    THE MERGE PATH NEVER GOT THE SANDBOX. adjudicate.py did, and this file kept the
    builtins-only builder, so on a real library every function touching `operator` or
    `itertools` abstained. Four functions adjudicated out of a real toolz diff, all
    abstentions, which a fixture would never have shown: the fixture had no imports.
    """
    params = []
    try:
        import ast as _ast
        for n in _ast.parse(mod_after).body:
            if isinstance(n, _ast.FunctionDef) and n.name == name:
                params = [(a.arg, None) for a in n.args.args]
                break
    except (SyntaxError, ValueError):
        pass
    return compare_in_sandbox(mod_before, mod_after, name, params, budget,
                              before_root=root_before, after_root=root_after)


def _verdict(name, src, deps_before, deps_after, budget):
    rec = {"function": name, "rung": f"sampled({budget})", "budget": budget,
           "witness": None, "reason": None}
    gb = build_with_deps(name, src, deps_before)
    ga = build_with_deps(name, src, deps_after)
    if gb is None or ga is None:
        return {**rec, "verdict": "abstained", "reason": "not constructible with its module"}
    for fn, which in ((gb, "before"), (ga, "after")):
        try:
            v, why = classify(fn, trials=10, repeats=3)
        except Exception as e:
            return {**rec, "verdict": "abstained", "reason": f"gate raised {type(e).__name__}"}
        if v not in (DETERMINISTIC, INPUT_MUTATING):
            return {**rec, "verdict": "abstained", "reason": f"{which} is {v}: {why[:60]}"}
    try:
        res = compare(gb, ga, trials=budget)
    except Exception as e:
        return {**rec, "verdict": "abstained", "reason": f"compare raised {type(e).__name__}"}
    if getattr(res, "abstained", False):
        return {**rec, "verdict": "abstained", "reason": res.note}
    if res.same:
        return {**rec, "verdict": "no_change",
                "reason": f"no difference found in {res.trials} inputs"}
    d = res.diffs[0] if res.diffs else None
    w = ({"args": [repr(x) for x in d.args], "before": d.a.show(), "after": d.b.show()}
         if d else None)
    return {**rec, "verdict": "changed", "witness": w, "reason": "behaviour differs"}


def merge_check(repo, base, branch_a, branch_b, budget=300):
    """Every function whose behaviour appears only in the merge. Returns (rows, notes)."""
    notes = []
    tree_a, err_a = merge_tree(repo, base, branch_a)
    tree_b, err_b = merge_tree(repo, base, branch_b)
    tree_ab, err_ab = (None, "")
    if tree_a:
        tree_ab, err_ab = merge_tree(repo, tree_a, branch_b) if False else merge_tree(
            repo, branch_a, branch_b)
    for e in (err_a, err_b, err_ab):
        if e:
            notes.append(e)
    if not (tree_a and tree_b and tree_ab):
        return [], notes or ["could not compute all three merged trees"]

    files = set()
    for rev in (branch_a, branch_b):
        out = git(repo, "diff", "--name-only", f"{base}..{rev}", "--", "*.py")
        files |= {l.strip() for l in (out or "").splitlines() if l.strip().endswith(".py")}

    # Materialise all four trees once. Each is a real directory with the package intact, so a
    # module's own imports resolve exactly as they would in a checkout.
    roots = {"base": materialise(repo, base), "a": materialise(repo, tree_a),
             "b": materialise(repo, tree_b), "ab": materialise(repo, tree_ab)}
    if any(v is None for v in roots.values()):
        return [], notes + ["could not materialise one of the four trees"]

    rows = []
    for path in sorted(files):
        srcs = {k: file_at_tree(repo, {"base": base, "a": tree_a, "b": tree_b,
                                       "ab": tree_ab}[k], path) for k in roots}
        if any(v is None for v in srcs.values()):
            continue
        mods = {k: module_functions(v) for k, v in srcs.items()}
        common = set(mods["base"]) & set(mods["a"]) & set(mods["b"]) & set(mods["ab"])
        for name in sorted(common):
            a = _verdict_sandboxed(name, srcs["base"], srcs["a"],
                                   roots["base"], roots["a"], budget)
            b = _verdict_sandboxed(name, srcs["base"], srcs["b"],
                                   roots["base"], roots["b"], budget)
            ab = _verdict_sandboxed(name, srcs["base"], srcs["ab"],
                                    roots["base"], roots["ab"], budget)
            rows.append({"file": path, "function": name, "a": a, "b": b, "ab": ab,
                         "interagent": (a["verdict"] == "no_change"
                                        and b["verdict"] == "no_change"
                                        and ab["verdict"] == "changed")})
    return rows, notes


def report(rows, notes, as_json=False):
    if as_json:
        print(json.dumps({"rows": rows, "notes": notes}, indent=2))
        return
    for n in notes:
        print(f"  note: {n}")
    if not rows:
        print("  no comparable functions in the changed files")
        return
    print(f"\n  {'file:function':<40} {'A':<11} {'B':<11} {'A+B':<11} inter-agent")
    print(f"  {'-'*40} {'-'*11} {'-'*11} {'-'*11} -----------")
    for r in rows:
        tag = "YES" if r["interagent"] else ""
        label = f"{Path(r['file']).name}:{r['function']}"
        print(f"  {label[:40]:<40} {r['a']['verdict']:<11} {r['b']['verdict']:<11} "
              f"{r['ab']['verdict']:<11} {tag}")
    hits = [r for r in rows if r["interagent"]]
    print(f"\n  INTER-AGENT REGRESSIONS: {len(hits)}")
    for r in hits:
        w = r["ab"]["witness"]
        print(f"    {r['file']}:{r['function']} is clean under each branch and CHANGED in the merge")
        if w:
            print(f"      at {', '.join(w['args'])}:  {w['before']}  ->  {w['after']}")
    if hits:
        print("\n  Both branches pass review on their own. The defect exists only in the merged")
        print("  tree, which is the one state no review tool examines.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("base")
    ap.add_argument("branch_a")
    ap.add_argument("branch_b")
    ap.add_argument("--budget", type=int, default=300)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows, notes = merge_check(args.repo, args.base, args.branch_a, args.branch_b, args.budget)
    report(rows, notes, args.json)


if __name__ == "__main__":
    main()
