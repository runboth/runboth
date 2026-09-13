"""
SYSTEM-WIDE BLAST RADIUS: which of the repository's thousands of functions actually behave
differently after one edit, established by RUNNING THEM, not by reading the call graph.

The call graph alone answers a different and much weaker question. It says what COULD be affected,
which on any real codebase is most of the repository, and a tool that highlights 400 functions
after a one-line change has told you nothing you can act on. Static impact analysis has been
available for thirty years and nobody runs it, for exactly this reason.

The question worth answering is which functions DID change, and the only honest way to establish
that is to execute both versions and watch.

# THE PRUNE IS WHAT MAKES IT TRACTABLE, AND IT IS ALSO THE HONEST PART

Breadth-first from the edited functions outward. At each function, compare it across the two
trees. If it CHANGED, its own callers go on the queue, because the change can propagate through
it. If it did NOT change, the walk stops there.

That prune is sound in the direction that matters: if `g` behaves identically at every input
tried, nothing that calls `g` can observe a difference THROUGH g. It inherits the sampling caveat
of everything else here, and it is stated rather than hidden: a difference outside the sample
would prune a branch that should have stayed open. The budget is reported with the result so the
strength of the claim travels with it.

# WHY THE TEST FILES ARE CALLED OUT SEPARATELY

A changed test function is a test that is ABOUT TO FAIL, named before the suite has been run.
That is the most immediately actionable output this produces: not "something may be affected"
but "test_get will fail, on this input, because of the line you just wrote".
"""

import ast
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

TEST_MARKERS = ("test_", "_test", "/tests/", "\\tests\\", "conftest")


def _git(repo, *a):
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def repo_files(repo, limit=4000):
    out = _git(repo, "ls-files", "*.py") or ""
    return [f.strip() for f in out.splitlines() if f.strip()][:limit]


def call_graph(repo, files, rev="HEAD"):
    """(defs, callers) over the whole repo, by NAME.

    Name-based resolution is deliberately loose. Python's real binding is dynamic and resolving it
    exactly would mean writing an import-aware type inferencer, which is a different project. Over
    -inclusion costs a comparison that comes back `no_change` and prunes; under-inclusion silently
    drops a real regression. That asymmetry decides it.
    """
    from adjudicate import functions_in
    defs, callers = {}, {}
    for f in files:
        src = _git(repo, "show", f"{rev}:{f}")
        if src is None:
            continue
        try:
            fns = functions_in(src, f, strict=True)
        except (SyntaxError, ValueError):
            continue
        for q, node in fns.items():
            name = q.split("::", 1)[-1].split(".")[-1]
            defs.setdefault(name, []).append((q, f, node, src))
            for c in ast.walk(node):
                if isinstance(c, ast.Name) and isinstance(c.ctx, ast.Load):
                    callers.setdefault(c.id, set()).add(q)
                elif isinstance(c, ast.Attribute):
                    callers.setdefault(c.attr, set()).add(q)
    return defs, callers


def _witness_values(witness):
    """The concrete argument values from a witness, parsed back out of its printed form.

    The witness crosses a process boundary as text, so the values arrive as strings like "-6" and
    "'abc'". `literal_eval` recovers the ones that are literals and drops the rest, which is the
    right split: a repr that does not round-trip is an object whose identity would not have
    survived the boundary anyway.
    """
    # An A/B switch so the mechanism can be MEASURED rather than assumed. A feature that is only
    # ever run in the on position has no evidence behind it, and this one costs real budget.
    import os
    if not witness or os.environ.get("RUNBOTH_NO_WITNESS_SEED"):
        return []
    out = []
    for a in witness.get("args", []):
        try:
            v = ast.literal_eval(str(a))
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            continue
        if isinstance(v, (int, float, str)) and not isinstance(v, bool):
            out.append(v)
    return out


def blast_radius(repo, edited_names, head_root, work_root, budget=60, deadline=45,
                 max_functions=400, on_step=None, seed_values=None):
    """BFS outward from the edited names, comparing as it goes. Returns (hits, stats)."""
    from adjudicate import adjudicate_pair

    files = repo_files(repo)
    defs, callers = call_graph(repo, files)
    index = {}
    for name, entries in defs.items():
        for q, f, node, src in entries:
            index[q] = (f, node, src, name)

    seen, hits = set(), []
    frontier = deque()
    for n in edited_names:
        for q in callers.get(n, ()):
            if q in index:
                frontier.append((q, 1, tuple(seed_values or ())))

    t0, checked, truncated = time.time(), 0, False
    while frontier:
        if time.time() - t0 > deadline or checked >= max_functions:
            truncated = True
            break
        q, depth, inherited = frontier.popleft()
        if q in seen:
            continue
        seen.add(q)
        f, node, src, name = index[q]
        checked += 1
        if on_step:
            on_step(checked, q)
        # SAME source both sides. Only what is UNDERNEATH it differs, which is the whole question.
        rec = adjudicate_pair(q, node, node, budget, src, src, head_root, work_root,
                              extra=list(inherited) or None)
        if rec["verdict"] != "changed":
            # PRUNE. If this function behaves identically, nothing can observe a difference
            # through it. Sampled, and the budget travels with the claim.
            continue
        hits.append({"function": q, "file": f, "depth": depth,
                     "witness": rec.get("witness"),
                     "is_test": any(m in f for m in TEST_MARKERS) or name.startswith("test")})
        # CARRY THE WITNESS UP. The values this function was proven to differ at are the best
        # constants available for testing whatever calls it, and they compound: a value that
        # survives two hops is one that genuinely propagates through the chain.
        passed = _witness_values(rec.get("witness")) + list(inherited)
        for up in callers.get(name, ()):
            if up not in seen and up in index:
                frontier.append((up, depth + 1, tuple(passed[:12])))

    return hits, {"checked": checked, "reached": len(seen), "functions_in_repo": len(index),
                  "files": len(files), "seconds": round(time.time() - t0, 1),
                  "truncated": truncated, "budget": budget}


def render(hits, stats):
    if not hits:
        return (f"  no downstream function changed behaviour "
                f"({stats['checked']} checked of {stats['functions_in_repo']} in the repo, "
                f"{stats['seconds']}s, budget {stats['budget']})")
    tests = [h for h in hits if h["is_test"]]
    other = [h for h in hits if not h["is_test"]]
    lines = [f"  BLAST RADIUS: {len(hits)} function(s) in "
             f"{len({h['file'] for h in hits})} file(s) behave differently"]
    if tests:
        lines.append(f"\n  {len(tests)} TEST(S) WILL FAIL, named without running the suite:")
        for h in tests[:8]:
            lines.append(f"    {h['function'].split('::', 1)[-1]}   {h['file']}")
    if other:
        lines.append(f"\n  {len(other)} non-test function(s), by distance from the edit:")
        for h in sorted(other, key=lambda x: x["depth"])[:10]:
            w = h.get("witness")
            lines.append(f"    depth {h['depth']}  {h['function'].split('::', 1)[-1]}   {h['file']}")
            if w:
                lines.append(f"              at {', '.join(w['args'])}:  "
                             f"{w['before']} -> {w['after']}")
    lines.append(f"\n  {stats['checked']} function(s) executed in both trees, "
                 f"{stats['functions_in_repo']} in the repo, {stats['seconds']}s, "
                 f"budget {stats['budget']}. Evidence, not proof.")
    if stats["truncated"]:
        lines.append("  WALK TRUNCATED on the deadline: this is a LOWER BOUND, not the full set.")
    return "\n".join(lines)


def blast_from_edit(repo, rel, budget=60, deadline=45):
    """The whole question from one edited file: what moved, and where does it reach.

    The edited functions are adjudicated FIRST, not merely diffed, because their witnesses seed
    the walk. Knowing that `_get` differs at -6, 2, -3 is worth more to every caller above it
    than any number of generated draws, and skipping this step would throw that away at the root.
    """
    from adjudicate import adjudicate_pair, functions_in, materialise
    before_src = _git(repo, "show", f"HEAD:{rel}")
    if before_src is None:
        return None, None, {}, f"{rel} is not in HEAD; nothing to compare against"
    after_src = (Path(repo) / rel).read_text(encoding="utf-8", errors="ignore")
    try:
        b = functions_in(before_src, rel, strict=True)
        a = functions_in(after_src, rel, strict=True)
    except (SyntaxError, ValueError):
        return None, None, {}, "source does not parse yet"
    edited = {q for q in a if q not in b or ast.dump(b[q]) != ast.dump(a[q])}
    if not edited:
        return [], {}, {}, "no function in this file changed"

    head_root = materialise(repo, "HEAD")
    work_root = str(Path(repo).resolve())
    seeds, direct = [], []
    for q in sorted(edited & set(b)):
        rec = adjudicate_pair(q, b[q], a[q], budget, before_src, after_src, head_root, work_root)
        if rec["verdict"] == "changed":
            direct.append(rec)
            seeds.extend(_witness_values(rec.get("witness")))
    names = {q.split("::", 1)[-1] for q in edited}
    hits, stats = blast_radius(repo, names, head_root, work_root, budget, deadline,
                               seed_values=seeds[:12])
    return hits, stats, {"edited": sorted(names), "direct": direct, "seeds": seeds[:12]}, ""


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="runboth blast")
    ap.add_argument("repo")
    ap.add_argument("file", help="the edited file, relative to the repo")
    ap.add_argument("--budget", type=int, default=60)
    ap.add_argument("--deadline", type=float, default=45)
    a = ap.parse_args()
    hits, stats, meta, err = blast_from_edit(a.repo, a.file, a.budget, a.deadline)
    if err:
        print(f"  {err}")
        sys.exit(0)
    print(f"  edited: {', '.join(meta.get('edited', [])) or '(none)'}")
    if meta.get("seeds"):
        print(f"  witness values carried into the walk: {meta['seeds']}")
    print(render(hits, stats))
