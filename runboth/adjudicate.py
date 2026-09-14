"""
THE PRODUCT: adjudicate every changed function between two revisions of a real repository.

Everything else in this tree measures. This is the thing a user runs.

    runboth adjudicate <repo> <base_sha> <head_sha>

and gets, per changed function, one of:

    no_change     behaviour identical at budget N. Review not required.
    changed       behaviour differs, WITH THE INPUT THAT PROVES IT.
    abstained     could not be compared, WITH THE REASON. Never silently dropped.

# The contract, which names no vendor

The JSON below is the whole integration surface. A CLI, an MCP server, a GitHub Action and a REST
endpoint are all thin wrappers on it, so no single buyer's rejection costs more than a wrapper.

    {"function": "pkg.mod.name",
     "verdict":  "no_change" | "changed" | "abstained",
     "rung":     "sampled(400)",
     "budget":   400,
     "witness":  {"args": [...], "before": "...", "after": "..."},   # changed only
     "reason":   "...",                                              # abstained only
     "claim":    "refactor" | null,          # what the author said
     "claim_ok": true | false | null}        # whether the claim survived checking

# Three production properties, each of which was learned the hard way today

1. **It never says "same".** It says `no_change at budget N`, because the false-same rate is 12%
   at 20 inputs and 4% at 400. A UI reporting "no changes" would be actively dangerous.
2. **Abstention is a first-class verdict with a reason.** Six defects today all presented as
   success; the fix is that a thing which could not be checked can never look like a thing that
   passed.
3. **Nothing it is handed can crash it.** A repo is adversarial input. Every stage catches, and
   an uncatchable stage is a bug in this file.

# What it does NOT do, said here so nobody has to discover it

It compares functions that can be CALLED in isolation with generated inputs. It does not build
your project, resolve your dependencies, or run your test suite. On a repo whose functions need
fixtures, most results will be abstentions with `not constructible` as the reason, and that count
is printed at the top of every run instead of being buried.
"""

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from determinism import DETERMINISTIC, INPUT_MUTATING, classify  # noqa: E402
from isolate import build, harvestable  # noqa: E402
from engine import compare  # noqa: E402


# ---------------------------------------------------------------------------------------
# Reading two revisions of a repository, without checking anything out.
# ---------------------------------------------------------------------------------------

def git(repo, *args, binary=False):
    """Run git and return stdout, or None. Never raises: a repo is adversarial input."""
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


# Directories whose functions are not the product. A reviewer reading a pull-request comment cares
# that the LIBRARY changed; a helper inside a benchmark script that was added by the same commit is
# noise wearing the same typeface as a finding. Measured on 2026-09-12 against sqlparse's ReDoS fix
# (d1d8060274): seven findings, five of them helpers in `benchmarks/bench_dollar_quote_redos.py`,
# which is a file whose whole purpose is to be rewritten alongside the fix.
#
# Set RUNBOTH_ALL_PATHS=1 to adjudicate everything, which is what the coverage studies want.
#
# `scripts/` and `fixtures/` were in this list and were REMOVED on the same day, before shipping:
# plenty of projects keep real product code under `scripts/`, and skipping real code is a worse
# failure than reporting noise. Silence is how this tool says "no difference found", so a
# directory only belongs here when its contents are unambiguously not the product.
_NOISE_DIRS = ("test", "tests", "testing", "benchmarks", "benchmark", "bench",
               "examples", "example", "docs", "doc")

# BUILD AND TASK-RUNNER FILES. Not the product, and they import dev-only dependencies that are not
# installed in the environment running the adjudication, so they abstain en masse and drown the
# real signal. Measured 2026-09-12 on pypa/packaging: 51 of 55 abstentions were a single reason,
# "No module named 'nox'", from `noxfile.py`, which holds 17 functions and appeared in three of
# six sampled commits. Those three commits produced nothing but abstentions.
_NOISE_FILES = ("noxfile.py", "conftest.py", "setup.py", "tasks.py", "fabfile.py",
                "manage.py", "dodo.py", "sconstruct.py", "wscript.py")


def is_noise_path(path):
    """True for a path whose functions should not be REPORTED as product findings.

    Deliberately conservative: it matches whole directory names only, so `src/contest/` and a
    module named `benchmarks.py` are both left alone. Only a directory called exactly `tests`
    (and friends) counts, plus the `test_*.py` / `*_test.py` file convention.
    """
    parts = path.replace("\\", "/").split("/")
    if any(p.lower() in _NOISE_DIRS for p in parts[:-1]):
        return True
    name = parts[-1].lower()
    return (name.startswith("test_") or name.endswith("_test.py")
            or name in _NOISE_FILES)


def changed_python_files(repo, base, head, include_all=None):
    out = git(repo, "diff", "--name-only", f"{base}..{head}", "--", "*.py")
    files = [l.strip() for l in (out or "").splitlines() if l.strip().endswith(".py")]
    if include_all is None:
        include_all = os.environ.get("RUNBOTH_ALL_PATHS") == "1"
    if include_all:
        return files
    return [f for f in files if not is_noise_path(f)]


def file_at(repo, rev, path):
    return git(repo, "show", f"{rev}:{path}")


def functions_in(src, path, strict=False):
    """Top-level and method functions, keyed by a qualified name stable across revisions.

    `strict` raises on unparseable source instead of returning {}. A caller that cannot tell
    "this file has no functions" from "this file does not parse" will report the second as the
    first, which is the exact failure mode this project exists to remove. Default stays False so
    existing batch callers keep skipping broken files quietly.
    """
    out = {}
    try:
        tree = ast.parse(src or "")
    except (SyntaxError, ValueError):
        if strict:
            raise
        return out

    def walk(node, prefix):
        for child in getattr(node, "body", []):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.FunctionDef):
                out[f"{path}::{prefix}{child.name}"] = child
    walk(tree, "")
    return out


# ---------------------------------------------------------------------------------------
# Adjudicating one function pair.
# ---------------------------------------------------------------------------------------

def _touches_module_state(node, module_src):
    """True if this function could be affected by a change ELSEWHERE in its module or imports.

    Sound in the direction that matters: it may say True for a function that is in fact a leaf
    (costing a comparison that finds nothing), and it must never say False for one that is not.
    So anything it cannot resolve counts as touching state.

    A name is dangerous when it is FREE in the function (not a parameter, not locally assigned)
    and is not a builtin. Attribute access on such a name counts through the base name.
    """
    import builtins as _b
    bound = set()
    for a in list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs):
        bound.add(a.arg)
    for extra in (node.args.vararg, node.args.kwarg):
        if extra is not None:
            bound.add(extra.arg)
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                bound.add((al.asname or al.name).split(".")[0])
        elif isinstance(n, (ast.comprehension,)):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
    builtin_names = set(dir(_b))
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id not in bound and n.id not in builtin_names:
                return True
    return False


_DYNAMIC_CALLS = {"getattr", "setattr", "eval", "exec", "__import__", "globals", "locals",
                  "vars", "compile", "importlib", "partial", "wraps"}


def _short_names(q):
    """BOTH spellings of a function's name, and the reason is a live false-negative.

    Definitions are keyed `path::Class.method`, so the short name is `Class.method`. A CALL SITE
    writes `self.exec_module(...)`, whose AST attribute is the bare `exec_module`. Matching only
    the qualified spelling means the two never meet, so EVERY METHOD IS AN ISOLATED NODE in the
    call graph: nothing reaches it and nothing it calls is reachable through it.

    That is not academic. `_t_prune.sh` caught `tlz/_build_tlz.py::TlzLoader.load_module` coming
    back `no_change` under the prune and `changed` when actually executed, because its only path
    to an edited function runs through `self.exec_module`, an edge that never existed.

    Returning both spellings only ever ADDS edges, which can only make more functions reachable
    and therefore executed. The error direction is the safe one by construction.
    """
    s = q.split("::", 1)[-1]
    return {s, s.rsplit(".", 1)[-1]}


def _dynamic(node):
    """True if this function's call structure cannot be read statically. ONE-SIDED ON PURPOSE.

    False must mean "definitely legible". True may over-fire, costing an execution that finds
    nothing. That asymmetry is the whole point: over-firing wastes time, under-firing emits a
    `no_change` for code that was never run and could not be reasoned about.
    """
    if node.decorator_list:
        # A decorator can replace the body entirely, so the source in front of us is not
        # necessarily the code that runs.
        return True
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id in _DYNAMIC_CALLS:
            return True
        if isinstance(n, ast.Attribute) and n.attr in _DYNAMIC_CALLS:
            return True
        if isinstance(n, ast.Global) or isinstance(n, ast.Nonlocal):
            return True
        if isinstance(n, ast.Call) and not isinstance(n.func, (ast.Name, ast.Attribute)):
            # Calling the result of an expression: a dispatch table, a returned closure, a
            # lambda from a dict. There is no name to follow.
            return True
    return False


def adjudicate_pair(qname, before_node, after_node, budget,
                    before_mod=None, after_mod=None,
                    before_root=None, after_root=None, extra=None):
    """One verdict. Every path returns a dict; nothing here may raise.

    `extra` carries concrete values a CALLEE was already proven to differ at, so a caller is not
    searched blindly around evidence that is already in hand. See the note in compare_in_sandbox.
    """
    rec = {"function": qname, "rung": f"sampled({budget})", "budget": budget,
           "witness": None, "reason": None}

    if before_node is None:
        return {**rec, "verdict": "changed", "reason": "added in this revision"}
    if after_node is None:
        return {**rec, "verdict": "changed", "reason": "deleted in this revision"}

    try:
        b_src, a_src = ast.unparse(before_node), ast.unparse(after_node)
    except Exception as e:
        return {**rec, "verdict": "abstained", "reason": f"unparseable: {type(e).__name__}"}

    if b_src == a_src:
        # IDENTICAL SOURCE IS NOT IDENTICAL BEHAVIOUR, and believing it was is the single worst
        # defect this project has had. A function whose text did not change still changes when
        # something it CALLS changes, and that is precisely the blast radius: the regression class
        # no diff-reader can ever see, because there is no diff to read. A tool whose pitch is
        # "we run the code instead of reading the diff" was reading the diff for the one case
        # where reading it cannot work.
        #
        # The fast path survives only where it is SOUND: when both sides resolve against the same
        # tree (nothing underneath can differ), or when the function is a LEAF that touches no
        # module-level or imported name at all.
        same_tree = (before_root is None or after_root is None or
                     str(before_root) == str(after_root))
        if same_tree or not _touches_module_state(after_node, after_mod):
            return {**rec, "verdict": "no_change", "reason": "identical source"}
        # Otherwise fall through and actually RUN it in both trees.

    # Signature changes are behaviour changes by definition, and comparing across them is
    # meaningless because the two functions do not accept the same calls.
    ba = [a.arg for a in before_node.args.args]
    aa = [a.arg for a in after_node.args.args]
    if len(ba) != len(aa):
        return {**rec, "verdict": "changed",
                "reason": f"arity changed, {len(ba)} to {len(aa)}"}

    # A METHOD needs an instance, so it never goes down the isolated-function path.
    # qname is `path::Class.method`; anything with a dot after the :: is a method.
    _short = qname.split("::", 1)[-1]
    if "." in _short and before_mod is not None and after_mod is not None:
        from methods import compare_method
        cls, meth = _short.rsplit(".", 1)
        return {**compare_method(before_mod, after_mod, cls, meth, budget,
                                 before_root=before_root, after_root=after_root,
                                 rel_path=qname.split("::", 1)[0],
                                 repo_root=after_root),
                "function": qname}

    # NOT SELF-CONTAINED? Use the sandbox instead of giving up. This is the difference between
    # abstaining on most of a real repository and adjudicating it: real code imports.
    if not (harvestable(before_node) and harvestable(after_node)):
        if before_mod is not None and after_mod is not None:
            from sandbox import compare_in_sandbox
            params = [(a.arg, None) for a in after_node.args.args]
            return {**compare_in_sandbox(before_mod, after_mod, after_node.name,
                                         params, budget,
                                         before_root=before_root,
                                         after_root=after_root,
                                         rel_path=qname.split("::", 1)[0],
                                         extra=extra,
                                         # The nodes are what make "which lines changed" a
                                         # answerable question, and therefore what lets a
                                         # `no_change` say whether it reached the change at all.
                                         before_node=before_node, after_node=after_node),
                    "function": qname}
        return {**rec, "verdict": "abstained",
                "reason": "not constructible in isolation and no module source was supplied"}

    fb = build(b_src, before_node.name)
    fa = build(a_src, after_node.name)
    if fb is None or fa is None:
        return {**rec, "verdict": "abstained", "reason": "could not be compiled in isolation"}

    for fn, which in ((fb, "before"), (fa, "after")):
        try:
            verdict, why = classify(fn, trials=12, repeats=3)
        except Exception as e:
            return {**rec, "verdict": "abstained",
                    "reason": f"determinism gate raised {type(e).__name__}"}
        if verdict not in (DETERMINISTIC, INPUT_MUTATING):
            return {**rec, "verdict": "abstained",
                    "reason": f"{which} version is {verdict}: {why}"}

    try:
        res = compare(fb, fa, trials=budget)
    except Exception as e:
        return {**rec, "verdict": "abstained", "reason": f"comparison raised {type(e).__name__}"}

    if getattr(res, "abstained", False):
        return {**rec, "verdict": "abstained", "reason": res.note}
    if res.same:
        return {**rec, "verdict": "no_change",
                "reason": f"no difference found in {res.trials} inputs"}
    d = res.diffs[0] if res.diffs else None
    witness = None
    if d is not None:
        witness = {"args": [repr(x) for x in d.args], "before": d.a.show(), "after": d.b.show()}
    return {**rec, "verdict": "changed", "witness": witness,
            "reason": "differs on a generated input"}


def materialise(repo, rev):
    """Extract a whole revision into a temp directory. Returns the path, or None.

    A single module written to a temp file has no package around it, so `from pkg.x import y`
    fails and the function abstains for a reason unrelated to its behaviour. That is exactly what
    toolz did on the first real-repo run: 2 of 40 abstained with ModuleNotFoundError.

    `git archive` writes the tree at a revision without a checkout and without touching the
    working directory, which is the same constraint merge-check already respects: a tool that
    disturbs a developer's uncommitted work does not get a second trial.
    """
    import tempfile
    d = tempfile.mkdtemp(prefix=f"runboth_tree_{rev[:8]}_")
    tar = git(repo, "archive", "--format=tar", rev, binary=True)
    if tar is None:
        return None
    import io
    import tarfile
    try:
        with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
            tf.extractall(d, filter="data")
    except Exception:
        return None
    _stub_generated_version(d)
    return d


def _stub_generated_version(root):
    """Write the `_version.py` that the BUILD would have written, when it is missing.

    setuptools_scm and friends generate `pkg/_version.py` at build time and gitignore it,
    so it is never in the tree `git archive` gives us. The package's `__init__` imports it,
    the import raises ModuleNotFoundError, and EVERY function in the package abstains for a
    reason that has nothing to do with its behaviour.

    Measured 2026-09-14 on humanize: 20 of 23 functions abstained, all of them on
    `No module named 'humanize._version'`, dropping the adjudicated rate to 13%. This is not
    a rare shape; it is most of the modern packaging ecosystem.

    Deliberately narrow: only a directory that is already a package, only when `_version.py`
    is absent, and only when a sibling module actually mentions `_version`. The honest cost is
    that a commit which changes how a version string is DERIVED will not be measured, since
    both sides now get the same stub. That trades a rare miss for a whole ecosystem of
    functions that could not be executed at all, and the stub is identical on both sides so it
    can never manufacture a `changed`.
    """
    import os as _o
    if _o.environ.get("RUNBOTH_NO_VERSION_STUB"):
        return  # escape hatch: measure the cost of the stub, or refuse it on a repo it harms
    for dirpath, _dirs, files in _o.walk(root):
        if "__init__.py" not in files or "_version.py" in files:
            continue
        if not any(f.endswith(".py") and _needs_version_stub(_read_quiet(_o.path.join(dirpath, f)))
                   for f in files):
            continue
        try:
            with open(_o.path.join(dirpath, "_version.py"), "w", encoding="utf-8") as fh:
                fh.write("# synthesised by RunBoth: the build generates this file and git "
                         "does not carry it.\n"
                         '__version__ = version = "0.0.0"\n'
                         "__version_tuple__ = version_tuple = (0, 0, 0)\n")
        except OSError:
            pass


def _needs_version_stub(src):
    """True only when importing `_version` would HARD FAIL this module.

    A package that already guards the import and falls back is working, and stubbing it
    changes an answer that was never broken. dateutil does exactly this:

        try:
            from ._version import version as __version__
        except ImportError:
            __version__ = 'unknown'

    Measured 2026-09-14: without this check the stub flipped dateutil's __version__ from
    'unknown' to '0.0.0' on a package that imported perfectly well. Both sides get the same
    stub so it could not have manufactured a `changed`, but changing behaviour that was not
    broken is the one thing this tool may never do. So: only an UNGUARDED import counts.
    """
    if "_version" not in src:
        return False
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False

    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            catches_import = any(
                h.type is None
                or (isinstance(h.type, ast.Name) and h.type.id in ("ImportError", "Exception"))
                or (isinstance(h.type, ast.Tuple)
                    and any(isinstance(e, ast.Name) and e.id in ("ImportError", "Exception")
                            for e in h.type.elts))
                for h in node.handlers)
            if catches_import:
                for sub in node.body:
                    for n2 in ast.walk(sub):
                        guarded.add(id(n2))

    for node in ast.walk(tree):
        hit = False
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("_version"):
            hit = True
        elif isinstance(node, ast.Import):
            hit = any(al.name.endswith("_version") for al in node.names)
        if hit and id(node) not in guarded:
            return True
    return False


def _read_quiet(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def call_text(qname_or_name, args):
    """Render the call that produced a witness, the way someone would type it.

    A METHOD witness carries two pieces, the construction and the call:

        ["FIFOCache(-2)", ".clear()"]

    They are meant to be concatenated, and every report surface was comma-joining them and
    wrapping the result in the function name again, which printed

        FIFOCache.clear(FIFOCache(-2), .clear())

    instead of `FIFOCache(-2).clear()`. Found by red team on 2026-09-12; it affected every method
    finding the tool has ever printed, not just constructors, so it lived in the output of the
    most convincing part of the product.
    """
    args = [str(a) for a in (args or [])]
    name = str(qname_or_name).split("::", 1)[-1]
    if len(args) == 2 and args[1].startswith("."):
        # A CONSTRUCTOR is written the way it is called. The harness treats `__init__` as a method
        # on an already-built object, which is right for measuring it and wrong for printing it:
        # `Cache(-2).__init__(-6, 2)` is not how anyone constructs a Cache. The arguments that
        # matter are the ones passed to __init__, so it renders as `Cache(-6, 2)`.
        if args[1].startswith(".__init__("):
            cls = args[0].split("(", 1)[0]
            inner = args[1][len(".__init__("):].rstrip(")")
            return f"{cls}({inner})"
        return args[0] + args[1]
    return f"{name}({', '.join(args)})"


def _is_ctor_failure(rec):
    """The AFTER side is nothing but a failed construction, so the method never ran.

    The engine emits `['ctor', '<ExceptionType>']` for exactly this: it could build the object on
    the before side and could not on the after side, so there is no method behaviour to compare.
    """
    w = rec.get("witness") or {}
    return str(w.get("after", "")).startswith("['ctor'")


def collapse_constructor_findings(records):
    """One root cause, one finding.

    Found by red team on 2026-09-12. cachetools `dd181c5a72` adds two lines rejecting a negative
    `maxsize` in `Cache.__init__`. The engine reported 45 functions changed, 39 of whose witnesses
    said nothing but "can no longer be constructed":

        FIFOCache.clear    FIFOCache(-2).clear()
          used to:  return None
          now:      fail to construct (ValueError)

    `FIFOCache.clear` did not change. What changed is that `FIFOCache(-2)` no longer exists. Every
    one of those statements is true and none of them is a false positive, but attributing the
    change to `clear` is a category error, and a first pull request showing 45 findings for one
    intended line is a tool that gets switched off.

    So they collapse into the constructor that caused them, which keeps the count honest without
    repeating one fact 39 times. Returns (kept_records, rollup) where rollup maps a
    "path::Class.__init__" key to the number of methods folded into it, plus a list of the classes
    involved. Records that are not constructor failures pass through untouched and in order.
    """
    by_class = {}
    for r in records:
        if r.get("verdict") != "changed" or not _is_ctor_failure(r):
            continue
        path, _, short = r.get("function", "").partition("::")
        if "." not in short:
            continue
        cls = short.rsplit(".", 1)[0]
        by_class.setdefault((path, cls), []).append(r)

    if not by_class:
        return list(records), {}

    # A constructor finding to hang them on: the class's own __init__ when it was reported, else
    # any changed __init__ in the same file, which is the inherited-constructor case (cachetools
    # has exactly this: FIFOCache methods fail because Cache.__init__ gained the check).
    changed_ctors = {r["function"] for r in records
                     if r.get("verdict") == "changed" and r["function"].endswith(".__init__")}

    folded, rollup = set(), {}
    for (path, cls), recs in by_class.items():
        own = f"{path}::{cls}.__init__"
        anchor = own if own in changed_ctors else next(
            (q for q in sorted(changed_ctors) if q.startswith(f"{path}::")), None)
        if anchor is None:
            # Nothing to attribute them to. Keep them rather than invent an anchor: dropping a
            # finding with no home would be the tool going quiet about a real difference.
            continue
        for r in recs:
            if r["function"] == anchor:
                continue          # never fold the anchor into itself
            folded.add(r["function"])
        entry = rollup.setdefault(anchor, {"methods": 0, "classes": set()})
        entry["methods"] += sum(1 for r in recs if r["function"] != anchor)
        if any(r["function"] != anchor for r in recs):
            entry["classes"].add(cls)

    kept = [r for r in records if r.get("function") not in folded]
    for v in rollup.values():
        v["classes"] = sorted(v["classes"])
    rollup = {k: v for k, v in rollup.items() if v["methods"]}
    return kept, rollup


def constructor_rollup_line(rollup, anchor):
    """The one sentence that replaces the folded findings, or None."""
    entry = rollup.get(anchor)
    if not entry or not entry["methods"]:
        return None
    n, classes = entry["methods"], entry["classes"]
    who = ", ".join(classes[:4]) + (f" and {len(classes) - 4} more" if len(classes) > 4 else "")
    if n == 1:
        return f"1 method of {who} can no longer be constructed with that input, and is not listed separately."
    return (f"{n} methods of {who} can no longer be constructed with that input, "
            f"and are not listed separately.")


def adjudicate(repo, base, head, budget=400, progress=None):
    files = changed_python_files(repo, base, head)
    if not files:
        # SAY WHICH, never just go quiet. A commit that touches only tests or benchmarks has no
        # library behaviour to report, and answering that instantly is strictly better than
        # spending the whole budget on a test file. Measured on 2026-09-12: three of eight
        # more-itertools commits hit a 700s timeout and returned NOTHING, and all three touched
        # only `tests/test_more.py`. Naming the skipped files keeps silence from covering them.
        all_files = changed_python_files(repo, base, head, include_all=True)
        if all_files:
            listed = ", ".join(all_files[:6])
            more = f" and {len(all_files) - 6} more" if len(all_files) > 6 else ""
            verb = "is" if len(all_files) == 1 and not more else "are"
            return [], (f"no library Python files changed; {listed}{more} {verb} tests, "
                        "benchmarks or docs (set RUNBOTH_ALL_PATHS=1 to include them)")
        return [], "no changed Python files between those revisions"
    base_root = materialise(repo, base)
    head_root = materialise(repo, head)

    # PARALLELISM, because scale is the gap that decides whether this runs on a customer repo.
    # Each comparison spawns three interpreters (before, after, and the cross-process determinism
    # re-run), and they were serialised on one core. On toolz that was 40 functions in a minute;
    # on a monorepo it would never finish. The work is embarrassingly parallel: every function is
    # independent, nothing shares state, and the sandbox already isolates each run.
    #
    # Workers are capped at cpu_count, not more: each worker itself spawns sandbox subprocesses,
    # so oversubscribing multiplies process count instead of throughput.
    import concurrent.futures as _cf
    import os as _os

    # THE CALLER CLOSURE, and it is a pure win. Removing the identical-source fast path was
    # correct and it cost a large multiple in runtime, because every function in every changed
    # file then got EXECUTED rather than skipped: jinja went to 757s, which is not a pull-request
    # gate anyone keeps. But a function that cannot reach an edited one provably cannot have
    # moved, so running it spends the whole budget to confirm what the call graph already knows.
    #
    # Computed ACROSS ALL CHANGED FILES AT ONCE, not per file. A function in one changed file
    # routinely calls something in another, and a per-file closure would drop exactly those.
    per_file, edited = {}, set()
    for path in files:
        b_src = file_at(repo, base, path)
        a_src = file_at(repo, head, path)
        before = functions_in(b_src, path)
        after = functions_in(a_src, path)
        per_file[path] = (b_src, a_src, before, after)
        for q in set(before) | set(after):
            b, a = before.get(q), after.get(q)
            if b is None or a is None:
                edited |= _short_names(q)
                continue
            try:
                if ast.dump(b) != ast.dump(a):
                    edited |= _short_names(q)
            except (RecursionError, AttributeError):
                edited |= _short_names(q)

    calls, all_names = {}, set()
    for _p, (_b, _a, _bf, af) in per_file.items():
        for q, node in af.items():
            all_names |= _short_names(q)
    for _p, (_b, _a, _bf, af) in per_file.items():
        for q, node in af.items():
            n = q.split("::", 1)[-1]
            calls[q] = {c.id for c in ast.walk(node)
                        if isinstance(c, ast.Name) and c.id in all_names and c.id != n} | \
                       {c.attr for c in ast.walk(node)
                        if isinstance(c, ast.Attribute) and c.attr in all_names}
    # ANYTHING WHOSE REACHABILITY CANNOT BE READ STATICALLY IS NEVER PRUNED.
    #
    # The prune emits `no_change` for a function it did not execute, which is the most dangerous
    # verdict this tool can produce: a FALSE no_change is the one error that makes it worse than
    # useless, because the whole contract is that "no" and "cannot tell" stay separate. The claim
    # rests on a NAME-BASED call graph, and a name-based graph cannot see `getattr(obj, name)()`,
    # a dispatch table, a decorator that swaps the body, `eval`, or a global that an edited
    # function mutates. Each of those is an invisible edge, and an invisible edge means the
    # "no path to an edited function" premise is simply false.
    #
    # So opacity is treated as reachability. A function that does anything this cannot read is
    # EXECUTED, exactly as before, and only functions whose call structure is fully legible get
    # the static answer. That costs speed on dynamic code and it is the correct trade: the prune
    # is an optimisation, and an optimisation is never allowed to buy time with a wrong verdict.
    opaque = set()
    for _p, (_b, _a, _bf, af) in per_file.items():
        for q, node in af.items():
            if _dynamic(node):
                opaque.add(q.split("::", 1)[-1])
    edited |= opaque

    reach = set(edited)
    # THE PRUNE IS OPT-IN, AND WAS DEFAULT-ON UNTIL IT EMITTED A FALSE `no_change`.
    #
    # `_t_prune.sh` caught `TlzLoader.load_module` coming back `no_change` under the prune and
    # `changed` when executed. Cause found, not guessed: the call graph keyed definitions as
    # `Class.method` while call sites say `.method`, so every method was an ISOLATED NODE and the
    # "no call path to an edited function" premise was false for all of them. `_short_names` fixes
    # that specific hole and the disagreement is gone.
    #
    # It stays OFF anyway. The remaining static `no_change` claims rest on this call graph being
    # sound, and the only evidence about its soundness so far is that it was badly wrong. A
    # verdict that says `no_change` for code that never ran needs positive evidence, not the
    # absence of a known counterexample, and this is the one verdict the whole contract rests on.
    # Turn it on with RUNBOTH_PRUNE=1 to measure the speedup; ship it only when the graph is earned.
    if not __import__("os").environ.get("RUNBOTH_PRUNE"):
        reach = all_names | edited
    for _ in range(8):
        grew = set()
        for q, tgt in calls.items():
            if tgt & reach:
                grew |= _short_names(q)
        grew -= reach
        if not grew:
            break
        reach |= grew

    # A PRUNED FUNCTION IS STILL AN ANSWER, and dropping it would shrink the totals and read as
    # lost coverage. Its source did not change and it has no path to anything that did, so
    # `no_change` here is a static proof and not a sample: it is the one verdict in this whole
    # project that does not carry a budget caveat, and it says so.
    # PAIR RENAMED AND MOVED FUNCTIONS, or their behaviour is never compared at all.
    #
    # Functions are keyed `path::name`, so `foo` becoming `_foo`, or moving to another module,
    # reads as a delete plus an add. Neither half has a counterpart, so neither is ever executed,
    # and the verdict is a pair of structural notes with no behaviour claim behind them.
    #
    # That is not an edge case. click 333c28d7 ("Mark clearly functions private status") renamed
    # 45 functions, broke 13 tests, and runboth reported ZERO behaviour changes because every
    # renamed function had lost its partner. RENAMING AND MOVING IS WHAT A REFACTOR IS, and a
    # refactor is the population this tool exists for.
    #
    # Two matches only, both unambiguous, because a wrong pairing compares unrelated functions
    # and invents a difference: same name in a different file (moved), and a name differing only
    # in leading underscores (made private). Anything else stays structural and says so.
    orphan_b, orphan_a = {}, {}
    for path in files:
        b_src, a_src, before, after = per_file[path]
        for q in set(before) - set(after):
            orphan_b[q] = (path, b_src, before[q])
        for q in set(after) - set(before):
            orphan_a[q] = (path, a_src, after[q])

    def short(q):
        return q.split("::", 1)[-1]

    paired, used_a = {}, set()
    for qb, (pb, sb, nb) in sorted(orphan_b.items()):
        nameb = short(qb)
        cands = [qa for qa in orphan_a
                 if qa not in used_a
                 and (short(qa) == nameb or short(qa).lstrip("_") == nameb.lstrip("_"))]
        if len(cands) != 1:
            continue                       # ambiguous is worse than unpaired: never guess
        qa = cands[0]
        used_a.add(qa)
        pa, sa, na = orphan_a[qa]
        paired[qb] = (qa, sb, sa, nb, na, short(qa) != nameb)

    jobs, static = [], []
    for qb, (qa, sb, sa, nb, na, renamed) in sorted(paired.items()):
        label = f"{qb} -> {short(qa)}" if renamed else f"{qb} (moved)"
        jobs.append((label, nb, na, budget, sb, sa, base_root, head_root))

    for path in files:
        b_src, a_src, before, after = per_file[path]
        for qname in sorted(set(before) | set(after)):
            if qname in paired or qname in used_a:
                continue                   # already adjudicated as a rename/move pair
            if qname.split("::", 1)[-1] not in reach:
                static.append({"function": qname, "verdict": "no_change", "rung": "static",
                               "budget": 0, "witness": None,
                               "reason": "source unchanged and no call path to an edited function"})
                continue
            jobs.append((qname, before.get(qname), after.get(qname), budget,
                         b_src, a_src, base_root, head_root))

    def _safe_pair(j):
        # ONE FUNCTION CAN NEVER END THE RUN. On real flask a sandbox payload too deep to decode
        # raised out of adjudicate_pair, the thread pool re-raised it, and 174 functions' worth
        # of work died with one traceback. An internal failure on one function is an abstention
        # with the exception named, which is what every other failure in this tool already is.
        try:
            return adjudicate_pair(*j)
        except Exception as e:  # noqa: BLE001
            qname, _b, _a, budget = j[0], j[1], j[2], j[3]
            return {"function": qname, "verdict": "abstained", "rung": f"sampled({budget})",
                    "budget": budget, "witness": None,
                    "reason": f"internal error: {type(e).__name__}: {str(e)[:120]}"}

    # THE SAME CAP THE GATE ALREADY HAD, which this path never got. Each job spawns THREE
    # interpreters (before, after, and the cross-process determinism re-run), so `cpu_count()`
    # workers means three times that many processes fighting for the machine. Measured on
    # 2026-09-12 on a 20-core box: sqlparse abstained on 37.8% of functions with two jobs sharing
    # the machine and 7.8% idle, the SAME commits at the SAME budget. Every one of those extra
    # abstentions was the 30s wall clock, which is a measure of contention rather than of the
    # code. A verdict must not depend on how busy the machine is, and a CI runner is two shared
    # cores, which is the busy case by default.
    workers = max(1, min(len(jobs),
                         int(_os.environ.get("RUNBOTH_WORKERS", "4")),
                         (_os.cpu_count() or 2)))
    # SILENCE IS NOT A STATUS. This runs for minutes on a real repository and printed
    # nothing until it finished, so a user watching it cannot tell work from a hang, and
    # neither could I: on 2026-09-14 I killed my own runs twice assuming they were stuck.
    # A tool whose whole claim is saying what it checked and how hard should not go quiet
    # while doing it. Progress goes to STDERR only, the same place the controls narrate
    # under --json, so the stdout contract is untouched and a pipeline sees no difference.
    total = len(jobs)

    def _tick(n, qname):
        if progress is None:
            return
        try:
            progress.write("\r  adjudicating %d/%d  %-46s" % (n, total, str(qname)[:46]))
            progress.flush()
        except Exception:  # noqa: BLE001  a broken pipe must never end the run
            pass

    def _tick_done():
        if progress is None:
            return
        try:
            progress.write("\r" + " " * 72 + "\r")
            progress.flush()
        except Exception:  # noqa: BLE001
            pass

    if workers == 1 or len(jobs) <= 2:
        out = list(static)
        for i, j in enumerate(jobs, 1):
            _tick(i, j[0])
            out.append(_safe_pair(j))
        _tick_done()
        return out, ""
    out = list(static)
    with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
        # Threads, not processes: the work these threads do is almost entirely WAITING on sandbox
        # subprocesses, so the GIL is released for the duration and a process pool would only add
        # pickling of the module sources for no gain.
        for i, rec in enumerate(ex.map(_safe_pair, jobs), 1):
            _tick(i, rec.get("function", ""))
            out.append(rec)
    _tick_done()
    return out, ""


# ---------------------------------------------------------------------------------------
# CONTROLS. Known-answer pairs whose verdict is fixed in advance, INCLUDING pairs that must
# come back `changed`. The address-keyed generator bug proved the comparison layer can be
# wrong in ways that look exactly like results, and only known answers catch that.
# ---------------------------------------------------------------------------------------

CONTROL_PAIRS = [
    ("pure refactor, same behaviour",
     "def f(a, b):\n    return a + b",
     "def f(a, b):\n    t = a\n    return t + b", "no_change"),
    # THIS CONTROL WAS WRONG AND THE TOOL WAS RIGHT. `a * 2 + b` and `b + 2 * a` are equal for
    # numbers and NOT for strings or lists: "x"*2+"y" is "xxy" while "y"+2*"x" is "yxx". The
    # adjudicator returned `changed` with a string witness. Kept as a `changed` control, because
    # catching non-commutativity that a human would wave through as a harmless reorder is
    # precisely the product working.
    ("reorder unsafe for strings, must be caught",
     "def f(a, b):\n    return a * 2 + b",
     "def f(a, b):\n    return b + 2 * a", "changed"),
    ("genuinely commutative reorder, same behaviour",
     "def f(a, b):\n    return (a + b) + 1",
     "def f(a, b):\n    return 1 + (b + a)", "no_change"),
    ("off-by-one, must be caught",
     "def f(n):\n    return n + 1",
     "def f(n):\n    return n + 2", "changed"),
    ("comparison flipped, must be caught",
     "def f(n):\n    return 1 if n >= 10 else 0",
     "def f(n):\n    return 1 if n > 10 else 0", "changed"),
    ("exception type changed, must be caught",
     "def f(n):\n    if n < 0:\n        raise ValueError('neg')\n    return n",
     "def f(n):\n    if n < 0:\n        raise TypeError('neg')\n    return n", "changed"),
    ("generator, equivalent, must NOT be a false diff",
     "def f(n):\n    for i in range(n):\n        yield i * 2",
     "def f(n):\n    for i in range(n):\n        yield i + i", "no_change"),
    ("generator, genuinely different",
     "def f(n):\n    for i in range(n):\n        yield i * 2",
     "def f(n):\n    for i in range(n):\n        yield i * 3", "changed"),
    ("arity changed",
     "def f(a):\n    return a",
     "def f(a, b):\n    return a", "changed"),
    ("nondeterministic, must abstain",
     "def f(n):\n    return n",
     "def f(n):\n    xs = []\n    for _ in range(3):\n        xs.append(object())\n"
     "    return id(xs[0]) % 97", "abstained"),
]


def run_controls(budget=200, stream=None):
    """Known-answer pairs. `stream` exists so the banner can leave stdout alone.

    THE JSON CONTRACT IS THE INTEGRATION SURFACE, and this banner was printed to stdout ahead of
    it, so `runboth adjudicate --json` emitted something no parser accepts. Every machine consumer
    of the documented contract was broken, and it went unnoticed because the human report looks
    fine and nobody had piped the JSON into anything until now. Found by trying to do exactly
    that, which is the only way this class of bug is ever found.
    """
    import sys as _s
    out = stream if stream is not None else _s.stdout
    print("  CONTROLS: known-answer pairs. Half MUST come back `changed`, or the suite is\n"
          "  measuring nothing. The generator pair is here because comparing by repr keyed on\n"
          "  the object ADDRESS, which made every generator look changed.\n", file=out)
    ok = True
    for label, b, a, expected in CONTROL_PAIRS:
        bn = ast.parse(b).body[0]
        an = ast.parse(a).body[0]
        got = adjudicate_pair("control::f", bn, an, budget)
        good = got["verdict"] == expected
        ok = ok and good
        print(f"  {'PASS' if good else 'FAIL'}  {label:<44} expected {expected:<10} "
              f"got {got['verdict']}", file=out)
        if not good:
            print(f"        reason: {(got.get('reason') or '')[:80]}", file=out)
    print(file=out)
    return ok


def main():
    ap = argparse.ArgumentParser(description="Adjudicate behaviour change between two revisions.")
    ap.add_argument("repo", nargs="?", help="path to a git repository")
    ap.add_argument("base", nargs="?", help="base revision")
    ap.add_argument("head", nargs="?", help="head revision")
    ap.add_argument("--budget", type=int, default=400)
    ap.add_argument("--json", action="store_true", help="emit the contract, nothing else")
    ap.add_argument("--controls", action="store_true", help="run the known-answer suite and exit")
    args = ap.parse_args()

    if args.controls or not args.repo:
        sys.exit(0 if run_controls() else 1)

    if not run_controls(200):
        print("  CONTROLS FAILED. Refusing to adjudicate: the comparison layer is not trustworthy.")
        sys.exit(1)

    records, err = adjudicate(args.repo, args.base, args.head, args.budget)
    if err:
        print(f"  {err}")
        return
    if args.json:
        print(json.dumps(records, indent=2))
        return

    piles = {"no_change": [], "changed": [], "abstained": []}
    for r in records:
        piles[r["verdict"]].append(r)

    print(f"  {args.repo}  {args.base}..{args.head}   budget {args.budget}\n")
    print(f"  NO BEHAVIOUR CHANGE   {len(piles['no_change']):>4} functions   "
          f"no difference found in {args.budget} inputs")
    print(f"  BEHAVIOUR CHANGED     {len(piles['changed']):>4} functions   with a witness input")
    print(f"  COULD NOT DETERMINE   {len(piles['abstained']):>4} functions   with a reason\n")

    for r in piles["changed"][:20]:
        w = r["witness"]
        print(f"  CHANGED   {r['function']}")
        if w:
            print(f"            at {', '.join(w['args'])}:  {w['before']}  ->  {w['after']}")
        else:
            print(f"            {r['reason']}")
    if piles["abstained"]:
        print()
        for r in piles["abstained"][:12]:
            print(f"  ABSTAIN   {r['function']}\n            {r['reason'][:90]}")

    print("\n  `NO BEHAVIOUR CHANGE` means no difference was found in the budget above. It does")
    print("  NOT mean the functions are equivalent: measured false-same is 12% at 20 inputs and")
    print("  4% at 400. The abstention count is printed first on purpose, because a tool that")
    print("  hides what it could not check is worse than one that checks nothing.")


if __name__ == "__main__":
    main()
