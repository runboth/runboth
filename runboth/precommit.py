"""
The pre-commit gate itself. Adjudicates STAGED changes and blocks only on a behaviour change.

Separate from `hook.py` because the question is different. `hook.py` compares the working tree
against HEAD, which is what an agent wants after an edit. A commit gate must compare what is
actually being COMMITTED, which is the index, and those differ the moment someone stages a subset
of their changes.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from adjudicate import (  # noqa: E402
    adjudicate_pair, call_text, functions_in, git, materialise,
)


_INTENT_UNIT_RE = __import__("re").compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_NO_CHANGE_RE = __import__("re").compile(
    r"no behaviou?r change|behaviou?r[- ]preserv|preserves? behaviou?r"
    r"|pure refactor|refactor only|behaviou?r (is )?unchanged|same behaviou?r",
    __import__("re").I,
)


def declared_in_message(msg, changed_names):
    """Which changed functions did the commit message SAY it was changing?

    Returns a set of short names, or None when the message makes no claim either way.

    THE SINGLE BIGGEST REASON SOMEONE UNINSTALLS THIS is that it blocks a change they meant to
    make. A real commit observed in the wild deliberately rewrote a generated install script,
    said so in its subject line, and was blocked anyway. A gate that argues with you about work
    you did on purpose gets removed the same afternoon.

    Three rules, mirroring `uvc.core.declared_units` deliberately rather than importing it,
    because the engine does not depend on the ledger package and should not start here:

      "Intent: name, name"   an explicit list. Those are declared, nothing else is.
      "behaviour unchanged"  a claim of NO change. Declares the EMPTY set, so every measured
                             difference is undeclared and blocks. This is the lie the tool
                             exists to catch and it must stay caught.
      otherwise              a function is implicitly declared if its own bare name appears as
                             a whole word in the message. Lower precision on purpose; it never
                             fires when no changed function is named, in which case the message
                             simply says nothing and the gate behaves as it always did.

    Order matters: the explicit list wins over a no-change phrase, because a message saying
    "pure refactor. Intent: clamp" is declaring clamp and claiming nothing about the rest.
    """
    if not msg:
        return None
    body = "\n".join(l for l in msg.splitlines() if not l.lstrip().startswith("#"))
    low = body.lower()
    if "intent:" in low:
        tail = body[low.index("intent:") + len("intent:"):]
        named = set(_INTENT_UNIT_RE.findall(tail))
        return {n.rsplit(".", 1)[-1] for n in named}
    if _NO_CHANGE_RE.search(body):
        return set()
    import re as _re
    found = {n for n in changed_names
             if len(n) >= 3 and _re.search(r"\b" + _re.escape(n) + r"\b", body)}
    return found or None


def _staged_py(repo):
    out = git(repo, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "--", "*.py")
    return [l.strip() for l in (out or "").splitlines() if l.strip().endswith(".py")]


def staged_files(repo):
    """Staged Python files that are PRODUCT, so tests and task runners are not adjudicated.

    A commit gate freezing the terminal is fatal for adoption, and this was measured doing exactly
    that: three of eight more-itertools commits spent over 700 seconds and produced nothing, and
    every one of them touched only `tests/test_more.py`. Staging test changes is the most ordinary
    thing a developer does between two real commits.

    What is skipped is NAMED by `unchecked_staged`, never silently dropped, because silence is how
    this gate says "no difference found". `RUNBOTH_ALL_PATHS=1` adjudicates everything.
    """
    from adjudicate import is_noise_path
    files = _staged_py(repo)
    if os.environ.get("RUNBOTH_ALL_PATHS") == "1":
        return files
    return [f for f in files if not is_noise_path(f)]


# Extensions that are SOURCE, so a change to one can change behaviour, but which this gate
# cannot adjudicate at function level. Data, docs and lockfiles are deliberately absent: nobody
# expects those to be executed, and listing them would make the notice noise.
_UNCHECKED = {
    ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".c": "C", ".h": "C", ".cc": "C++",
    ".cpp": "C++", ".hpp": "C++", ".swift": "Swift", ".scala": "Scala", ".ex": "Elixir",
    ".exs": "Elixir", ".sh": "Shell", ".bash": "Shell", ".ps1": "PowerShell", ".sql": "SQL",
}


def unchecked_staged(repo):
    """Staged source files in languages this gate cannot check. Returns {language: [paths]}.

    WITHOUT THIS THE GATE LIES BY OMISSION, and it is the same failure this project already
    names at the function level, one level up. Measured 2026-09-12 on a repo holding Python,
    TypeScript and Go: an off-by-one (`>=` to `>`) was introduced in BOTH the TypeScript and
    the Go file, and the gate stayed silent and let the commit through, because it only ever
    looked at `*.py`. Silence is how this tool says "no difference found". A developer who
    installed it and watched it pass would reasonably conclude they were covered.

    So silence is now reserved for what was actually checked, and anything else is named. It
    does not block: not being able to check something has never been grounds to stop a commit
    here, and starting now would make the gate unusable in every polyglot repository.
    """
    from collections import defaultdict
    out = git(repo, "diff", "--cached", "--name-only", "--diff-filter=ACMR")
    groups = defaultdict(list)
    for line in (out or "").splitlines():
        p = line.strip()
        for ext, lang in _UNCHECKED.items():
            if p.lower().endswith(ext):
                groups[lang].append(p)
                break
    # Python files that were filtered out as tests, benchmarks, docs or task runners belong in the
    # SAME notice, for the same reason: the gate did not look at them, so its silence must not be
    # read as covering them.
    kept = set(staged_files(repo))
    for p in _staged_py(repo):
        if p not in kept:
            groups["Python (tests, benchmarks, docs or build files)"].append(p)
    return dict(groups)


def print_unchecked(groups):
    if not groups:
        return
    n = sum(len(v) for v in groups.values())
    langs = ", ".join(sorted(groups))
    print(f"\n  NOTE: {n} changed file(s) were NOT checked ({langs}).")
    print("  Nothing above says anything about them.")
    for lang in sorted(groups):
        shown = ", ".join(groups[lang][:3])
        more = f" (+{len(groups[lang]) - 3} more)" if len(groups[lang]) > 3 else ""
        print(f"    {lang}: {shown}{more}")
    print()


def staged_source(repo, path):
    """The file exactly as it will be committed, which is the index and not the worktree."""
    return git(repo, "show", f":{path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--budget", type=int, default=80)
    ap.add_argument("--message-file", default=None,
                    help="the commit message file, as `commit-msg` receives it in $1. "
                         "NOT readable from a pre-commit hook: .git/COMMIT_EDITMSG exists "
                         "there but still holds the PREVIOUS commit's message, verified "
                         "2026-09-12, so reading it from pre-commit would silently reconcile "
                         "against the wrong text.")
    args = ap.parse_args()
    message = ""
    if args.message_file:
        try:
            message = Path(args.message_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            message = ""
    repo = args.repo

    files = staged_files(repo)
    skipped = unchecked_staged(repo)
    if not files:
        # Even with no Python staged the notice matters: a commit of pure TypeScript through a
        # repo with this gate installed used to be indistinguishable from a commit that passed.
        print_unchecked(skipped)
        return 0

    head_root = materialise(repo, "HEAD")
    if head_root is None:
        print_unchecked(skipped)
        return 0                                    # first commit: nothing to compare against

    # The staged content has to exist on disk as a tree for imports to resolve, so the index is
    # materialised too. `git checkout-index` writes exactly what will be committed.
    staged_root = tempfile.mkdtemp(prefix="runboth_staged_")
    subprocess.run(["git", "-C", str(repo), "checkout-index", "-a", "-f",
                    f"--prefix={staged_root}/"], capture_output=True)

    changed, abstained, reduced = [], 0, []
    edited_names, seed_values = set(), []
    # THE GATE IS SYNCHRONOUS: a developer typed `git commit` and is watching a cursor, so
    # latency here is not a performance nicety, it is whether the tool survives the week.
    #
    # Measured 2026-09-12 on a real repo (quire, numerics/safety.py, FIVE functions): 54
    # seconds, serial. The cost is not the comparison, it is that the file imports numpy and
    # scipy and every sandbox subprocess pays that import again. Three spawns per function
    # (before, after, determinism re-check) times five functions is fifteen interpreter starts
    # each dragging scipy in behind it.
    #
    # `adjudicate()` has run these through a thread pool for months; the gate never did. The
    # work is almost entirely WAITING on subprocesses, so the GIL is released throughout and
    # threads are the right tool. On this 20-core machine that is most of the difference.
    jobs = []
    for path in files:
        before_src = git(repo, "show", f"HEAD:{path}")
        after_src = staged_source(repo, path)
        if before_src is None or after_src is None:
            continue
        before = functions_in(before_src, path)
        after = functions_in(after_src, path)
        for q in sorted(set(before) & set(after)):
            jobs.append((q, before[q], after[q], before_src, after_src))

    def _one(j):
        q, bnode, anode, bsrc, asrc = j
        try:
            return q, adjudicate_pair(q, bnode, anode, args.budget,
                                      bsrc, asrc, head_root, staged_root)
        except Exception as e:  # noqa: BLE001
            return q, {"function": q, "verdict": "abstained", "budget": args.budget,
                       "witness": None,
                       "reason": f"internal error: {type(e).__name__}: {str(e)[:100]}"}

    # A GATE THAT SAYS NOTHING IS INDISTINGUISHABLE FROM A HUNG ONE, and this file already
    # records that freezing the terminal is what kills adoption. stderr only: git shows it to
    # the developer, and nothing reading stdout sees a change.
    def _tick(i, _q):
        # COUNT ONLY, NEVER THE NAME. In this gate a named function is a changed function,
        # and tests/test_measurement_artifacts.py asserts exactly that by checking a name
        # never appears anywhere in the output. Progress must prove liveness without
        # borrowing the meaning that naming carries.
        try:
            sys.stderr.write("\r  runboth: checking %d/%d" % (i, len(jobs)))
            sys.stderr.flush()
        except Exception:  # noqa: BLE001  a broken pipe must never block a commit
            pass

    def _tick_done():
        try:
            sys.stderr.write("\r" + " " * 40 + "\r")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass

    if len(jobs) <= 1:
        results = [_one(j) for j in jobs]
    else:
        import concurrent.futures as _cf
        # NOT cpu_count(). Each job spawns interpreters that re-import the repo's dependencies,
        # so the scarce resource is memory bandwidth and page cache, not cores. Measured on
        # quire (numpy + scipy): serial 54s, twenty workers 96s. Oversubscribing made it WORSE
        # by a third. Small and tunable.
        workers = max(1, min(len(jobs), int(os.environ.get("RUNBOTH_WORKERS", "4"))))
        with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
            results = []
            for _i, _r in enumerate(ex.map(_one, jobs), 1):
                _tick(_i, _r[0])
                results.append(_r)
    if jobs:
        _tick_done()

    for q, rec in results:
        if True:
            if rec["verdict"] == "changed":
                changed.append(rec)
                edited_names.add(q.split("::", 1)[-1].split(".")[-1])
                w = rec.get("witness")
                if w:
                    for a in w.get("args", []):
                        try:
                            seed_values.append(__import__("ast").literal_eval(a))
                        except (ValueError, SyntaxError):
                            pass
            elif rec["verdict"] == "abstained":
                abstained += 1
            # A function too slow for the full budget is re-run on a slice, so its verdict is
            # real but thinner. The summary line below used to print the REQUESTED budget for
            # every function, which would now be false for these. Counted here so the footer
            # can say so instead of quietly overstating what was checked.
            if rec.get("budget") and rec["budget"] < args.budget:
                reduced.append((q.split("::", 1)[-1], rec["budget"]))

    def _compact(before, after, width=88):
        """When the values are big strings, show WHAT MOVED, not both blobs.

        Found on a real repository, 2026-09-12: a function that builds an install script
        changed, and the gate printed two two-thousand-character shell scripts one after the
        other. Every word of that was true and none of it was usable; a reader cannot diff two
        walls of text by eye, which is the exact job this tool exists to do for them.

        Multi-line values are reduced to the lines that differ. Long single-line values are
        reduced to the neighbourhood of the first difference. Short values are untouched.
        """
        def _unwrap(k):
            # The engine's key is `['val', "<repr of the value>"]`, so the value is TWO layers
            # in. Diffing the outer form printed `-['val', "'set +e...` with the escaping
            # doubled, which is worse than useless: it shows the reader our internals instead
            # of their string. Peel both layers, and fall back to the raw text if either peel
            # fails rather than guessing.
            import ast
            s = str(k)
            try:
                v = ast.literal_eval(s) if s.startswith("[") else s
            except (ValueError, SyntaxError):
                return s
            if isinstance(v, list) and len(v) > 1 and v[0] == "val":
                inner = v[1]
                try:
                    return str(ast.literal_eval(inner))
                except (ValueError, SyntaxError):
                    return str(inner)
            return s

        b, a = _unwrap(before), _unwrap(after)
        if len(b) <= width and len(a) <= width:
            return None
        if "\n" in b or "\n" in a:
            import difflib
            bl = b.splitlines()
            al = a.splitlines()
            out, shown = [], 0
            for line in difflib.unified_diff(bl, al, lineterm="", n=0):
                if line.startswith(("---", "+++", "@@")):
                    continue
                out.append(f"        {line[:width]}")
                shown += 1
                if shown >= 6:
                    out.append("        ... (more)")
                    break
            if out:
                return ["      the text it builds changed:"] + out
        # Single long line: show the first place they diverge, with context either side.
        i = 0
        while i < min(len(b), len(a)) and b[i] == a[i]:
            i += 1
        lo = max(0, i - 24)
        return ["      first difference at character %d:" % i,
                f"        was:  ...{b[lo:i + 40]}...",
                f"        now:  ...{a[lo:i + 40]}..."]

    def _english(key):
        """Turn an outcome key into something a person reads at a glance.

        The engine's keys are shaped for comparison, not for humans: `['exc', 'TypeError']`,
        `['lazy', [...]]`, `['eff', ...]`. A developer interrupted mid-commit should not have to
        decode them. Anything unrecognised falls through unchanged rather than being guessed at.
        """
        s = str(key)
        try:
            import ast as _ast
            v = _ast.literal_eval(s) if s.startswith("[") else s
        except (ValueError, SyntaxError):
            v = s
        if isinstance(v, list) and v:
            tag = v[0]
            if tag == "exc":
                return f"raise {v[1]}"
            if tag == "ctor":
                return f"fail to build the object ({v[1]})"
            if tag == "val":
                return f"return {v[1]}"
            if tag == "lazy":
                return "return a lazy sequence (contents differ)"
            if tag == "eff":
                return "have a different side effect (output, warning, or object state)"
            if tag == "ctx":
                return "return a different context manager"
        # The engine sometimes hands back prose it already conjugated ("raised TypeError").
        # Prepending "return" to that produced "return raised ValueError". Normalise instead:
        # anything that already opens with a verb is rewritten to the present tense and used
        # as-is, and only a bare value gets "return" put in front of it.
        text = str(v).strip()
        verbs = {"raised": "raise", "raises": "raise", "returned": "return",
                 "returns": "return", "yielded": "yield", "yields": "yield"}
        head, _, rest = text.partition(" ")
        if head.lower() in verbs:
            return f"{verbs[head.lower()]} {rest}".strip()
        return f"return {text}"

    if not changed:
        # SILENCE IS THE NORMAL CASE and it must stay silent. A gate that prints on every commit
        # is a gate that gets uninstalled. Abstentions are not a problem and never block: a
        # function that could not be checked is not evidence of anything.
        #
        # The one thing silence must NOT cover is a language this gate never looked at, because
        # then silence means "nothing to report" and "I did not look" at the same time.
        print_unchecked(skipped)
        return 0

    # THE BLAST RADIUS BELONGS IN THE GATE, and for a month it was not in it.
    #
    # "It checks the functions nobody touched" is the headline claim on the landing page and the
    # one capability with no equivalent anywhere. It lived in `blast.py`, wired into the runtime
    # hook and the MCP server, and NOT into the pre-commit gate, which is the surface the whole
    # pitch rests on because it needs no vendor cooperation. Measured 2026-09-12: changing
    # `rate()` in one file while `invoice.total()` called it from another, the gate reported
    # `rate` and never mentioned `total`, whose behaviour genuinely moved at units=100. Callers
    # in the SAME file were already caught (every function in a touched file gets executed), so
    # the hole was exactly the cross-file case, which is the compelling one.
    #
    # Seeded with the witnesses already in hand, because knowing the callee differs at 100 is
    # worth more to its callers than any number of blind draws. Deadline-bounded and reported as
    # a lower bound when it truncates: a gate that hangs is a gate that gets uninstalled.
    downstream = []
    try:
        from blast import blast_radius
        hits, bstats = blast_radius(repo, edited_names, head_root, staged_root,
                                    budget=max(20, args.budget // 2),
                                    deadline=float(os.environ.get("RUNBOTH_BLAST_DEADLINE", "20")),
                                    seed_values=seed_values[:8])
        seen_here = {r["function"] for r in changed}
        downstream = [h for h in hits if h.get("function") not in seen_here]
    except Exception:
        # The blast walk is an ENRICHMENT. If it cannot run, the verdict on the edited
        # functions still stands and the commit decision is unchanged.
        bstats = None

    # THE OUTPUT IS THE PRODUCT. Whoever reads this has ten seconds and did not ask for a
    # lecture: they typed `git commit` and got interrupted. So it leads with the consequence in
    # plain words, shows the call the way they would write it themselves, and only then gets
    # to the budget and the escape hatch. The previous version printed the raw verdict shape
    # ("at 100:  100  ->  99") which is accurate and makes a reader work out for themselves why
    # they should care. Rewritten 2026-09-12 because the output has to show the
    # pain, not report a measurement.
    # ORDER BY WHAT IT COSTS THE READER, NOT BY WHAT THE WALK HAPPENED TO FIND FIRST.
    #
    # A live run on real agent output (2026-09-12, a billing module, DeepSeek asked to "handle
    # edge cases more gracefully") returned four findings in walk order. The first was
    # `apply_credit(-2, '%s must be a finite number')`, TypeError becoming ValueError on an
    # input nobody would ever pass. The third was `line_total(-6, 2)` going from -12.0 to a
    # raise, which in billing code means every refund line now crashes. The one that mattered
    # was buried under the one that did not, and a reader who stops after the first entry
    # concludes the tool is pedantic. Ranking is by the SHAPE of the difference, which is
    # knowable without guessing at intent:
    #
    #   a value became a DIFFERENT value    silently wrong answers, the worst outcome
    #   a value became an exception         it used to work and now crashes
    #   an exception became a value         it used to reject this and now accepts it
    #   an exception became a DIFFERENT one  real, but callers rarely branch on the type
    #   same value, different type          real, and usually cosmetic (0 vs 0.0)
    def _rank(r):
        w = r.get("witness")
        if not w:
            return 3
        b, a = str(w.get("before", "")), str(w.get("after", ""))
        # A RENAMED LOCAL FUNCTION IS NOT A BEHAVIOUR CHANGE WORTH BLOCKING. Found on flask's
        # own history: renaming an inner helper changed the repr of the closure stored in
        # `deferred_functions`, and three Blueprint methods were reported as changed. True, and
        # useless. Same treatment as `0` versus `0.0`: reported, never blocking.
        from sandbox import differs_only_by_a_function_name
        if differs_only_by_a_function_name(b, a):
            return 4
        b_exc, a_exc = "exc" in b[:6] or b.startswith("raise"), "exc" in a[:6] or a.startswith("raise")
        if not b_exc and not a_exc:
            # Same printed value with a different type is the cosmetic case (0 versus 0.0).
            # SIGNED ZERO belongs there too: 0.0 == -0.0 is True in Python, so a caller
            # comparing values sees nothing. It is still REPORTED rather than dropped, because
            # "-0.00" printed on an invoice is a real bug in exactly this kind of code, and
            # deciding it is harmless is the caller's call to make, not ours.
            def _canon(s):
                return s.lstrip("-").replace(".0", "") if s.lstrip("-").replace(".0", "") in ("0", "") else s
            if b.replace(".0", "") == a.replace(".0", "") or _canon(b) == _canon(a) == "0":
                return 4
            return 0
        if not b_exc and a_exc:
            return 1
        if b_exc and not a_exc:
            return 1
        return 3

    # ONE ROOT CAUSE, ONE FINDING, on the gate too. A constructor that gains a validation makes
    # every method of the class unbuildable, and a gate that prints 45 blocked findings for a
    # two-line commit gets uninstalled the same afternoon. Same helper the PR report uses, so the
    # two surfaces cannot drift. See adjudicate.collapse_constructor_findings.
    from adjudicate import collapse_constructor_findings, constructor_rollup_line
    changed, _ctor_rollup = collapse_constructor_findings(changed)

    changed.sort(key=_rank)

    # COSMETIC DIFFERENCES ARE REPORTED BUT DO NOT BLOCK, and the line between the two is
    # drawn where the CALLER can see it: `0 == 0.0` is True and `0.0 == -0.0` is True, so a
    # caller comparing results sees nothing, while the type or the sign did move.
    #
    # This is the same rule the gate already applies to abstentions, for the same reason. A
    # live demo run caught it: a clean, genuinely equivalent agent refactor was blocked solely
    # because `round(0, 2)` returns int `0` and the rewrite returned `0.0`. Blocking a commit
    # for that is how a gate gets uninstalled on its first afternoon, and the project's own
    # rules already say so. Reporting it is still right: `-0.00` printed on an invoice is a
    # real bug in exactly this kind of code. So it prints, and the commit proceeds.
    blocking = [r for r in changed if _rank(r) < 4]
    cosmetic = [r for r in changed if _rank(r) >= 4]

    def _show(r):
        w = r.get("witness")
        name = r["function"].split("::", 1)[-1]
        if w:
            print(f"    {call_text(name, w['args'])}")
            small = _compact(w["before"], w["after"])
            if small:
                for line in small:
                    print(line)
            else:
                print(f"      used to:  {_english(w['before'])}")
                print(f"      now:      {_english(w['after'])}")
        else:
            print(f"    {name}")
            print(f"      {r.get('reason')}")
        print()

    if not blocking:
        print("\n  Committed, with one note." if len(cosmetic) == 1
              else f"\n  Committed, with {len(cosmetic)} notes.")
        print("  The value a caller sees is unchanged; the type or sign moved.\n")
        for r in cosmetic[:6]:
            _show(r)
        print_unchecked(skipped)
        return 0

    # DECLARED CHANGES REPORT BUT DO NOT BLOCK. Reconciled against the real commit message,
    # which is why the gate installs as `commit-msg` rather than `pre-commit`.
    declared = declared_in_message(message, [r["function"].split("::", 1)[-1].split(".")[-1]
                                             for r in blocking])
    if declared:
        kept, waved = [], []
        for r in blocking:
            short = r["function"].split("::", 1)[-1].split(".")[-1]
            (waved if short in declared else kept).append(r)
        if waved and not kept:
            print(f"\n  Committed. {len(waved)} declared change(s) measured as described:\n")
            for r in waved[:6]:
                _show(r)
            if downstream:
                # THE MOST USEFUL SENTENCE THIS TOOL CAN PRODUCE lives on this path, not on the
                # blocked one: you meant to change that function, and here is what else moved
                # because of it. Dropping the blast radius just because the edit was declared
                # would throw away the finding nothing else can produce, on the exact commits a
                # developer is most confident about.
                print(f"  Worth knowing: {len(downstream)} function(s) you did NOT touch moved")
                print("  as a consequence. Not blocking, because you declared the cause.\n")
                for h in downstream[:6]:
                    w = h.get("witness") or {}
                    name = str(h.get("function", "")).split("::", 1)[-1]
                    print(f"    {call_text(name, w.get('args', []))}   {h.get('file', '')}")
                    if w:
                        print(f"      used to:  {_english(w.get('before'))}")
                        print(f"      now:      {_english(w.get('after'))}")
                    print()
            else:
                print("  Nothing undeclared changed.\n")
            print_unchecked(skipped)
            return 0
        if waved:
            print(f"\n  ({len(waved)} declared change(s) matched the message and are not "
                  f"blocking.)")
        blocking = kept

    changed = blocking
    n = len(changed)
    # An EMPTY declaration is not the same as no declaration. `declared_in_message` returns the
    # empty set when the message actively claims nothing changed ("pure refactor", "behaviour
    # unchanged"), and None when it makes no claim at all. Both block, but only one of them is
    # a contradiction the author will want to see named, and it is the case this whole tool
    # exists for.
    if declared == set():
        print("\n  BLOCKED: your commit message says the behaviour did not change.")
        print("  It did.\n")
    else:
        print(f"\n  BLOCKED: this commit changes what your code does.\n")
    for r in changed[:12]:
        _show(r)
        rolled = constructor_rollup_line(_ctor_rollup, r.get("function", ""))
        if rolled:
            print(f"    {rolled}\n")
    if downstream:
        print(f"  AND {len(downstream)} function(s) you did NOT touch now behave differently,")
        print("  because they call what you changed:\n")
        for h in downstream[:6]:
            w = h.get("witness") or {}
            where = h.get("file", "")
            name = str(h.get("function", "")).split("::", 1)[-1]
            print(f"    {call_text(name, w.get('args', []))}   {where}")
            if w:
                print(f"      used to:  {_english(w.get('before'))}")
                print(f"      now:      {_english(w.get('after'))}")
            print()
        if bstats and bstats.get("truncated"):
            print("    (walk hit its deadline, so this is a lower bound)\n")

    subject = "That call is" if n == 1 else "Those calls are"
    print(f"  {subject} the proof. Anything relying on the old result behaves")
    print("  differently now, and your test suite did not stop this commit.\n")
    if cosmetic:
        print(f"  ({len(cosmetic)} further difference(s) are type- or sign-only and never block.)")
    if abstained:
        print(f"  ({abstained} other function(s) could not be checked. Those never block.)")
    if reduced:
        names = ", ".join(f"{n} ({b})" for n, b in reduced[:3])
        more = f" +{len(reduced) - 3} more" if len(reduced) > 3 else ""
        print(f"  Checked {args.budget} inputs per function, except these, which were too slow")
        print(f"  and were checked on fewer: {names}{more}.")
    else:
        print(f"  Checked {args.budget} inputs per function. Evidence, not proof.")
    print("  Meant to change it?  git commit --no-verify")
    print_unchecked(skipped)
    return 1


if __name__ == "__main__":
    sys.exit(main())
