"""Scan real history for the one thing no diff reader can find.

    python scripts/scan_blast.py <repo> [<repo> ...] --commits 40 --budget 60

The hunt for commits whose MESSAGE claims a refactor searches a tiny, well-guarded slice of
history: a handful of commits per repository, in projects with review and CI. Finding nothing
there says more about those projects than about this tool.

This looks somewhere else entirely. For every commit, it asks which functions changed behaviour
**whose own source did not change a character**. A reviewer reading that pull request sees
nothing to review, because there is no diff on the function that moved. The only way to see it
is to execute the callers, which is what the blast radius does.

Every hit is a LEAD, not a finding. Promotion rules, unchanged from the other hunts:

  * the function's own source is byte-identical across the commit
  * something it calls really did change
  * the witness reproduces by hand, outside the tool
  * the inputs are ones a caller could plausibly pass

Severity ordering matters more here than in the other scans, because the interesting case is not
"something moved" but "something moved silently". A value that became a different value outranks
anything that started or stopped raising.
"""
import argparse
import ast
import collections
import json
import subprocess
import sys
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "runboth"
sys.path.insert(0, str(ENGINE))


def git(repo, *a, timeout=120):
    try:
        r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return r.stdout if r.returncode == 0 else None


def funcs_at(repo, sha, path):
    src = git(repo, "show", f"{sha}:{path}")
    if src is None:
        return {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    out = {}

    def walk(node, stack):
        for n in getattr(node, "body", []):
            if isinstance(n, ast.ClassDef):
                walk(n, stack + [n.name])
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                try:
                    out[".".join(stack + [n.name])] = ast.unparse(n)
                except Exception:
                    pass
    walk(tree, [])
    return out


def severity(w):
    """0 is worst: a silently different answer."""
    b, a = str(w.get("before", "")), str(w.get("after", ""))
    b_exc = b.startswith("['exc") or b.startswith("['ctor")
    a_exc = a.startswith("['exc") or a.startswith("['ctor")
    if not b_exc and not a_exc:
        return 0
    if b_exc != a_exc:
        return 1
    return 2


SEV = {0: "SILENT WRONG ANSWER", 1: "started/stopped raising", 2: "exception type changed"}


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


def scan_repo(repo, commits, budget, timeout):
    shas = (git(repo, "log", "--no-merges", "--format=%H", "-n", str(commits), "--", "*.py") or "").split()
    leads, seen = [], 0
    for sha in shas:
        base = (git(repo, "rev-parse", f"{sha}^") or "").strip()
        if not base:
            continue
        recs, el = adjudicate(repo, base, sha, budget, timeout)
        if recs is None:
            continue
        seen += 1
        changed = [r for r in recs if r["verdict"] == "changed" and r.get("witness")]
        if not changed:
            continue

        cache, hits = {}, []
        for r in changed:
            path, _, short = r["function"].partition("::")
            if not path.endswith(".py"):
                continue
            for key, rev in ((("b", path), base), (("a", path), sha)):
                if key not in cache:
                    cache[key] = funcs_at(repo, rev, path)
            b, a = cache[("b", path)].get(short), cache[("a", path)].get(short)
            if b is None or a is None or b != a:
                continue          # added, removed, or genuinely edited: a reviewer can see it
            hits.append({
                "function": r["function"], "witness": r["witness"],
                "severity": severity(r["witness"]),
                "rung": r.get("rung"),
            })

        if hits:
            hits.sort(key=lambda h: h["severity"])
            leads.append({
                "repo": Path(repo).name, "sha": sha, "base": base,
                "subject": (git(repo, "log", "-1", "--format=%s", sha) or "").strip()[:90],
                "date": (git(repo, "log", "-1", "--format=%ci", sha) or "").strip()[:10],
                "seconds": round(el, 1), "hits": hits,
            })
            worst = SEV[hits[0]["severity"]]
            print(f"  {Path(repo).name}/{sha[:9]} {el:6.1f}s  {len(hits)} unchanged-source "
                  f"finding(s), worst: {worst}", flush=True)
    return leads, seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repos", nargs="+")
    ap.add_argument("--commits", type=int, default=40)
    ap.add_argument("--budget", type=int, default=60)
    ap.add_argument("--timeout", type=int, default=400)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    all_leads, total = [], 0
    for repo in a.repos:
        p = Path(repo).resolve()
        if not (p / ".git").exists():
            continue
        print(f"=== {p.name} ({a.commits} commits) ===", flush=True)
        leads, seen = scan_repo(p, a.commits, a.budget, a.timeout)
        total += seen
        all_leads.extend(leads)

    print(f"\nadjudicated {total} commits across {len(a.repos)} repos, "
          f"{len(all_leads)} carry a source-unchanged finding\n")

    by_sev = collections.Counter(h["severity"] for L in all_leads for h in L["hits"])
    for s in sorted(by_sev):
        print(f"  {by_sev[s]:>4}  {SEV[s]}")
    print()

    for L in sorted(all_leads, key=lambda L: L["hits"][0]["severity"]):
        print("=" * 78)
        print(f"{L['repo']}  {L['sha'][:12]}  {L['date']}  {L['subject']}")
        for h in L["hits"][:4]:
            w = h["witness"]
            print(f"  [{SEV[h['severity']]}]  {h['function']}   ({h['rung']})")
            print(f"    args:   {w.get('args')}")
            print(f"    before: {str(w.get('before'))[:140]}")
            print(f"    after:  {str(w.get('after'))[:140]}")
        print()

    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(all_leads, indent=2), encoding="utf-8")
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
