"""
THE DETERMINISM GATE: the prerequisite that makes every other fingerprint mean anything.

# Why this comes first

A behaviour fingerprint compares two functions by running them on the same inputs. That comparison
is only meaningful if a function run twice on ONE input gives one answer. If it does not, then
"the fingerprints differ" carries no information at all: the function differs from ITSELF.

Nothing in this project checked that. Every measurement taken today, the 88% detection, the 4%
residual, the equivalent-mutant counts, silently assumed determinism and never tested it. A
function touching time, randomness, hash ordering, object identity or ambient state would have
produced noise that reads exactly like a real behaviour change.

That is the failure mode this whole session has been about: not a wrong answer, but a confident
answer over nothing. So this gate runs BEFORE any comparison, and a nondeterministic function is
an ABSTENTION with a reason, never a verdict.

# What nondeterminism actually looks like in Python

    time / clocks           datetime.now(), time.time(), perf_counter
    randomness              random, secrets, uuid4
    identity                id(), default object repr. CROSS-PROCESS ONLY in CPython: address
                            reuse makes these stable within a run, which the controls proved twice
    hash ordering           set iteration, dict ordering. CROSS-PROCESS ONLY: PYTHONHASHSEED is
                            fixed per process and randomised between them
    ambient state           environment, locale, cwd, filesystem, network
    mutation of inputs      a function that mutates its argument returns something different on
                            the second call with the SAME argument object

That last one is the subtle one and it is common. It is not nondeterminism in the mathematical
sense; it is state leaking between trials. It has to be separated from true nondeterminism because
the fix is different: deep-copy the inputs per call. This module reports the two apart.

# The three verdicts

# The two tiers, and why one is not enough

The in-process check catches time, randomness and state leaking between calls. It provably CANNOT
catch identity or hash ordering, because CPython makes both stable within a single process. Those
show up only when the same function runs in two processes, which is precisely the situation a
fingerprint is used in: two CI runs, on two machines, days apart.

# The three verdicts

    DETERMINISTIC       repeated runs on identical inputs agree. Fingerprinting is meaningful.
    INPUT_MUTATING      repeats disagree, but agree once inputs are deep-copied per call.
                        Fingerprintable, with copying, and the mutation is itself a behaviour.
    NONDETERMINISTIC    repeats disagree even with fresh inputs. NOT fingerprintable. Abstain.
"""

import argparse
import ast
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from engine import call, make_inputs, mine_constants, signature_of  # noqa: E402

DETERMINISTIC = "deterministic"
INPUT_MUTATING = "input_mutating"
NONDETERMINISTIC = "nondeterministic"
UNCALLABLE = "uncallable"


def classify(fn, trials=40, repeats=3, seed=0):
    """Returns (verdict, evidence). Evidence is the input that exposed the problem, when there is one.

    Two passes, deliberately. The first shares the argument objects across repeats, which is what a
    naive harness does. The second deep-copies per call. A function that passes only the second is
    mutating its inputs, which is a real and fingerprintable behaviour rather than nondeterminism,
    and conflating the two would wrongly discard a large and ordinary class of code.
    """
    params, _ = signature_of(fn)
    if params is None:
        return UNCALLABLE, "no readable signature"
    inputs = make_inputs(params, trials, seed, mine_constants(fn))
    if not inputs:
        return UNCALLABLE, "no inputs could be generated"

    shared_ok = True
    evidence = None
    for args in inputs:
        first = call(fn, args)
        for _ in range(repeats - 1):
            if call(fn, args).key() != first.key():
                shared_ok = False
                evidence = args
                break
        if not shared_ok:
            break
    if shared_ok:
        return DETERMINISTIC, ""

    # second pass: fresh copies of the arguments for every single call
    for args in inputs:
        try:
            first = call(fn, copy.deepcopy(args))
        except Exception:
            continue
        for _ in range(repeats - 1):
            try:
                again = call(fn, copy.deepcopy(args))
            except Exception:
                return NONDETERMINISTIC, f"argument could not be copied at {args!r}"
            if again.key() != first.key():
                return NONDETERMINISTIC, f"differs from itself at {args!r}"
    return INPUT_MUTATING, f"stable only with fresh inputs; mutates its argument at {evidence!r}"


# ---------------------------------------------------------------------------------------
# The control. A gate that passes everything is not a gate, so it is pointed at functions
# whose verdict is known in advance and has to return them correctly.
# ---------------------------------------------------------------------------------------

CONTROLS = [
    ("pure_add", "def pure_add(a, b):\n    return a + b", DETERMINISTIC),
    ("pure_str", "def pure_str(s):\n    return len(str(s)) * 2", DETERMINISTIC),
    ("uses_random", "def uses_random(n):\n    import random\n    return random.random() + n",
     NONDETERMINISTIC),
    ("uses_time", "def uses_time(n):\n    import time\n    return time.time() * 0 + n",
     DETERMINISTIC),   # multiplied by zero: LOOKS nondeterministic, is not. The gate must not
                       # guess from the source text, and this control is what proves it does not.
    ("mutates_arg", "def mutates_arg(xs):\n    xs.append(1)\n    return len(xs)", INPUT_MUTATING),
]


CROSS_PROCESS_CONTROLS = [
    # Stable within one process and different between processes. The in-process gate CANNOT see
    # these, by construction, and pretending otherwise would be the exact failure this project
    # keeps finding: a check that cannot fail on the case it claims to cover.
    # MOVED HERE BY ITS OWN CONTROL, TWICE. This started as an in-process nondeterminism control
    # and the gate returned DETERMINISTIC. Retaining the objects to defeat address reuse did not
    # help either: CPython frees the previous call's objects and hands back the same addresses, so
    # id() is stable ACROSS CALLS inside one process. Identity nondeterminism is a cross-process
    # phenomenon in CPython, exactly like hash ordering, and the taxonomy above was wrong.
    ("uses_id",
     "def uses_id(n):\n"
     "    xs = []\n"
     "    for _ in range(3):\n"
     "        xs.append(object())\n"
     "    return id(xs[0]) % 97 + n * 0\n"),
    # TWELVE elements and the FULL ordering, not four and the first element.
    #
    # FOUND BY RUNNING ON A DIFFERENT MACHINE. This control passed on the dev box and FAILED on a
    # clean Ubuntu droplet, reporting the function stable across processes. Hash randomisation was
    # on; the problem was the control. With four short strings the same ordering recurs often by
    # chance: measured on the droplet, 4 elements over 8 runs gave 7 distinct orderings, so three
    # runs agreeing is entirely plausible. Twelve elements over 8 runs gave 8 distinct.
    #
    # This control is therefore PROBABILISTIC, and that is worth stating rather than hiding. It
    # cannot be made certain, because the thing it demonstrates is itself random. What it can be
    # is overwhelmingly likely, and a false pass means the cross-process tier was not demonstrated
    # on that machine, which SHOULD fail the selftest. A flaky control that quietly passes is the
    # exact failure this project keeps finding; a flaky control that quietly FAILS gets disabled
    # by whoever is annoyed by it, which is the same disease.
    ("set_order",
     "def set_order(n):\n"
     "    s = {chr(97 + i) + str(i) for i in range(12)}\n"
     "    return str(list(s)) + str(n)\n"),
]


def cross_process_stable(src, name, args, runs=6):
    """Is this function stable ACROSS processes? Returns (stable, evidence).

    THE SECOND TIER, and it exists because the first tier provably cannot reach this class.
    Python randomises the string hash seed per process, so set and dict iteration order is fixed
    within a run and differs between runs. Object addresses do the same. A function depending on
    either is perfectly stable to a repeat-in-one-process check and unstable in exactly the
    situation a fingerprint is used in, which is two CI runs on two machines.
    """
    import subprocess
    prog = (src + "\n"
            + "try:\n"
            + f"    print(repr({name}(*{args!r})))\n"
            + "except Exception as _e:\n"
            + "    print('EXC:' + type(_e).__name__)\n")
    seen = set()
    for _ in range(runs):
        try:
            r = subprocess.run([sys.executable, "-c", prog], capture_output=True,
                               text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return None, "could not run a subprocess"
        seen.add(r.stdout.strip())
        if len(seen) > 1:
            return False, f"differs across processes: {sorted(seen)}"
    return True, ""


def run_controls():
    print("  CONTROL: the gate must return a known answer on each of these\n")
    ok = True
    for name, src, expected in CONTROLS:
        ns = {}
        exec(compile(src, "<c>", "exec"), ns)  # noqa: S102
        got, why = classify(ns[name], trials=12, repeats=4)
        mark = "PASS" if got == expected else "FAIL"
        ok = ok and got == expected
        print(f"  {mark}  {name:<14} expected {expected:<16} got {got:<16} {why[:40]}")
    print()
    print("  CONTROL, cross-process tier: the in-process gate must MISS these, and the")
    print("  cross-process check must CATCH them. A tier that catches nothing it claims is worse")
    print("  than no tier.\n")
    for name, src in CROSS_PROCESS_CONTROLS:
        ns = {}
        exec(compile(src, "<c>", "exec"), ns)  # noqa: S102
        in_proc, _ = classify(ns[name], trials=8, repeats=4)
        stable, why = cross_process_stable(src, name, (1,))
        good = in_proc == DETERMINISTIC and stable is False
        ok = ok and good
        print(f"  {'PASS' if good else 'FAIL'}  {name:<14} in-process {in_proc:<16} "
              f"cross-process {'UNSTABLE' if stable is False else stable}")
        if why:
            print(f"        {why[:76]}")
    print()
    return ok


# The repo-scale measurement harness that used to sit here scanned hard-coded local clone paths and imported modules that do not ship, so it stays
# in the working tree rather than the release. The library half of this module is
# what the shipped tool actually uses.


# ---------------------------------------------------------------------------------------
# THE MEASUREMENT THAT ACTUALLY MATTERS: real library functions, allowed to import.
#
# The self-contained corpus above cannot reach a clock by construction, so its ~100%
# fingerprintable rate is close to tautological. This runs the same gate over functions from
# real installed modules, and it includes `random`, `uuid`, `secrets` and `time` ON PURPOSE.
# Those MUST come back nondeterministic. A sweep that reported every module clean would be
# measuring its own plumbing, and including modules whose answer is known in advance is the
# only way to tell the two apart from the outside.
# ---------------------------------------------------------------------------------------

PURE_MODULES = [
    "math", "statistics", "json", "base64", "textwrap", "difflib", "fractions", "decimal",
    "urllib.parse", "itertools", "functools", "heapq", "bisect", "string", "calendar",
    "colorsys", "binascii", "codecs", "html", "ipaddress", "posixpath", "ntpath", "shlex",
    "quopri", "unicodedata", "operator", "copy", "keyword", "token", "stat", "numbers",
]

# Known-nondeterministic. The gate must flag these or it is not working.
IMPURE_MODULES = ["random", "uuid", "secrets", "time"]


def sweep_modules(mods, label, per_module=40):
    import importlib
    import inspect

    counts = {DETERMINISTIC: 0, INPUT_MUTATING: 0, NONDETERMINISTIC: 0, UNCALLABLE: 0}
    flagged = []
    for mname in mods:
        try:
            mod = importlib.import_module(mname)
        except Exception:
            continue
        n = 0
        for fname, fn in sorted(vars(mod).items()):
            if n >= per_module or fname.startswith("_"):
                continue
            if not (inspect.isfunction(fn) or inspect.isbuiltin(fn)):
                continue
            params, _ = signature_of(fn)
            if params is None or not (1 <= len(params) <= 3):
                continue
            n += 1
            try:
                verdict, why = classify(fn, trials=12, repeats=4)
            except Exception:
                verdict, why = UNCALLABLE, "classifier raised"
            counts[verdict] += 1
            if verdict == NONDETERMINISTIC and len(flagged) < 14:
                flagged.append((mname, fname, why))
    tot = max(sum(counts.values()), 1)
    print(f"\n  {label}: {sum(counts.values())} functions from {len(mods)} modules")
    for k in (DETERMINISTIC, INPUT_MUTATING, NONDETERMINISTIC, UNCALLABLE):
        print(f"    {k:<20} {counts[k]:>5} {counts[k] / tot:>6.0%}")
    if flagged:
        print("    flagged nondeterministic:")
        for m, f, why in flagged:
            print(f"      {m + '.' + f:<34} {why[:44]}")
    return counts


if __name__ == "__main__" and "--modules" in sys.argv:
    print("  DETERMINISM OVER REAL LIBRARY FUNCTIONS (allowed to import)\n")
    print("  The self-contained corpus could not reach a clock by construction. This can.")
    print("  The impure modules are included deliberately: if the gate does not flag them,")
    print("  nothing it says about the pure ones is worth reading.")
    imp = sweep_modules(IMPURE_MODULES, "KNOWN-IMPURE (control, must flag)")
    pure = sweep_modules(PURE_MODULES, "ORDINARY LIBRARY MODULES")
    caught = imp[NONDETERMINISTIC]
    print(f"\n  control: {caught} nondeterministic found in the impure modules")
    if caught == 0:
        print("  CONTROL FAILED. The gate flagged nothing in random/uuid/secrets/time, so the")
        print("  pure-module numbers above are measuring the harness and not the code.")
    else:
        fp = pure[DETERMINISTIC] + pure[INPUT_MUTATING]
        tot = max(sum(pure.values()), 1)
        print(f"  FINGERPRINTABLE in ordinary modules: {fp} of {tot} ({fp / tot:.0%})")
    sys.exit(0)
