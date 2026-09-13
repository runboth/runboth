"""
Differential execution, which works on everything.

# The architecture correction this file represents

This engine began as an exact-equivalence checker over a small language where
equivalence is decidable. That forced a
restricted fragment, and a survey of ~19,000 real functions said only 4.3% of them fit it. A tool
that only speaks when it can be exact is silent on 96% of real code, which makes it a niche
instrument no matter how nice the proofs are.

The inversion: **the weakest rung is the universal one.** Running two versions on the same inputs
and comparing needs no decidable fragment, no type restrictions, no IVL and no extraction. It
works on any Python function that can be called. Proof becomes an opportunistic UPGRADE for the
minority of code that admits it, instead of the price of admission.

So the ladder is unchanged and its order of use is reversed:

    sampled     universal, always available, evidence not proof   <- the base
    exact       for the fragment that allows it                   <- the upgrade

# What this gives an AI coding workflow, today, on real code

* a model produced N candidates: which are actually DIFFERENT? (`classes`)
* a model edited a function: did behaviour change, and on which input? (`same`)
* two agents edited in parallel: do they disagree by MEANING? (`merge`)

None of that needs the code to be provable. It needs the code to be runnable.

# The honesty that has to survive the reframe

Sampling can only ever find differences, never prove their absence. Every "same" result from this
file is `sampled(N)` and says so. It is EVIDENCE. Reporting it as proof would be the same failure
the rung ladder was built to prevent, and a universal tool that lies about its confidence is
worse than a niche one that does not.
"""

import argparse
import importlib.util
import inspect
import itertools
import json
import math
import random
import re
import sys
import typing
from dataclasses import dataclass, field


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------------------
# Input generation. Boundary-heavy on purpose: real behavioural differences cluster at zero,
# one, empty, negative and the edges, not in the middle of a uniform range.
# ---------------------------------------------------------------------------------------

INT_POOL = [0, 1, -1, 2, -2, 3, 10, -10, 100, -100, 7, 1000, -1000, 2**31, -(2**31)]
FLOAT_POOL = [0.0, 1.0, -1.0, 0.5, -0.5, 2.0, 1e-9, 1e9, -1e9, 3.14159]
STR_POOL = ["", "a", "abc", " ", "0", "-1", "Hello, World", "  padded  ", "\n", "aA1!"]


def mine_constants(*fns):
    """Pull the literals out of the functions themselves, and add their neighbours.

    FOUND BY THE COLLAPSE STUDY, and it was a FALSE SAME, which is the dangerous direction.
    Two `grade` implementations differing only in `>=` against `>` disagree at exactly 70, 80
    and 90. None of those were in the generic input pool, so 400 trials found nothing and the
    tool reported SAME for two genuinely different functions. A false SAME can inherit an
    approval and launder unreviewed code.

    Generic boundaries (0, 1, -1) are not enough, because real disagreements sit on DOMAIN
    boundaries the generator cannot guess. But the code names them: `90` is right there in the
    source. So mine the constants from the code objects, and probe each one at `k-1, k, k+1`
    since off-by-one at a named threshold is the single most common real difference.

    `co_consts` is used instead of the source text because it works for any callable, including
    ones built by `exec` where `inspect.getsource` fails.
    """
    ints, floats, strs = set(), set(), set()

    def walk(code, depth=0):
        if depth > 6:
            return
        for c in getattr(code, "co_consts", ()) or ():
            if isinstance(c, bool):
                continue
            if isinstance(c, int):
                ints.update({c - 1, c, c + 1})
            elif isinstance(c, float):
                floats.update({c, c - 1.0, c + 1.0})
            elif isinstance(c, str):
                strs.add(c)
            elif hasattr(c, "co_consts"):
                walk(c, depth + 1)

    for fn in fns:
        code = getattr(fn, "__code__", None)
        if code is not None:
            walk(code)
    # keep it bounded: a function full of literals should not explode the input space
    return sorted(ints)[:40], sorted(floats)[:20], sorted(strs)[:20]



def mine_from_source(*srcs):
    """Literals from SOURCE TEXT, for paths where no function object exists.

    `mine_constants` reads `co_consts` off a compiled function, which is right when the callable
    is in hand. On the sandbox path it is not: the function lives in another process, and the
    parent only has the module source. Calling mine_constants() with nothing then returns nothing,
    the generator never probes the domain boundaries, and a method differing only at `> 10` versus
    `>= 10` comes back `no_change`. A method control caught exactly that.

    Same rule as the code-object version: every integer is probed at k-1, k, k+1, because an
    off-by-one at a named threshold is the most common real difference there is.
    """
    import ast as _ast
    ints, floats, strs = set(), set(), set()
    for src in srcs:
        try:
            tree = _ast.parse(src or "")
        except (SyntaxError, ValueError):
            continue
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Constant):
                continue
            v = node.value
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                ints.update({v - 1, v, v + 1})
            elif isinstance(v, float):
                floats.update({v, v - 1.0, v + 1.0})
            elif isinstance(v, str) and len(v) <= 40:
                strs.add(v)
    return sorted(ints)[:60], sorted(floats)[:20], sorted(strs)[:20]


def gen_value(ann, rng, depth=0, extra_ints=(), extra_strs=(), extra_floats=()):
    """One value for a parameter, guided by its annotation when there is one."""
    origin = typing.get_origin(ann)
    args = typing.get_args(ann)

    if origin in (list, typing.List):
        inner = args[0] if args else int
        n = rng.choice([0, 1, 2, 3, 5])
        return [gen_value(inner, rng, depth + 1) for _ in range(n)]
    if origin in (dict, typing.Dict):
        k = args[0] if args else str
        v = args[1] if len(args) > 1 else int
        n = rng.choice([0, 1, 2])
        return {gen_value(k, rng, depth + 1): gen_value(v, rng, depth + 1) for _ in range(n)}
    if origin in (tuple, typing.Tuple) and args:
        return tuple(gen_value(a, rng, depth + 1) for a in args if a is not Ellipsis)
    if origin is typing.Union:
        return gen_value(rng.choice([a for a in args]), rng, depth + 1)

    if ann is int:
        return rng.choice(INT_POOL + list(extra_ints))
    if ann is float:
        return rng.choice(FLOAT_POOL + list(extra_floats))
    if ann is str:
        return rng.choice(STR_POOL + list(extra_strs))
    if ann is bool:
        return rng.choice([True, False])
    if ann is type(None):
        return None

    # UNANNOTATED. This is the common case in real Python and the honest thing is to admit the
    # generator is guessing: try a spread of shapes and report low confidence upward.
    return rng.choice(
        [
            rng.choice(INT_POOL),
            rng.choice(FLOAT_POOL),
            rng.choice(STR_POOL),
            rng.choice([True, False]),
            [],
            [1, 2, 3],
            None,
        ]
    )


def signature_of(fn):
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None, False
    params = []
    annotated = True
    for name, p in sig.parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        ann = p.annotation if p.annotation is not inspect.Parameter.empty else None
        if ann is None:
            annotated = False
        params.append((name, ann))
    return params, annotated


# `<... object ... at 0x7f...>`: the default repr, with the address in it.
_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")


@dataclass
class Outcome:
    """What a call produced. An EXCEPTION is a behaviour, not a failure to record."""
    ok: bool
    value: object = None
    exc: str = ""
    _key: object = None

    def key(self):
        # MEMOISED, BECAUSE MATERIALISING A LAZY VALUE CONSUMES IT.
        #
        # key() drains a generator to compare it by contents. Callers legitimately call key() more
        # than once on the same Outcome, and the second call then saw an EXHAUSTED generator and
        # returned a different answer. The determinism gate reported that a generator "differs
        # from itself", which was true and was caused entirely by the act of measuring it.
        #
        # A measurement that changes what it measures is the worst possible bug in this project,
        # because it is indistinguishable from a real finding. Compute once, keep the answer.
        if self._key is None:
            self._key = self._compute_key()
        return self._key

    def _compute_key(self):
        if not self.ok:
            return ("exc", self.exc)
        v = self.value
        if isinstance(v, float):
            # NaN never equals itself, so compare it structurally or every run looks different
            if math.isnan(v):
                return ("float", "nan")
            return ("float", repr(v))
        # LAZY RETURNS MUST BE MATERIALISED, and this was found by the determinism gate rather
        # than by any comparison test. `repr` of a generator or iterator embeds its ADDRESS:
        #
        #     <generator object merge at 0xADDR>      (schematic, not captured)
        #
        # so two calls returning equivalent generators produce different keys. The determinism
        # sweep flagged heapq.merge, codecs.iterdecode, difflib.restore, itertools-style code and
        # ipaddress.collapse_addresses as "nondeterministic". None of them are. They return a
        # fresh object each call.
        #
        # The consequence in `compare` was worse than a misclassification: both sides return
        # generators, the reprs differ, and EVERY generator-returning function reports DIFFERENT
        # on its first input whether or not anything changed. That is a false-different flood over
        # a large and ordinary class of Python.
        #
        # Consuming is safe because the value was just produced by our own call and nothing else
        # holds it. The cap keeps an infinite generator from hanging the comparison, and hitting
        # the cap is recorded in the key so a truncated comparison is never confused with a
        # complete one.
        if hasattr(v, "__next__") and not isinstance(v, (str, bytes)):
            items, cap = [], 512
            try:
                for i, item in enumerate(v):
                    if i >= cap:
                        items.append("<truncated>")
                        break
                    items.append(Outcome(True, item).key())
            except Exception as e:
                items.append(("exc", type(e).__name__))
            return ("lazy", tuple(items))
        try:
            r = repr(v)
        except Exception:
            return ("val", "<unreprable>")
        # An address in a repr is identity leaking into the comparison. Fall back to the type,
        # which is a weaker key and is not noise. Recorded as `opaque` so it is visible.
        if _ADDR.search(r):
            return ("opaque", type(v).__name__)
        return ("val", r)

    def show(self):
        return f"raised {self.exc}" if not self.ok else repr(self.value)


def call(fn, args, timeout_steps=None):
    try:
        return Outcome(True, fn(*args))
    except Exception as e:
        # the exception TYPE is the behaviour; the message often carries addresses or paths
        return Outcome(False, exc=type(e).__name__)
    except BaseException as e:  # noqa: BLE001
        return Outcome(False, exc=f"BASE:{type(e).__name__}")


def relational_inputs(params, mined_ints, limit=600):
    """Argument tuples whose parameters are RELATED to each other, not drawn independently.

    FOUND BY MEASURING THE PRODUCT LAYER. At 400 inputs, differential execution missed 8 of 252
    real behaviour changes, and every single miss was tie-breaking or threshold logic:
    `_pydatetime._divide_and_round`, `_pydecimal._div_nearest`, `punycode.selective_len`.

    Those functions differ only when one argument sits at an exact boundary RELATIVE TO ANOTHER.
    `_divide_and_round(a, b)` splits on whether `a / b` lands exactly on `.5`, which happens when
    `a = k*b + b/2`. No per-parameter pool can produce that, however rich it is and however many
    constants are mined, because the condition is a relationship and the pools are independent.
    Adding more samples does not help either: the target is a measure-zero set under independent
    sampling, so the false-same rate for this shape does not fall with budget.

    So the pairs are constructed directly: exact multiples, off-by-one from a multiple, and the
    exact half-way point that makes a rounding tie.
    """
    if not params or not (2 <= len(params) <= 3):
        return []
    bases = [2, 3, 4, 5, 7, 10, 16, 100] + [b for b in mined_ints if 2 <= abs(b) <= 1000][:8]
    out = []
    for b in bases:
        if b == 0:
            continue
        for k in (-3, -1, 0, 1, 2, 7):
            half = b // 2
            for a in (k * b, k * b + 1, k * b - 1, k * b + half, k * b - half,
                      k * b + half + 1, k * b + (b + 1) // 2):
                pair = (a, b)
                out.append(pair if len(params) == 2 else pair + (k,))
                if len(out) >= limit:
                    return out
    return out


def _sweep_pool(ei, ef, es, cap=14):
    """The deterministic pool for an UNANNOTATED parameter, which is every real one.

    Mined constants come first and in full, because a value written literally in the function's
    own source is the single most likely place for its behaviour to turn. Generic corners follow,
    covering the shapes a mined constant cannot: empty, negative, zero, and the type confusions
    that catch a changed `except` clause.

    Capped, because this pool is raised to the power of the parameter count. The cap is the whole
    reason mined constants are ordered first: when it truncates, it truncates the guesses.
    """
    pool = []
    for v in list(ei)[:6]:
        pool.extend([v - 1, v, v + 1])
    pool.extend([0, 1, -1, 2])
    pool.extend(list(ef)[:2])
    pool.extend(list(es)[:2])
    pool.extend(["", None, [], [1, 2, 3], {}])
    seen, out = set(), []
    for v in pool:
        k = (type(v).__name__, repr(v))
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out[:cap]


def make_inputs(params, trials, seed, mined=((), (), ())):
    ei, ef, es = mined
    rng = random.Random(seed)
    out = []
    # deterministic corner sweep first, over generic corners AND the mined constants, so a
    # threshold named in the code is always probed at k-1, k, k+1 before random search starts
    #
    # THE GUARD USED TO BE `all(a is int for _, a in params)` AND IT NEVER FIRED IN PRODUCTION.
    # On the sandbox path parameters arrive as `(name, None)` because a repository function has
    # no usable annotation, so `None is int` is False and the entire mined-constant sweep was
    # dead code for every real function this tool has ever adjudicated. It only ever ran for the
    # annotated toy functions in the control suite, which is why the controls kept passing.
    #
    # Measured cost: `def leaf(x): return "old" if x == 987654321 else x * 2` versus the same
    # returning "new", with 987654321 written LITERALLY IN ITS OWN SOURCE, came back `no_change`
    # at budget 200. Mined, then never tried. Fourteenth defect, fourteenth to present as success.
    #
    # The sweep now runs whenever there is anything to sweep. Untyped parameters get the mined
    # constants plus generic corners, which is exactly the pool an unannotated parameter should
    # get, and the islice cap already bounds the product.
    if params and all(a is int for _, a in params):
        pool = INT_POOL[:6] + list(ei)
        pools = [pool] * len(params)
        out.extend(itertools.islice(itertools.product(*pools), min(max(trials // 2, 400), 4000)))
    elif params:
        # THE UNTYPED SWEEP GETS A BOUNDED SHARE, NOT THE FRONT OF THE QUEUE.
        #
        # First attempt gave it the same `max(trials // 2, 400)` the typed path uses, and at
        # budget 200 that is 400 combinations: the entire budget, spent before a single random
        # draw. Selftest caught it immediately, failing "reorder unsafe for strings" because the
        # string inputs that expose `a*2+b` versus `b+2*a` never got generated.
        #
        # This is the SAME mistake the relational-input work already made and documented forty
        # lines below, which is the argument for interleaving by default: a targeted generator
        # that crowds out the diverse one is a net loss, however good its targets are.
        pool = _sweep_pool(ei, ef, es)
        if pool:
            share = max(4, trials // 3)
            sweep = list(itertools.islice(itertools.product(*([pool] * len(params))), share))
            out.extend(sweep)
    # RELATIONAL PAIRS GET A SLICE OF THE BUDGET, NOT THE FRONT OF THE QUEUE.
    #
    # The first version prepended all ~378 of them, and measuring before and after showed it was
    # a REGRESSION: detection at 100 inputs fell from 95% to 79% and at 400 from 97% to 86%, while
    # only the 1-input case improved (21% to 35%). Prepending meant a budget of 100 was spent
    # entirely on small-magnitude related pairs and never reached the corner sweep or the random
    # draws, so a targeted generator crowded out the diverse one.
    #
    # Both are needed and they cover different things. The split gives relational inputs a fixed
    # minority share, enough to hit the tie-break shape that independent sampling provably cannot
    # reach, and leaves the majority for breadth.
    rel = relational_inputs(params, list(ei))
    if rel:
        share = max(4, min(len(rel), trials // 4))
        step = max(1, len(out) // share) if out else 1
        merged = []
        ri = iter(rel[:share])
        for idx, item in enumerate(out):
            if idx % step == 0:
                nxt = next(ri, None)
                if nxt is not None:
                    merged.append(nxt)
            merged.append(item)
        merged.extend(ri)
        out = merged
    while len(out) < trials:
        out.append(tuple(gen_value(a, rng, 0, ei, es, ef) for _, a in params))
    return out[:trials]


@dataclass
class Diff:
    args: tuple
    a: Outcome
    b: Outcome


@dataclass
class Result:
    same: bool
    trials: int
    diffs: list = field(default_factory=list)
    annotated: bool = True
    note: str = ""
    # A THIRD OUTCOME. `same=False, abstained=True` means "could not compare", which is not the
    # same claim as "these differ" and must never be rendered as one.
    abstained: bool = False


def _determinism_verdict(fn):
    """Ask the gate about one function. Imported lazily: determinism imports this module."""
    try:
        from determinism import classify
    except ImportError:
        return "unchecked", "determinism module unavailable"
    try:
        return classify(fn, trials=12, repeats=3)
    except Exception as e:
        return "unchecked", f"gate raised {type(e).__name__}"


def compare(fa, fb, trials=400, seed=0):
    """Differential execution. Finds differences; can never prove their absence."""
    params, annotated = signature_of(fa)
    pb, _ = signature_of(fb)
    if params is None or pb is None:
        return Result(False, 0, note="could not read a signature")
    # THE DETERMINISM GATE, ENFORCED AND NOT MERELY MEASURED.
    #
    # A comparison of two functions on the same inputs means nothing if either one disagrees with
    # ITSELF. Without this, a nondeterministic function reports DIFFERENT on its first input
    # forever, which reads exactly like a real behaviour change and is noise. Building the gate
    # and leaving it unwired would have been the same mistake as every other instrument in this
    # project that reported confidently over nothing.
    #
    # An abstention here is a THIRD outcome, never a `same=True`, because "I cannot compare this"
    # and "these agree" are different claims and collapsing them is what makes a gate dangerous.
    for fn, which in ((fa, "before"), (fb, "after")):
        verdict, why = _determinism_verdict(fn)
        if verdict == "nondeterministic":
            return Result(False, 0, abstained=True,
                          note=f"{which} version is nondeterministic, so no comparison is "
                               f"meaningful: {why}")
    if len(params) != len(pb):
        return Result(False, 0, note="different arity, so not comparable")

    mined = mine_constants(fa, fb)
    diffs = []
    for args in make_inputs(params, trials, seed, mined):
        ra, rb = call(fa, args), call(fb, args)
        if ra.key() != rb.key():
            diffs.append(Diff(args, ra, rb))
            if len(diffs) >= 3:
                break
    return Result(not diffs, trials, diffs, annotated)


def equivalence_classes(fns, names, trials=400, seed=0):
    """Group implementations by observed behaviour. The `classes` command, on real Python."""
    classes = []
    for i, f in enumerate(fns):
        placed = False
        for c in classes:
            r = compare(fns[c["members"][0]], f, trials, seed)
            if r.same:
                c["members"].append(i)
                placed = True
                break
        if not placed:
            w = None
            if classes:
                r = compare(fns[classes[0]["members"][0]], f, trials, seed)
                if r.diffs:
                    d = r.diffs[0]
                    w = f"{d.args} -> first {d.a.show()}, this {d.b.show()}"
            classes.append({"members": [i], "witness": w})
    return classes


def main():
    ap = argparse.ArgumentParser(description="RunBoth: differential execution for real Python")
    ap.add_argument("mode", choices=["same", "classes"])
    ap.add_argument("func", help="function name to compare")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--trials", type=int, default=400)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    mods = [load_module(p, f"_runboth_m{i}") for i, p in enumerate(args.files)]
    fns = []
    for p, m in zip(args.files, mods):
        f = getattr(m, args.func, None)
        if f is None:
            print(f"  {p}: no function `{args.func}`")
            sys.exit(2)
        fns.append(f)

    if args.mode == "same":
        if len(fns) != 2:
            print("  `same` takes exactly two files")
            sys.exit(2)
        r = compare(fns[0], fns[1], args.trials, args.seed)
        if args.json:
            print(json.dumps({
                "same": r.same, "rung": f"sampled({r.trials})",
                "annotated": r.annotated,
                "diffs": [{"args": repr(d.args), "a": d.a.show(), "b": d.b.show()} for d in r.diffs],
            }, indent=2))
            return
        print(f"  comparing `{args.func}` by execution\n")
        if r.note:
            print(f"  UNKNOWN: {r.note}")
        elif r.same:
            print(f"  SAME    rung: sampled({r.trials} inputs)")
            print(f"          No distinguishing input found. This is EVIDENCE, not proof:")
            print(f"          sampling can find differences and can never prove their absence.")
            if not r.annotated:
                print(f"          Some parameters are UNANNOTATED, so the inputs are guesses.")
                print(f"          Confidence is lower than the trial count suggests.")
        else:
            print(f"  DIFFERENT   ({len(r.diffs)} distinguishing input(s) shown)")
            for d in r.diffs:
                print(f"      {d.args}")
                print(f"          first : {d.a.show()}")
                print(f"          second: {d.b.show()}")
        return

    classes = equivalence_classes(fns, args.files, args.trials, args.seed)
    if args.json:
        print(json.dumps({
            "texts": len(fns), "behaviours": len(classes),
            "classes": [{"files": [args.files[m] for m in c["members"]], "witness": c["witness"]}
                        for c in classes],
        }, indent=2))
        return
    print(f"  {len(fns)} implementations of `{args.func}`\n")
    for i, c in enumerate(classes):
        print(f"  BEHAVIOUR {i + 1}  ({len(c['members'])} candidate"
              f"{'' if len(c['members']) == 1 else 's'})")
        for m in c["members"]:
            print(f"      {args.files[m]}")
        if c["witness"]:
            print(f"      differs from behaviour 1 at: {c['witness']}")
        print()
    print(f"  {'-' * 62}")
    print(f"  texts to review      : {len(fns)}")
    print(f"  BEHAVIOURS to review : {len(classes)}")
    print(f"  rung: sampled({args.trials} inputs). Evidence, not proof.")


if __name__ == "__main__":
    main()
