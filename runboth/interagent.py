"""
THE DEFECT THAT ONLY EXISTS IN THE MERGE.

    PR #1 (agent A)  changes normalise()         reviewed alone: clean
    PR #2 (agent B)  adds a caller of normalise  reviewed alone: clean
    merge both                                   production breaks

Every review tool reviews a PR against its base. This defect exists in neither PR against its
base. It comes into being at the merge, which is the one state nobody reviews. Agent volume makes
it the dominant pattern: agents open PRs concurrently, never talk, and each reads the codebase as
it was when it started.

# The mechanism, and why it is not a call graph

A call graph says who MIGHT be affected. It cannot say who IS, because that depends on whether the
caller ever reaches the inputs where the callee's behaviour actually changed. So a static answer is
either a flood ("47 functions call normalise") or silence.

DEPENDENCY SUBSTITUTION answers it by running:

    fingerprint g  with f = f_before
    fingerprint g  with f = f_after
    different  ->  the change propagated THROUGH g, with a witness input
    same       ->  no propagation found at budget N

The call graph enumerates candidates. Execution draws the conclusion. That turns "47 might break"
into "3 do, here are the inputs", which is the difference between a report people ignore and one
they act on.

# The combination check

For two independent edits A and B:

    A alone      base + A            adjudicate every caller
    B alone      base + B            adjudicate every caller
    A and B      base + A + B        adjudicate every caller

A caller that is unchanged under A, unchanged under B, and CHANGED under both is the inter-agent
regression. Per-PR review cannot see it by construction, because neither branch is wrong alone.

# The control that makes any of this believable

A suite where every case passes is measuring nothing. The controls below include a constructed
case whose answer is known in advance: two edits that are individually behaviour-preserving and
jointly are not. If the harness cannot produce `changed` on that, nothing else it says counts.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from determinism import DETERMINISTIC, INPUT_MUTATING, classify  # noqa: E402
from isolate import SAFE_BUILTINS  # noqa: E402
from engine import compare  # noqa: E402


def build_with_deps(entry_name, entry_src, dep_srcs):
    """Compile `entry` in a namespace that also contains its dependencies. Returns the callable.

    THE WHOLE MECHANISM IS HERE. Everything else in this file arranges which versions of the
    dependencies go in. `build()` in isolate compiles a function alone with only builtins,
    which is right for measuring one function and useless for measuring propagation: a caller
    needs its callee present, and the point is to vary WHICH callee.
    """
    import builtins as _b
    ns = {"__builtins__": {k: getattr(_b, k) for k in SAFE_BUILTINS if hasattr(_b, k)}}
    try:
        for src in dep_srcs:
            exec(compile(src, "<dep>", "exec"), ns)  # noqa: S102
        exec(compile(entry_src, "<entry>", "exec"), ns)  # noqa: S102
    except Exception:
        return None
    return ns.get(entry_name)


def propagates(caller_name, caller_src, deps_before, deps_after, budget=400):
    """Does a change in the dependencies change the CALLER's behaviour? Returns a verdict dict."""
    rec = {"function": caller_name, "rung": f"sampled({budget})", "budget": budget,
           "witness": None, "reason": None}
    g_before = build_with_deps(caller_name, caller_src, deps_before)
    g_after = build_with_deps(caller_name, caller_src, deps_after)
    if g_before is None or g_after is None:
        return {**rec, "verdict": "abstained", "reason": "caller not constructible with its deps"}
    for fn, which in ((g_before, "before"), (g_after, "after")):
        try:
            v, why = classify(fn, trials=12, repeats=3)
        except Exception as e:
            return {**rec, "verdict": "abstained", "reason": f"gate raised {type(e).__name__}"}
        if v not in (DETERMINISTIC, INPUT_MUTATING):
            return {**rec, "verdict": "abstained", "reason": f"{which} is {v}: {why}"}
    try:
        res = compare(g_before, g_after, trials=budget)
    except Exception as e:
        return {**rec, "verdict": "abstained", "reason": f"compare raised {type(e).__name__}"}
    if getattr(res, "abstained", False):
        return {**rec, "verdict": "abstained", "reason": res.note}
    if res.same:
        return {**rec, "verdict": "no_change",
                "reason": f"no propagation found in {res.trials} inputs"}
    d = res.diffs[0] if res.diffs else None
    w = {"args": [repr(x) for x in d.args], "before": d.a.show(), "after": d.b.show()} if d else None
    return {**rec, "verdict": "changed", "witness": w, "reason": "the change propagated"}


def combination_check(callers, base_deps, edit_a, edit_b, budget=400):
    """Per caller: verdict under A alone, B alone, and A+B together.

    `base_deps` maps a dependency name to its source. `edit_a` and `edit_b` are partial maps
    replacing some of them. A caller clean under each edit and changed under both is the
    inter-agent regression.
    """
    def merged(*edits):
        out = dict(base_deps)
        for e in edits:
            out.update(e)
        return list(out.values())

    base = merged()
    rows = []
    for name, src in callers.items():
        a = propagates(name, src, base, merged(edit_a), budget)
        b = propagates(name, src, base, merged(edit_b), budget)
        ab = propagates(name, src, base, merged(edit_a, edit_b), budget)
        rows.append({"caller": name, "a": a, "b": b, "ab": ab,
                     "interagent": (a["verdict"] == "no_change"
                                    and b["verdict"] == "no_change"
                                    and ab["verdict"] == "changed")})
    return rows


# ---------------------------------------------------------------------------------------
# CONTROLS. Known answers, including one that MUST come back as an inter-agent regression
# and one that must NOT, so the harness cannot pass by reporting everything or nothing.
# ---------------------------------------------------------------------------------------

# The base tree. A FEATURE FLAG, which is the most common real shape of this defect: a new code
# path exists but is switched off, so it is dead and nobody reviewing it can see its effect.
BASE_DEPS = {
    "enabled": "def enabled(n):\n    return 0",       # agent A owns the flag
    "newpath": "def newpath(n):\n    return n * 2",   # agent B owns the new path
}

CALLERS = {
    # Uses the new path only when the flag is on. Today the flag is off AND the new path agrees
    # with the old one, so the feature is doubly masked.
    "pipeline": "def pipeline(n):\n    return newpath(n) if enabled(n) else n * 2",
    # Calls the new path unconditionally, so agent B's edit IS visible here alone. Present so the
    # harness has to tell a visible change from a hidden one.
    "direct": "def direct(n):\n    return newpath(n)",
    # Depends on neither. The null control.
    "independent": "def independent(n):\n    return n + 1",
}

# AGENT A: turn the flag on. Alone this changes NOTHING, because newpath currently agrees with the
# old path. A reviewer sees a flag flip with no behavioural effect, and they are right.
EDIT_A = {"enabled": "def enabled(n):\n    return 1"}

# AGENT B: change the new path. Alone this changes NOTHING through `pipeline`, because the flag is
# off and the branch is dead. A reviewer sees an edit to unreachable code, and they are right too.
EDIT_B = {"newpath": "def newpath(n):\n    return n * 3"}

# Visible alone, so the harness cannot label everything inter-agent.
EDIT_A_VISIBLE = {"newpath": "def newpath(n):\n    return n * 2 + 1"}


def run_controls(budget=200):
    print("  CONTROLS: the harness must find the inter-agent case and must NOT invent one.\n")
    ok = True

    # 1. a change visible alone must NOT be labelled inter-agent
    rows = combination_check(CALLERS, BASE_DEPS, EDIT_A_VISIBLE, {}, budget)
    r = next(x for x in rows if x["caller"] == "direct")
    good = r["a"]["verdict"] == "changed" and not r["interagent"]
    ok = ok and good
    print(f"  {'PASS' if good else 'FAIL'}  visible-alone change     A={r['a']['verdict']:<10} "
          f"interagent={r['interagent']} (must be False)")

    # 2. the null control: a caller depending on neither must stay clean everywhere
    r = next(x for x in rows if x["caller"] == "independent")
    good = all(r[k]["verdict"] == "no_change" for k in ("a", "b", "ab"))
    ok = ok and good
    print(f"  {'PASS' if good else 'FAIL'}  independent caller       "
          f"A={r['a']['verdict']}, B={r['b']['verdict']}, A+B={r['ab']['verdict']} "
          f"(all must be no_change)")

    # 3. THE ONE THAT MATTERS: an inter-agent regression the harness must FIND.
    rows = combination_check(CALLERS, BASE_DEPS, EDIT_A, EDIT_B, budget)
    r = next(x for x in rows if x["caller"] == "pipeline")
    good = r["interagent"]
    ok = ok and good
    print(f"  {'PASS' if good else 'FAIL'}  hidden inter-agent case  A={r['a']['verdict']:<10} "
          f"B={r['b']['verdict']:<10} A+B={r['ab']['verdict']:<10} (must be no/no/changed)")
    if r["ab"]["witness"]:
        w = r["ab"]["witness"]
        print(f"        witness at {', '.join(w['args'])}: {w['before']} -> {w['after']}")
    print()
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=300)
    args = ap.parse_args()
    print("  INTER-AGENT REGRESSION: the defect that only exists in the merge\n")
    if not run_controls(args.budget):
        print("  CONTROLS FAILED. Nothing this harness reports is worth reading.")
        sys.exit(1)

    print("  DEMONSTRATION: two independent edits, each clean alone\n")
    rows = combination_check(CALLERS, BASE_DEPS, EDIT_A, EDIT_B, args.budget)
    print(f"  {'caller':<14} {'A alone':<12} {'B alone':<12} {'A + B':<12} inter-agent")
    print(f"  {'-'*14} {'-'*12} {'-'*12} {'-'*12} -----------")
    for r in rows:
        print(f"  {r['caller']:<14} {r['a']['verdict']:<12} {r['b']['verdict']:<12} "
              f"{r['ab']['verdict']:<12} {'YES' if r['interagent'] else ''}")
    hits = [r for r in rows if r["interagent"]]
    print(f"\n  inter-agent regressions found: {len(hits)}")
    for r in hits:
        w = r["ab"]["witness"]
        print(f"    {r['caller']}: clean under each edit, CHANGED under both")
        if w:
            print(f"      at {', '.join(w['args'])}:  {w['before']}  ->  {w['after']}")
    print("\n  Both edits pass review on their own. The defect exists only in the merged tree,")
    print("  which is the state no review tool examines.")


if __name__ == "__main__":
    main()
