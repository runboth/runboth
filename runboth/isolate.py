"""
THE PRODUCT LAYER, MEASURED AT SCALE. The thing every other number says to build, and the thing
this project had barely measured.

# Why this is the gap that mattered most

Every measurement so far was about the PROOF layer: mutation kill rates for inferred invariants,
fragment reach, solver cost. All of it says the same thing, that differential execution is the
product and the proof rungs are an upgrade. And the differential-execution layer had exactly one
demonstration: five implementations of `dedupe`.

So the strategy rested on the least-measured component in the repository. This file fixes that.

# The number that can invalidate the strategy

Not the detection rate. The FALSE-SAME RATE.

Differential execution can only ever find differences; it cannot prove absence. When it reports
SAME it is saying "I looked and did not find one", and everything downstream treats that as a
green light for an AI edit. So the question that decides whether this is a product is:

    when two functions genuinely differ, how often does sampling say they are the same?

A high false-same rate means the behaviour gate silently waves through real regressions, which is
worse than having no gate, because a gate that is trusted and wrong is worse than an absent one.

# Method

Real self-contained functions harvested from installed packages. For each, source-level mutants
of the ordinary bug shapes. Ground truth from a LARGE budget; detection measured at CI-sized
budgets. Same oracle-versus-detector separation as the loop experiments, for the same reason: the
ground truth is established by running the programs, so a detector given the oracle's budget
scores 100% by construction and the number means nothing.

# The safety gate on executing harvested code

Only functions whose free names are entirely within a small builtin whitelist are accepted. That
makes them self-contained, which the experiment needs anyway, and it means nothing with an import,
an attribute access, or a call to anything outside the whitelist is ever executed.
"""

import ast
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Everything a harvested function is allowed to reach. Anything else and it is not executed.
SAFE_BUILTINS = {
    "len", "range", "abs", "min", "max", "sum", "sorted", "reversed", "enumerate", "zip",
    "int", "float", "str", "bool", "list", "tuple", "dict", "set", "frozenset", "round",
    "divmod", "pow", "all", "any", "map", "filter", "isinstance", "ord", "chr", "repr",
    "True", "False", "None",
    # EXCEPTION TYPES. Their absence made `harvestable` reject every function that raises a
    # named exception, which is an enormous fraction of real code, and the adjudicator abstained
    # on all of it with a reason that pointed at "free names outside builtins" without saying
    # which. Found by a known-answer control expecting `changed` on an exception-type change.
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError", "AttributeError",
    "ZeroDivisionError", "OverflowError", "StopIteration", "RuntimeError", "NotImplementedError",
    "ArithmeticError", "LookupError", "AssertionError", "UnicodeDecodeError", "OSError",
}


def _free_names(fn_node):
    """Names a function reads that it does not bind. Parameters and locals do not count."""
    bound = {a.arg for a in fn_node.args.args}
    bound |= {a.arg for a in fn_node.args.posonlyargs + fn_node.args.kwonlyargs}
    for n in ast.walk(fn_node):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                bound |= {x.id for x in ast.walk(t) if isinstance(x, ast.Name)}
        elif isinstance(n, (ast.AugAssign, ast.For)):
            tgt = n.target
            bound |= {x.id for x in ast.walk(tgt) if isinstance(x, ast.Name)}
        elif isinstance(n, (ast.comprehension,)):
            bound |= {x.id for x in ast.walk(n.target) if isinstance(x, ast.Name)}
        elif isinstance(n, ast.FunctionDef) and n is not fn_node:
            bound.add(n.name)
    free = set()
    for n in ast.walk(fn_node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound:
            free.add(n.id)
    return free


def harvestable(fn_node, extra_names=()):
    """Is this function self-contained, annotated enough to generate inputs for, and safe to run?"""
    a = fn_node.args
    if a.vararg or a.kwarg or a.kwonlyargs or a.defaults or a.posonlyargs:
        return False
    if not (1 <= len(a.args) <= 3):
        return False
    for n in ast.walk(fn_node):
        # `Yield` is deliberately NOT in this list. It was, and that banned every generator in
        # the corpus while the exit check below happily accepted them, so the two halves of this
        # function disagreed and generators abstained with a reason that named neither. A
        # generator is ordinary, callable, comparable code once its output is materialised.
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal,
                          ast.Attribute, ast.Await, ast.Lambda)):
            return False
    # `extra_names` is for a CALLER whose dependencies get supplied at build time. Without it
    # a caller is rejected BECAUSE it has dependencies, since the callee is a free name
    # outside the builtin whitelist. That made finding call triples impossible by
    # construction: the harvester returned zero from every installed package on the machine.
    if not _free_names(fn_node) <= (SAFE_BUILTINS | set(extra_names)):
        return False
    # Needs at least one value-producing exit, or every mutant is trivially identical.
    # A GENERATOR yields and does not return, and requiring `return` silently excluded every
    # generator in the corpus. Two known-answer controls, one equivalent pair and one genuinely
    # different pair, both came back `abstained` and exposed it.
    return any((isinstance(n, ast.Return) and n.value is not None)
               or isinstance(n, (ast.Yield, ast.YieldFrom))
               for n in ast.walk(fn_node))


class _Mut(ast.NodeTransformer):
    def __init__(self, kind, target):
        self.kind, self.target, self.seen, self.done = kind, target, 0, False

    def _hit(self):
        self.seen += 1
        return self.seen - 1 == self.target

    def visit_Constant(self, node):
        if self.kind == "const" and isinstance(node.value, int) and not isinstance(node.value, bool):
            if self._hit():
                self.done = True
                return ast.Constant(value=node.value + 1)
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if self.kind == "arith" and isinstance(node.op, (ast.Add, ast.Sub)) and self._hit():
            self.done = True
            node.op = ast.Sub() if isinstance(node.op, ast.Add) else ast.Add()
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        flip = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
                ast.Eq: ast.NotEq, ast.NotEq: ast.Eq}
        if self.kind == "cmp" and type(node.ops[0]) in flip and self._hit():
            self.done = True
            node.ops = [flip[type(node.ops[0])]()]
        return node


def mutants_of(fn_node, limit=6):
    out = []
    for kind in ("const", "arith", "cmp"):
        for target in range(3):
            m = _Mut(kind, target)
            new = m.visit(copy.deepcopy(fn_node))
            if not m.done:
                continue
            try:
                src = ast.unparse(new)
            except Exception:
                continue
            out.append((f"{kind}{target}", src))
            if len(out) >= limit:
                return out
    return out


def build(src, name):
    """Compile a harvested function in an isolated namespace. Returns the callable or None."""
    import builtins as _b
    allowed = {k: getattr(_b, k) for k in SAFE_BUILTINS if hasattr(_b, k)}
    ns = {"__builtins__": allowed}
    try:
        exec(compile(src, "<h>", "exec"), ns)  # noqa: S102
    except Exception:
        return None
    return ns.get(name)


# The repo-scale measurement harness that used to sit here scanned hard-coded local clone paths via realworld.ROOTS, so it stays
# in the working tree rather than the release. The library half of this module is
# what the shipped tool actually uses.
