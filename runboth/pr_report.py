"""Turn an adjudication into a pull-request comment, and post it.

    python pr_report.py --repo . --base <sha> --head <sha> [--post] [--fail-on-change]

Prints Markdown on stdout always. Posts a STICKY comment (edits its own previous one instead of
adding another) when --post is given and GITHUB_TOKEN plus GITHUB_REPOSITORY plus a PR number
are in the environment, which is what the Action supplies.

# Two rules this file exists to hold

A PR bot that comments on every pull request gets muted in a week, so **silence is the default**:
no findings, no comment, and any comment it left previously is edited down to a single quiet
line rather than deleted, because a vanishing comment reads as a crash.

And it does not fail the build unless asked. A check that blocks a merge on `sampled(80)`
evidence would be overclaiming, and the first false stop gets the whole thing uninstalled.
`--fail-on-change` is opt-in for teams that want it.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKER = "<!-- runboth-report -->"

# The engine is a flat directory whose modules import each other by bare name, so every entry
# point puts its own directory on the path. This file runs the adjudication as a SUBPROCESS (so a
# crash in repository code cannot take the reporter down), but it still shares the report-shaping
# rules, and one implementation of those beats two that drift.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from adjudicate import (  # noqa: E402
    call_text, changed_python_files, collapse_constructor_findings, constructor_rollup_line,
)


def english(key):
    s = str(key)
    try:
        import ast
        v = ast.literal_eval(s) if s.startswith("[") else s
    except (ValueError, SyntaxError):
        v = s
    if isinstance(v, list) and v:
        tag = v[0]
        if tag == "exc":
            return f"raise `{v[1]}`"
        if tag == "val":
            return f"return `{v[1]}`"
        if tag == "ctor":
            return f"fail to construct (`{v[1]}`)"
        if tag == "lazy":
            return "return a lazy sequence with different contents"
        if tag == "eff":
            return "have a different side effect"
        if tag == "ctx":
            return "return a different context manager"
    text = str(v).strip()
    verbs = {"raised": "raise", "returned": "return"}
    head, _, rest = text.partition(" ")
    if head.lower() in verbs:
        return f"{verbs[head.lower()]} `{rest}`"
    return f"return `{text}`"


def rank(r):
    """Worst first: a wrong value beats a crash beats a different exception type."""
    w = r.get("witness")
    if not w:
        return 3
    b, a = str(w.get("before", "")), str(w.get("after", ""))
    b_e = b.startswith("['exc") or b.startswith("raise")
    a_e = a.startswith("['exc") or a.startswith("raise")
    if not b_e and not a_e:
        return 4 if b.replace(".0", "").lstrip("-") == a.replace(".0", "").lstrip("-") else 0
    return 1 if b_e != a_e else 3


def adjudicate(repo, base, head, budget):
    r = subprocess.run([sys.executable, str(HERE / "cli.py"), "adjudicate", repo, base, head,
                        "--budget", str(budget), "--json"],
                       capture_output=True, text=True, timeout=3600)
    try:
        return json.loads(r.stdout or "[]"), r.stderr
    except json.JSONDecodeError:
        return [], r.stderr


def _skipped_line(skipped):
    shown = ", ".join(skipped[:3])
    more = f" and {len(skipped) - 3} more" if len(skipped) > 3 else ""
    if len(skipped) == 1:
        return (f"1 changed file was NOT checked because it is a test, benchmark or doc "
                f"({shown}); nothing above refers to it")
    return (f"{len(skipped)} changed files were NOT checked because they are tests, "
            f"benchmarks or docs ({shown}{more}); nothing above refers to them")


def skipped_paths(repo, base, head):
    """Python files that changed but were NOT adjudicated because they are tests or docs.

    SILENCE MUST NEVER COVER A SKIPPED FILE. That rule already exists one level up for languages
    the gate cannot read (`precommit.print_unchecked`); this is the same rule for paths. Without
    it, filtering out `tests/` would quietly shrink what the report is about while the report went
    on looking exactly as confident as before.
    """
    try:
        every = changed_python_files(repo, base, head, include_all=True)
        kept = set(changed_python_files(repo, base, head))
        return [f for f in every if f not in kept]
    except Exception:  # noqa: BLE001  never let a footnote break the report
        return []


def render(records, base, head, budget, skipped=()):
    # ONE ROOT CAUSE, ONE FINDING. A constructor that gains a validation makes every method of
    # the class unbuildable on the after side, and reporting each of those separately turns a
    # two-line commit into 45 findings. See adjudicate.collapse_constructor_findings.
    records, ctor_rollup = collapse_constructor_findings(records)

    changed = [r for r in records if r["verdict"] == "changed" and r.get("witness")]
    structural = [r for r in records if r["verdict"] == "changed" and not r.get("witness")]
    abst = [r for r in records if r["verdict"] == "abstained"]
    ok = [r for r in records if r["verdict"] == "no_change"]
    changed.sort(key=rank)
    real = [r for r in changed if rank(r) < 4]
    cosmetic = [r for r in changed if rank(r) >= 4]

    L = [MARKER, ""]
    if not real:
        L += [f"**RunBoth: no behaviour change found.** Executed {len(ok)} function(s) in both "
              f"versions at {budget} inputs each.", ""]
        if structural:
            # ADDED AND REMOVED FUNCTIONS USED TO VANISH HERE. This branch reported cosmetic
            # differences, abstentions and skipped files, but never the structural ones, so a
            # commit that added two functions and changed no behaviour said only "no behaviour
            # change found". True, and it omitted the most obvious fact about the commit.
            # Caught on sqlparse's ReDoS fix, which adds two helpers.
            names = ", ".join(sorted(r["function"].split("::")[-1] for r in structural)[:4])
            more = f" and {len(structural) - 4} more" if len(structural) > 4 else ""
            L.append(f"{len(structural)} function(s) were added or removed ({names}{more}); "
                     f"there is no before-version to compare them against.")
        if cosmetic:
            L.append(f"{len(cosmetic)} type- or sign-only difference(s) found; the value a "
                     f"caller sees is unchanged.")
        if abst:
            L.append(f"{len(abst)} function(s) could not be checked and are listed as abstained, "
                     f"not as passing.")
        if skipped:
            L.append(_skipped_line(skipped))
        L += ["", "<sub>Evidence, not proof. Sampled, never exhaustive.</sub>"]
        return "\n".join(L), False

    L += [f"### RunBoth found {len(real)} behaviour change"
          f"{'s' if len(real) != 1 else ''}", ""]
    for r in real[:15]:
        w = r["witness"]
        fn = r["function"]
        path, _, name = fn.partition("::")
        # For a constructor, "have a different side effect" is technically the `eff` channel and
        # tells a reader nothing. What actually happened is that the object used to be buildable.
        before_txt = english(w["before"]).replace("`", "")
        after_txt = english(w["after"]).replace("`", "")
        if str(w.get("after", "")).startswith("['ctor'") or name.endswith(".__init__"):
            if not str(w.get("before", "")).startswith("['ctor'"):
                before_txt = "construct successfully"
        L += [f"**`{name}`** &nbsp;<sub>{path}</sub>", "",
              f"```",
              f"{call_text(fn, w['args'])}",
              f"  used to:  {before_txt}",
              f"  now:      {after_txt}",
              f"```", ""]
        rolled = constructor_rollup_line(ctor_rollup, fn)
        if rolled:
            L += [f"<sub>{rolled}</sub>", ""]
    if len(real) > 15:
        L.append(f"…and {len(real) - 15} more.\n")

    tail = []
    if structural:
        tail.append(f"{len(structural)} function(s) added or removed")
    if cosmetic:
        tail.append(f"{len(cosmetic)} type- or sign-only difference(s), not counted above")
    if abst:
        tail.append(f"{len(abst)} could not be checked (abstained, never counted as passing)")
    if skipped:
        tail.append(_skipped_line(skipped))
    if tail:
        L += ["<sub>" + " · ".join(tail) + "</sub>", ""]
    L += [f"<sub>Executed both versions at {budget} generated inputs per function. "
          f"Evidence, not proof.</sub>"]
    return "\n".join(L), True


def post(body):
    tok = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr = os.environ.get("RUNBOTH_PR", "")
    if not (tok and repo and pr):
        print("  (not posting: GITHUB_TOKEN / GITHUB_REPOSITORY / RUNBOTH_PR not all set)",
              file=sys.stderr)
        return

    def api(path, method="GET", payload=None):
        req = urllib.request.Request(
            f"https://api.github.com{path}",
            data=json.dumps(payload).encode() if payload else None,
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                     "User-Agent": "runboth"}, method=method)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")

    try:
        mine = None
        for c in api(f"/repos/{repo}/issues/{pr}/comments?per_page=100"):
            if MARKER in (c.get("body") or ""):
                mine = c["id"]
                break
        if mine:
            api(f"/repos/{repo}/issues/comments/{mine}", "PATCH", {"body": body})
            print("  updated the existing comment", file=sys.stderr)
        else:
            api(f"/repos/{repo}/issues/{pr}/comments", "POST", {"body": body})
            print("  posted a comment", file=sys.stderr)
    except urllib.error.HTTPError as e:
        print(f"  could not post ({e.code}): {e.read().decode()[:180]}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--budget", type=int, default=60)
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--fail-on-change", action="store_true")
    a = ap.parse_args()

    records, err = adjudicate(a.repo, a.base, a.head, a.budget)
    if err.strip():
        print(err.strip()[-800:], file=sys.stderr)
    body, found = render(records, a.base, a.head, a.budget,
                         skipped=skipped_paths(a.repo, a.base, a.head))
    print(body)

    if a.post and (found or os.environ.get("RUNBOTH_ALWAYS_COMMENT")):
        post(body)
    elif a.post:
        # Quiet is the default, but an EXISTING comment must be updated rather than left
        # asserting a finding that a later push already fixed.
        post(body)

    return 1 if (found and a.fail_on_change) else 0


if __name__ == "__main__":
    sys.exit(main())
