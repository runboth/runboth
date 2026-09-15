"""Regression archaeology: find the commit that INTRODUCED a bug the project later fixed.

    python scripts/hunt_regressions.py <repo> --scan 600 --budget 80

The strongest honest claim this tool can make is not "a refactor changed something". It is:

    This commit introduced a behaviour change. The project shipped it, users hit it, and
    someone fixed it N commits later. RunBoth measures the change at the moment of the commit,
    from the code alone, with no test and no issue report.

That is checkable by anyone: the fix commit exists in the history, and so does the witness.

# How it works

  1. Find fix commits: messages that say they fix something, ideally with an issue number.
  2. Work out which functions the fix touched.
  3. For each such function, find the PREVIOUS commit that changed it. That is the suspect.
  4. Adjudicate the suspect. If RunBoth reports that function changed, the suspect is where
     the behaviour moved, and the fix is the project's own confirmation that it was wrong.

# What this cannot claim

That the measured change IS the bug. Two things can move in one commit, and a fix can be a
redesign rather than a revert. So every hit prints the fix commit alongside the suspect, and a
human reads both before anyone says the word "regression" out loud. The tool supplies the
witness and the dates; it does not supply the judgement.
"""
import argparse
import ast
import functools
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "runboth"
sys.path.insert(0, str(ENGINE))

FIX = re.compile(
    r"\bfix(?:e[sd])?\b|\bregression\b|\bbroke[n]?\b|\bbug\b|\bincorrect(?:ly)?\b"
    r"|\bwrong\b|\bno longer\b|\bstopped working\b|\bunexpected(?:ly)?\b",
    re.I)
ISSUE = re.compile(r"#\d{2,6}|\bGH-\d+|\bissue\s*\d+", re.I)

# A SUSPECT THAT ANNOUNCES ITSELF IS NOT A SILENT REGRESSION. The whole claim of this
# script is "this commit changed behaviour, the project shipped it, nobody noticed until a
# later fix". A commit whose own message says it is fixing, reverting or bugfixing is
# stating that it means to change behaviour, so detecting the change proves nothing.
#
# MEASURED 2026-09-14 on the first real run, four leads across three repositories, none
# reportable and three of them caught by exactly this:
#   werkzeug  Rule._parse_rule     suspect "Bugfix rewrite the rule parsing"
#   dateutil  _tzparser.parse      suspect "Revert b15f38a"
#   jsonschema is_date             suspect "fix: Python 3.11 date.fromisoformat() ..."
# In the jsonschema case the tool was right that behaviour changed, and the change was the
# author deliberately re-narrowing a validator after Python 3.11 widened fromisoformat.
# A hunt that surfaces that as a suspected regression is wasting the reader's attention,
# which is the only thing this tool is actually spending.
DECLARED = re.compile(
    r"\b(fix|fixes|fixed|fixing|bugfix|revert|reverts|reverted|"
    r"correct|corrects|workaround|hotfix)\b", re.I)

# ...EXCEPT WHEN THE PROJECT ITSELF SAYS REGRESSION. Fixes introduce regressions constantly,
# so "the suspect called itself a fix" cannot be a veto.
#
# This rule exists because the filter above was written on four noisy leads and would have
# thrown away the only real one in the same run. Python-Markdown:
#   suspect f925349 2026-01-21  "More HTML fixes"          <- DECLARED matches "fixes"
#   fix     c438647 2026-02-02  "Fix regression of special comments"  Fixes #1590, +26 tests
# `git log -S` confirms f925349 introduced the exact line c438647 replaced. A filter built on
# the noise would have suppressed the signal, which is the ordinary way a heuristic tuned on
# failures goes wrong. When the LATER commit uses the word regression, that is the project
# stating the earlier change was unintended, and it outranks anything the earlier message said.
REGRESSION = re.compile(r"\bregress(ion|ions|ed)?\b", re.I)


def git(repo, *a, timeout=120):
    try:
        r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return r.stdout if r.returncode == 0 else None


@functools.lru_cache(maxsize=256)
def _src_at(repo, sha, path):
    return git(repo, "show", f"{sha}:{path}")


@functools.lru_cache(maxsize=512)
def _funcs_at(repo, sha, path, only):
    """Cached, and when `only` is given it unparses ONE function instead of the file.

    previous_change walks up to 60 commits and asks about a single function at each
    one. Unparsing every function in the file to answer that is where the time went:
    measured 2026-09-14 on more-itertools, whose more.py is 5,633 lines, a single
    previous_change took 22.8s. Same call with `only` set and these caches: see the
    number in the module docstring.
    """
    src = _src_at(repo, sha, path)
    if src is None:
        return ()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ()
    out = []

    def walk(node, stack):
        for n in getattr(node, "body", []):
            if isinstance(n, ast.ClassDef):
                walk(n, stack + [n.name])
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = ".".join(stack + [n.name])
                if only is not None and name != only:
                    continue
                try:
                    out.append((name, ast.unparse(n)))
                except Exception:
                    pass
    walk(tree, [])
    return tuple(out)


def funcs_at(repo, sha, path, only=None):
    """{dotted name: normalised source}. Pass `only` when you want one function."""
    return dict(_funcs_at(repo, sha, path, only))


def functions_touched(repo, sha):
    """{path: [short names]} whose source actually moved in this commit."""
    base = (git(repo, "rev-parse", f"{sha}^") or "").strip()
    if not base:
        return {}
    files = [l.strip() for l in (git(repo, "diff", "--name-only", base, sha, "--", "*.py") or "").splitlines()
             if l.strip().endswith(".py")]
    touched = {}
    for p in files:
        if any(seg in p.lower().split("/") for seg in ("test", "tests", "docs", "benchmarks", "examples")):
            continue
        b, h = funcs_at(repo, base, p), funcs_at(repo, sha, p)
        moved = [k for k in set(b) & set(h) if b[k] != h[k]]
        if moved:
            touched[p] = moved
    return touched


@functools.lru_cache(maxsize=512)
def _span_at(repo, sha, path, short):
    """1-based (start, end) line range of `short` at `sha`, or None."""
    src = _src_at(repo, sha, path)
    if src is None:
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    want = short.split(".")
    found = []

    def walk(node, stack):
        for n in getattr(node, "body", []):
            if isinstance(n, ast.ClassDef):
                walk(n, stack + [n.name])
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if stack + [n.name] == want:
                    found.append((n.lineno, getattr(n, "end_lineno", n.lineno)))
    walk(tree, [])
    return found[0] if found else None


def _previous_change_walk(repo, sha, path, short, window=60):
    """Original implementation, kept as the fallback when -L cannot answer.

    Only sees `window` commits back ALONG THIS PATH, which is its defect: on a file
    that changes constantly that window does not reach the commit that actually moved
    the function, and it returns None rather than saying it ran out of room.
    """
    hist = (git(repo, "log", "--format=%H", f"{sha}^", "-n", str(window), "--", path) or "").split()
    for h in hist:
        parent = (git(repo, "rev-parse", f"{h}^") or "").strip()
        if not parent:
            return None
        b, a = funcs_at(repo, parent, path, short), funcs_at(repo, h, path, short)
        if short in b and short in a and b[short] != a[short]:
            return h
    return None


def previous_change(repo, sha, path, short):
    """The most recent commit BEFORE `sha` whose version of `path::short` differs from its parent.

    Asks git directly with `-L <start>,<end>:<path>`, which follows those lines back
    through history on its own, including across the rewrites that move them. Line
    numbers come from ast at `sha^`, so this never depends on git's funcname regex and
    needs no .gitattributes change in someone else's repository.

    MEASURED 2026-09-14, more-itertools (more.py, 5,633 lines, 65 fix commits in 400):
    the 60-commit walk returned None for all five functions tried; -L found a real
    commit for all five and was 25x faster (12.18s -> 0.49s). One of them, `only`, was
    changed by bc8c7aca "Simplify and speed-up only()", 211 commits back along that
    path, so the old window could never have reached it. Verified against humanize,
    where both agree on all three known answers.
    """
    sp = _span_at(repo, f"{sha}^", path, short)
    if sp:
        out = git(repo, "log", "--format=%H", "-s", "-n", "1",
                  f"-L{sp[0]},{sp[1]}:{path}", f"{sha}^")
        if out:
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            if lines:
                return lines[0]
    # -L could not answer: no such function at sha^, a rename, or a malformed range.
    return _previous_change_walk(repo, sha, path, short)


def adjudicate(repo, base, head, budget, timeout):
    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, str(ENGINE / "cli.py"), "adjudicate", str(repo), base, head,
             "--budget", str(budget), "--json"],
            capture_output=True, text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        return None, time.time() - t0
    try:
        return json.loads(r.stdout or "[]"), time.time() - t0
    except json.JSONDecodeError:
        return None, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--scan", type=int, default=500)
    ap.add_argument("--budget", type=int, default=80)
    ap.add_argument("--timeout", type=int, default=420)
    ap.add_argument("--max", type=int, default=25)
    ap.add_argument("--require-issue", action="store_true",
                    help="only fixes that cite an issue number, which are the ones users reported")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    log = git(repo, "log", "--no-merges", "-n", str(a.scan), "--format=%H%x1f%s%x1f%b%x1e", "--", "*.py") or ""
    fixes = []
    for rec in log.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) < 2 or not parts[0].strip():
            continue
        sha, subject = parts[0].strip(), parts[1]
        body = parts[2] if len(parts) > 2 else ""
        msg = subject + "\n" + body
        if not FIX.search(msg):
            continue
        if a.require_issue and not ISSUE.search(msg):
            continue
        fixes.append((sha, subject.strip()))

    print(f"{repo.name}: {len(fixes)} fix commits in the last {a.scan}", flush=True)

    leads, checked = [], 0
    timed_out = empty = declared = 0
    for fix_sha, fix_subject in fixes:
        if checked >= a.max:
            break
        touched = functions_touched(repo, fix_sha)
        if not touched:
            continue
        for path, shorts in list(touched.items())[:2]:
            for short in shorts[:2]:
                suspect = previous_change(repo, fix_sha, path, short)
                if not suspect:
                    continue
                s_msg = (git(repo, "log", "-1", "--format=%s%n%b", suspect) or "")
                fix_msg = (git(repo, "log", "-1", "--format=%s%n%b", fix_sha) or "")
                confirmed = bool(REGRESSION.search(fix_msg))
                if DECLARED.search(s_msg) and not confirmed:
                    declared += 1
                    continue
                sbase = (git(repo, "rev-parse", f"{suspect}^") or "").strip()
                if not sbase:
                    continue
                recs, el = adjudicate(repo, sbase, suspect, a.budget, a.timeout)
                checked += 1
                # SAY WHY, never just move on. This printed nothing for a suspect that
                # produced no records, so a run could report "adjudicated 10 suspects" while
                # nine of them timed out and the summary looked like coverage. Measured
                # 2026-09-14 on more-itertools: 10 checked, 1 line printed, 0 leads, and the
                # only completed adjudication took 64.9s against a 90s cap. A count that hides
                # its own failures is the thing this project exists to refuse.
                if recs is None:
                    timed_out += 1
                    print(f"  fix {fix_sha[:9]} -> suspect {suspect[:9]} {el:6.1f}s "
                          f"{path}::{short}  NO VERDICT: timed out at {a.timeout}s "
                          f"(raise --timeout)", flush=True)
                    continue
                if not recs:
                    empty += 1
                    print(f"  fix {fix_sha[:9]} -> suspect {suspect[:9]} {el:6.1f}s "
                          f"{path}::{short}  NO VERDICT: adjudicator returned no records",
                          flush=True)
                    continue
                # A CONSTRUCTOR ARTIFACT IS NOT A LEAD. When one side cannot build the object,
                # the method never ran there, so the witness is a statement about __init__
                # wearing the method's name. Two of the ten leads in the first real run were
                # this: dateutil `_ymd.resolve_ymd` and werkzeug `Rule._parse_rule`, both
                # costing a real investigation before the cause was visible.
                def _ctor_artifact(r):
                    w = r.get("witness") or {}
                    return (str(w.get("before", "")).startswith("['ctor'")
                            or str(w.get("after", "")).startswith("['ctor'"))

                hit = [r for r in recs
                       if r["verdict"] == "changed" and r.get("witness")
                       and not _ctor_artifact(r)
                       and r["function"].endswith(f"::{short}") and r["function"].startswith(path)]
                flag = "  <<< LEAD" if hit else ""
                print(f"  fix {fix_sha[:9]} -> suspect {suspect[:9]} {el:6.1f}s "
                      f"{path}::{short}{flag}", flush=True)
                if hit:
                    leads.append({
                        "fix": fix_sha, "fix_subject": fix_subject,
                        "fix_date": (git(repo, "log", "-1", "--format=%ci", fix_sha) or "").strip(),
                        "suspect": suspect,
                        "suspect_subject": (git(repo, "log", "-1", "--format=%s", suspect) or "").strip(),
                        "suspect_date": (git(repo, "log", "-1", "--format=%ci", suspect) or "").strip(),
                        "function": f"{path}::{short}",
                        "witness": hit[0]["witness"], "rung": hit[0].get("rung"),
                    })

    # The denominator that matters is what actually got a verdict, not what was attempted.
    verdicts = checked - timed_out - empty
    print(f"\n{checked} suspects attempted: {verdicts} got a verdict, {timed_out} timed out, "
          f"{empty} returned nothing. {len(leads)} leads.")
    print(f"  {declared} suspect(s) skipped before adjudication: the commit message declares a "
          f"fix or a revert, so a behaviour change there is intended, not a regression.\n")
    if timed_out:
        print(f"  {timed_out} of {checked} produced NO verdict at --timeout {a.timeout}s. "
              f"Those are unchecked, not clean.\n")
    for L in leads:
        print("=" * 78)
        print(f"FIX      {L['fix'][:12]}  {L['fix_date'][:10]}  {L['fix_subject'][:64]}")
        print(f"SUSPECT  {L['suspect'][:12]}  {L['suspect_date'][:10]}  {L['suspect_subject'][:64]}")
        print(f"  {L['function']}  [{L['rung']}]")
        w = L["witness"]
        print(f"    args:   {w.get('args')}")
        print(f"    before: {str(w.get('before'))[:160]}")
        print(f"    after:  {str(w.get('after'))[:160]}")
        print()

    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(leads, indent=2), encoding="utf-8")
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
