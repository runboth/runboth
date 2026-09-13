"""
METHODS AND CLASSES: the majority of real code, and the last structural gap in v1.

A free function is called. A method needs an OBJECT first, and that object has to be built the
same way on both sides or the comparison is meaningless. So a method comparison is really two
comparisons stacked:

    construct   C(*ctor_args)     identically in before and after
    call        obj.m(*args)      identically in before and after

# Three things that make this harder than it looks, each handled explicitly

**The instance carries state.** A method may mutate `self`, so the second call on the same object
sees different state. Every call therefore gets a FRESH instance, constructed from the same
arguments. Without that, a perfectly deterministic method looks nondeterministic, which is the
same trap the input-mutation tier of the determinism gate already found once.

**`__init__` may be unsatisfiable.** If the constructor needs a database handle, no generated
argument will do. That is an ABSTENTION with the constructor signature in the reason, never a
guess and never a silent skip.

**Constructing is itself behaviour.** If `C(...)` raises in one version and not the other, that IS
a behaviour change and is reported as one, with the constructor arguments as the witness. It is not
an abstention: the object failing to exist is an observable difference.

# What is deliberately out of scope for v1, and said so

Inheritance from classes outside the module, metaclasses, `__slots__` interacting with the state
reset, and properties with side effects on access. Each is a real thing in real code and each
would be guessed at rather than handled, so they abstain.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sandbox import run_module_fn  # noqa: E402


def split_qualified(name):
    """`Class.method` -> ('Class', 'method'); a bare name -> (None, name)."""
    if "." in name:
        cls, meth = name.rsplit(".", 1)
        return cls, meth
    return None, name


def ctor_params(module_src, class_name):
    """Parameters of `__init__` that must be SUPPLIED, excluding self. None if not constructible.

    Three things this used to get wrong, each costing real coverage on real repositories:

    ONLY TOP-LEVEL CLASSES WERE FOUND. `for node in tree.body` misses a class nested inside
    another class or defined inside a function, and the abstention then blamed the signature
    ("the class was not found") for what was actually a search that never looked.

    `*args`/`**kwargs` WAS A FLAT REFUSAL. `def __init__(self, app, *args, **kwargs)` is
    constructible as `Cls(app)`: the variadics accept nothing and that is a legal call. Refusing
    the whole class because it CAN take more arguments gave up on flask's AppContext and
    FlaskGroup, which are ordinary objects with one required parameter.

    PARAMETERS WITH DEFAULTS WERE GENERATED ANYWAY. An optional parameter already has a value its
    author chose as sensible, and substituting a generated one only lowers the odds the object
    constructs at all. Omitting them costs some exploration of those parameters; failing to
    construct costs ALL exploration, so the trade is not close.
    """
    try:
        tree = ast.parse(module_src)
    except (SyntaxError, ValueError):
        return None
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            target = node
            break
    if target is None:
        return None
    for item in target.body:
        if isinstance(item, ast.FunctionDef) and item.name == "__init__":
            a = item.args
            # A keyword-only parameter with no default cannot be supplied positionally, and the
            # sandbox boundary is positional. That one is still a real refusal.
            if any(d is None for d in a.kw_defaults):
                return None
            pos = list(a.posonlyargs) + list(a.args)
            pos = pos[1:] if pos and pos[0].arg in ("self", "cls") else pos
            required = pos[:len(pos) - len(a.defaults)] if a.defaults else pos
            return [(p.arg, None) for p in required]
    return []          # no explicit __init__: try it with no arguments


def _why_no_ctor(module_src, class_name):
    """The specific reason `ctor_params` refused, so the abstention points at its own fix."""
    try:
        tree = ast.parse(module_src)
    except (SyntaxError, ValueError):
        return f"the module defining {class_name} does not parse"
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    if node is None:
        return f"no class named {class_name} is defined in this module"
    init = next((i for i in node.body
                 if isinstance(i, ast.FunctionDef) and i.name == "__init__"), None)
    if init is None:
        return f"{class_name} has no __init__ here and none was reachable"
    missing = [k.arg for k, d in zip(init.args.kwonlyargs, init.args.kw_defaults) if d is None]
    if missing:
        return (f"{class_name}.__init__ requires keyword-only argument"
                f"{'s' if len(missing) > 1 else ''} {', '.join(missing)}, "
                f"which the positional sandbox boundary cannot supply")
    return f"{class_name}.__init__ could not be reduced to a positional signature"


def method_params(module_src, class_name, method_name):
    """Parameters of a method, excluding self. None if it cannot be called positionally.

    Same three fixes as `ctor_params` (find nested classes, tolerate variadics, refuse only on a
    required keyword-only parameter) with ONE DELIBERATE DIFFERENCE: parameters that have defaults
    are KEPT here.

    For a constructor an optional parameter is pure risk, because a generated value only lowers
    the odds the object builds at all. For a method the parameters ARE the input space being
    compared, so dropping the optional ones throws away exactly the exploration this exists to do.
    Constructing is a means; calling is the measurement.
    """
    try:
        tree = ast.parse(module_src)
    except (SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and item.name == method_name:
                    a = item.args
                    if any(d is None for d in a.kw_defaults):
                        return None
                    pos = list(a.posonlyargs) + list(a.args)
                    pos = pos[1:] if pos and pos[0].arg in ("self", "cls") else pos
                    return [(p.arg, None) for p in pos]
            return None
    return None


def compare_method(before_src, after_src, class_name, method_name, budget=200,
                   limits=None, before_root=None, after_root=None, rel_path=None,
                   repo_root=None, _via_subclass=False, _method_params=None):
    """Compare one METHOD across two versions of its module. Same contract as everywhere else."""
    from engine import make_inputs, mine_from_source

    entry = f"{class_name}.{method_name}"
    rec = {"function": entry, "rung": f"sampled({budget})", "budget": budget,
           "witness": None, "reason": None}

    cp = ctor_params(after_src, class_name)
    if cp is None:
        # NAME THE ACTUAL CAUSE. "takes *args/**kwargs or the class was not found" was wrong in
        # three ways at once: it fired for a required KEYWORD-ONLY parameter, it named variadics
        # that are now handled fine, and it folded "I could not find this" together with "I found
        # it and cannot satisfy it". Those are different problems with different fixes, and an
        # abstention that names the wrong one sends the next session after the wrong mole. Every
        # defect found in this project was found by reading a reason string.
        return {**rec, "verdict": "abstained", "reason": _why_no_ctor(after_src, class_name)}
    mp = method_params(after_src, class_name, method_name)
    if mp is None and _method_params is not None:
        # THE SUBCLASS INHERITS THE METHOD AND DOES NOT REDEFINE IT, which is the whole reason the
        # retry exists. `Flask` never mentions `select_jinja_autoescape`; it gets it from `App` in
        # a different module. Looking for the signature in the subclass's own source finds nothing
        # and the retry abstained before it could run. The signature comes from where the method
        # is actually DEFINED, and is passed in.
        mp = _method_params
    if mp is None:
        return {**rec, "verdict": "abstained",
                "reason": f"{entry} takes *args/**kwargs"}

    mined = mine_from_source(before_src, after_src)

    # MINED CONSTRUCTIONS FIRST. A generator cannot invent a plausible `import_name`, and it
    # should not try: guessing at a constructor produces a green result over an object that never
    # resembled the real thing. The repository already knows how the class is built, because its
    # own tests build it. Those calls are facts, not guesses.
    repo_ctors = []
    if repo_root:
        try:
            from fixtures import mine_for_class
            repo_ctors, _via = mine_for_class(repo_root, class_name)
        except Exception:
            repo_ctors = []
    n = max(budget, 40)
    ctor_inputs = [list(t) for t in make_inputs(cp, n, 1, mined)] if cp else [[]] * n
    meth_inputs = [list(t) for t in make_inputs(mp, n, 2, mined)] if mp else [[]] * n
    pairs = [[c, m] for c, m in zip(ctor_inputs, meth_inputs)]
    # Mined constructions are paired against every generated method argument, and go FIRST so a
    # small budget spends itself on objects that can actually be built.
    # When literals cannot satisfy the constructor, try building the object arguments by name.
    if not repo_ctors:
        try:
            from fixtures import cross_module_plan, object_arg_plan
            # REPO-WIDE FIRST. The same-module planner only ever sees classes in the file under
            # test, which is why `AppContext(app)` abstained: Flask is two files away.
            plan = None
            if repo_root:
                plan = cross_module_plan(repo_root, after_src, class_name)
            if plan is None:
                same = object_arg_plan(after_src, class_name)
                plan = [["__build__", c] if c else None for c in same] if same else None
        except Exception:
            plan = None
        if plan:
            if all(x is not None for x in plan):
                repo_ctors = [list(plan)]
            else:
                # A MIXED SIGNATURE: some parameters are objects the plan can build, the rest are
                # literals the plan refused to guess. Previously any None slot was passed through
                # as the literal None, which is a guess wearing a refusal's clothes. The literal
                # slots are filled from the generated constructor inputs instead, one plan per
                # generated tuple, so an object parameter gets a real object and a string
                # parameter gets the generator's strings, and nothing is invented.
                fill = ctor_inputs[:max(4, budget // 8)] or [[None] * len(plan)]
                repo_ctors = [[p if p is not None else (c[i] if i < len(c) else None)
                               for i, p in enumerate(plan)] for c in fill]

    if repo_ctors:
        mined_pairs = [[list(c), list(m)]
                       for c in repo_ctors for m in meth_inputs[:max(4, budget // 8)]]
        pairs = mined_pairs[:400] + pairs

    # RELATIONAL CTOR/METHOD PAIRS. The same wall the two-parameter case hit, one level up:
    # `self.n + k > 10` is a condition on the SUM of a constructor argument and a method
    # argument, and those two lists are generated independently. Under independent sampling that
    # target is measure-zero, so no budget reaches it and an off-by-one at a named threshold
    # comes back `no_change`. A method control caught exactly that, twice, first with an empty
    # constant miner and then with a full one.
    #
    # So the pairs are constructed: for every integer literal in the source, split it across the
    # constructor and the method so their sum lands on the threshold and on either side of it.
    if len(cp) == 1 and len(mp) == 1:
        rel = []
        for c in mined[0][:40]:
            for a in (0, 1, -1, 2, c // 2 if c else 0):
                for delta in (-1, 0, 1):
                    rel.append([[a], [c - a + delta]])
        pairs = rel[:400] + pairs
    if not pairs:
        return {**rec, "verdict": "abstained", "reason": "no inputs could be generated"}

    import tempfile
    d = Path(tempfile.mkdtemp(prefix="runboth_meth_"))
    pb, pa = d / "before.py", d / "after.py"
    pb.write_text(before_src, encoding="utf-8")
    pa.write_text(after_src, encoding="utf-8")

    # Methods need the package too. This path was missed when the function path was fixed, and a
    # real Flask method abstained with "attempted relative import" while free functions no longer
    # did, which is what pointed at it.
    from sandbox import dotted_name
    dotted = sys_b = sys_a = None
    if rel_path and before_root and after_root:
        dotted, sys_b = dotted_name(before_root, rel_path)
        d2, sys_a = dotted_name(after_root, rel_path)
        if dotted and d2 == dotted:
            tb, ta = Path(before_root) / rel_path, Path(after_root) / rel_path
            if tb.exists() and ta.exists():
                pb, pa = tb, ta
            else:
                dotted = None
        else:
            dotted = None

    # EVERY PACKAGE ROOT, on the method path too. `compare_in_sandbox` already passes
    # `package_roots()` so a test under `tests/` can import a package under `src/`; this path
    # never did, so under a src/ layout every METHOD in a test class imported the INSTALLED
    # package from site-packages instead of the tree. On attrs that was 152 abstentions
    # ("cannot import name 'ne' from attr.validators (...site-packages...)"), each blaming the
    # repository for a version skew the tool created. Found 2026-09-12.
    from sandbox import package_roots
    rb = package_roots(before_root) if before_root else []
    ra = package_roots(after_root) if after_root else []
    # SAME BACKOFF AS THE FUNCTION PATH, and this is where it matters most: a method that
    # constructs an object per trial is the expensive shape, and click's CLI-runner tests are
    # 123 of the 179 wall-clock abstentions measured on 2026-09-12. Fewer constructed calls is
    # weaker evidence, honestly labelled in the rung, and strictly better than no verdict.
    attempts = [pairs]
    if len(pairs) > 8:
        attempts.append(pairs[:max(8, len(pairs) // 8)])
    if len(pairs) > 40:
        attempts.append(pairs[:4])

    kb = ka = None
    for attempt, use in enumerate(attempts):
        kb, eb = run_module_fn(pb, entry, use, limits, sys_b if dotted else None, dotted, rb)
        if eb:
            if "wall clock" in eb and attempt + 1 < len(attempts):
                continue
            return {**rec, "verdict": "abstained", "reason": f"before: {eb}"}
        ka, ea = run_module_fn(pa, entry, use, limits, sys_a if dotted else None, dotted, ra)
        if ea:
            if "wall clock" in ea and attempt + 1 < len(attempts):
                continue
            return {**rec, "verdict": "abstained", "reason": f"after: {ea}"}
        if len(use) != len(pairs):
            rec = {**rec, "rung": f"sampled({len(use)})", "budget": len(use),
                   "slow": f"reduced from {len(pairs)} constructed calls to fit the budget"}
        pairs = use
        break
    if kb is None or ka is None or len(kb) != len(ka):
        return {**rec, "verdict": "abstained", "reason": "sandbox returned mismatched results"}

    # cross-process determinism, same as the function path
    kb2, eb2 = run_module_fn(pb, entry, pairs, limits, sys_b if dotted else None, dotted, rb)
    if eb2 or kb2 != kb:
        return {**rec, "verdict": "abstained",
                "reason": "before version differs from itself across processes"}

    # A constructor that fails in EVERY trial means the class was never built, so nothing was
    # compared. Reporting `no_change` there would be a green light over an empty measurement.
    if all(isinstance(k, list) and k[:1] == ["ctor"] for k in kb):
        # RETRY THROUGH A CONCRETE SUBCLASS BEFORE GIVING UP.
        #
        # Flask's `App` cannot be built at all: `App("test")` raises AttributeError because it is a
        # base class expecting subclass attributes, and no argument fixes that. But `Flask`
        # subclasses it, IS constructible, and INHERITS the method under test. Refusing here would
        # leave every base-class method permanently unadjudicated, which in real object-oriented
        # code is a large fraction of the interesting logic.
        #
        # The method being tested is still the one that changed; only the object it is called on
        # comes from a subclass. That is exactly how the method is reached in production.
        if not _via_subclass and repo_root:
            try:
                from fixtures import subclasses_of
                subs = subclasses_of(repo_root, class_name)[:3]
            except Exception:
                subs = []
            for sub, sub_rel in subs:
                # Use the SUBCLASS'S OWN MODULE. It imports the changed module transitively from
                # the materialised tree, so the modified method is what executes; the object just
                # comes from where it is actually defined.
                try:
                    b_src = (Path(before_root) / sub_rel).read_text(encoding="utf-8")
                    a_src = (Path(after_root) / sub_rel).read_text(encoding="utf-8")
                except OSError:
                    continue
                out = compare_method(b_src, a_src, sub, method_name, budget, limits,
                                     before_root, after_root, sub_rel, repo_root,
                                     _via_subclass=True, _method_params=mp)
                if out["verdict"] != "abstained":
                    out["reason"] = f"{out['reason']} (constructed as {sub}, which inherits it)"
                    out["function"] = entry
                    return out
        return {**rec, "verdict": "abstained",
                "reason": f"{class_name}(...) could not be constructed on any generated input; "
                          f"signature is ({', '.join(p for p, _ in cp)})"}

    # THE MEASUREMENT MUST NOT SHOW UP IN THE ANSWER. Identical reasoning to the function path in
    # `compare_in_sandbox`, and a method is the likelier carrier of the two: an object that records
    # where it was loaded from is an ordinary thing for a class to do. Each side loses only its own
    # roots, and only the prefix, so a genuine change in what is built under the root still reports.
    # `_strip_measurement_paths` adds each root's RESOLVED form itself, so the Windows 8.3
    # short-name case is covered here too without repeating the logic.
    from sandbox import _strip_measurement_paths
    nb = _strip_measurement_paths(kb, [p for p in (before_root, sys_b, pb) if p])
    na = _strip_measurement_paths(ka, [p for p in (after_root, sys_a, pa) if p])

    # Most legible differing call, not the first one. Same reasoning as the function path; see
    # sandbox._witness_rank. A method's witness is two pieces, so it is ranked here rather than
    # through best_witness().
    from sandbox import _witness_rank
    diffs = [(c, m, x, y) for (c, m), x, y in zip(pairs, nb, na) if x != y]
    if diffs:
        c, m, x, y = min(diffs, key=lambda d: _witness_rank(d[2], d[3], (d[0], d[1])))
        return {**rec, "verdict": "changed",
                "witness": {"args": [f"{class_name}({', '.join(map(repr, c))})",
                                     f".{method_name}({', '.join(map(repr, m))})"],
                            "before": str(x), "after": str(y)},
                "reason": "behaviour differs"}
    return {**rec, "verdict": "no_change",
            "reason": f"no difference found in {len(pairs)} constructed calls (sandboxed)"}


# ---------------------------------------------------------------------------------------
# CONTROLS. Known answers, including ones that must be `changed` and ones that must abstain.
# ---------------------------------------------------------------------------------------

_BASE = '''
class Counter:
    def __init__(self, start):
        self.n = start

    def bump(self, k):
        self.n = self.n + k
        return self.n

    def label(self, k):
        return "big" if self.n + k > 10 else "small"


class NeedsAHandle:
    def __init__(self, conn):
        self.rows = conn.fetchall()

    def count(self, k):
        return len(self.rows) + k
'''

_CASES = [
    ("method refactored, same behaviour",
     _BASE.replace("        self.n = self.n + k\n        return self.n",
                   "        total = self.n + k\n        self.n = total\n        return total"),
     "Counter", "bump", "no_change"),
    ("method off-by-one, must be caught",
     _BASE.replace('return "big" if self.n + k > 10 else "small"',
                   'return "big" if self.n + k >= 10 else "small"'),
     "Counter", "label", "changed"),
    ("state mutation is behaviour, fresh instance each call",
     _BASE.replace("        self.n = self.n + k\n        return self.n",
                   "        self.n = self.n + k + 1\n        return self.n"),
     "Counter", "bump", "changed"),
    ("unsatisfiable constructor must abstain",
     _BASE.replace("return len(self.rows) + k", "return len(self.rows) + k + 1"),
     "NeedsAHandle", "count", "abstained"),
]


def run_controls(budget=40):
    print("  METHOD CONTROLS: half must come back `changed`, one must abstain\n")
    ok = True
    for label, after, cls, meth, expected in _CASES:
        got = compare_method(_BASE, after, cls, meth, budget,
                             {"cpu_seconds": 5, "wall_seconds": 20})
        good = got["verdict"] == expected
        ok = ok and good
        print(f"  {'PASS' if good else 'FAIL'}  {label:<48} expected {expected:<10} "
              f"got {got['verdict']}")
        if not good:
            print(f"        {(got.get('reason') or '')[:88]}")
        elif got["witness"]:
            w = got["witness"]
            print(f"        witness {w['args'][0]}{w['args'][1]}: {w['before']} -> {w['after']}")
    print()
    return ok


if __name__ == "__main__":
    sys.exit(0 if run_controls() else 1)
