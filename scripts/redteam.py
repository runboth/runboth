"""Red-team RunBoth against real repositories and real commits.

    python scripts/redteam.py <repo> --commits 12 --budget 60 --out results/rt_<name>.json

This is not a demo script. It is built to make the tool look BAD if it deserves to, and its
central idea is an automated false-positive oracle:

    For every function RunBoth reports as `changed`, compare that function's own source at base
    and at head. If the source is identical, the verdict is only legitimate when something the
    function CALLS changed (that is the blast radius, the whole point of the product). A
    `changed` verdict on a function whose source is identical AND whose callees are all
    unchanged is a probable lie, and lies are the one failure this product cannot survive.

That oracle is what caught the measurement-path defect on 2026-09-12, by hand, on a single
commit. Here it runs over hundreds of functions without supervision.

Also recorded, because a tool nobody keeps installed has no value either:
  * wall-clock per commit, which decides whether a commit gate survives a week
  * the abstention rate and the REASONS, grouped, which is the honest coverage number
  * crashes and non-zero exits, separately from abstentions
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


def git(repo, *a):
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,
                       errors="replace")
    return r.stdout if r.returncode == 0 else None


def pick_commits(repo, n):
    """Recent non-merge commits that touch Python files, newest first.

    Merges are excluded because `commit^` is ambiguous for them and a merge's diff against its
    first parent attributes the whole side branch to one commit.
    """
    out = git(repo, "log", "--no-merges", "--format=%H", "-n", str(n * 4), "--", "*.py")
    if not out:
        return []
    shas = out.split()
    keep = []
    for s in shas:
        # Needs a parent to compare against, and a diff that is not enormous.
        if git(repo, "rev-parse", f"{s}^") is None:
            continue
        stat = git(repo, "diff", "--shortstat", f"{s}^", s, "--", "*.py") or ""
        if "file" not in stat:
            continue
        keep.append(s)
        if len(keep) >= n:
            break
    return keep


def functions_at(repo, sha, path):
    """{short_name: unparsed source} for one file at one commit. Empty on any failure."""
    src = git(repo, "show", f"{sha}:{path}")
    if src is None:
        return {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    out = {}

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def _fn(self, node):
            name = ".".join(self.stack + [node.name])
            try:
                out[name] = ast.unparse(node)
            except Exception:
                pass
            # do NOT descend into nested functions; their names would collide

        visit_FunctionDef = _fn
        visit_AsyncFunctionDef = _fn

    V().visit(tree)
    return out


def called_names(source):
    """Every bare name this function calls. Deliberately crude: over-collecting makes the
    oracle CONSERVATIVE (fewer false accusations of a false positive), which is the safe
    direction for a tool whose job is to accuse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def adjudicate(repo, base, head, budget, timeout):
    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, str(ENGINE / "cli.py"), "adjudicate", str(repo), base, head,
             "--budget", str(budget), "--json"],
            capture_output=True, text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        return None, time.time() - t0, "TIMEOUT", ""
    el = time.time() - t0
    if r.returncode not in (0, 1):
        return None, el, f"exit {r.returncode}", r.stderr[-1500:]
    try:
        return json.loads(r.stdout or "[]"), el, None, r.stderr[-1500:]
    except json.JSONDecodeError:
        return None, el, "unparseable json", (r.stdout[:400] + r.stderr[-1200:])


def audit_commit(repo, sha, budget, timeout):
    base = git(repo, "rev-parse", f"{sha}^").strip()
    recs, elapsed, err, stderr = adjudicate(repo, base, sha, budget, timeout)
    row = {"sha": sha[:10], "seconds": round(elapsed, 1), "error": err,
           "subject": (git(repo, "log", "-1", "--format=%s", sha) or "").strip()[:90]}
    if recs is None:
        row["stderr_tail"] = stderr[-400:]
        return row

    verdicts = collections.Counter(r["verdict"] for r in recs)
    row.update(total=len(recs), changed=verdicts["changed"],
               no_change=verdicts["no_change"], abstained=verdicts["abstained"])
    # Group on the whole reason, lightly normalised. An earlier version cut at the first colon,
    # which collapsed every distinct cause into the single useless bucket "before" and hid that
    # 82 of 87 sqlparse abstentions were one thing: the wall-clock kill.
    def norm(reason):
        r = (reason or "?").strip()
        for noisy in ("before: ", "after: ", "before version ", "after version "):
            if r.startswith(noisy):
                r = r[len(noisy):]
        return r.split("(")[0].strip()[:70]

    row["abstain_reasons"] = collections.Counter(
        norm(r.get("reason")) for r in recs if r["verdict"] == "abstained").most_common(8)

    # ---- the oracle ----
    changed_short = {r["function"].split("::")[-1].split(".")[-1]
                     for r in recs if r["verdict"] == "changed"}
    # A class whose __init__ changed explains every one of its METHODS, because construction is
    # an implicit call that no syntactic callee scan can see. Without this the oracle accuses the
    # tool of lying every time a constructor gains a validation, which it measurably does.
    ctor_changed_classes = {
        r["function"].split("::")[-1].rsplit(".", 1)[0]
        for r in recs
        if r["verdict"] == "changed" and r["function"].endswith(".__init__")
    }

    def ctor_caused(rec):
        """The AFTER side is nothing but a failed construction, so the method never ran."""
        w = rec.get("witness") or {}
        return str(w.get("after", "")).startswith("['ctor'")

    row["changed_records"] = [
        {"function": r["function"], "witness": r.get("witness"),
         "ctor_caused": ctor_caused(r)}
        for r in recs if r["verdict"] == "changed"
    ]
    row["ctor_caused_changes"] = sum(1 for r in recs
                                     if r["verdict"] == "changed" and ctor_caused(r))

    suspects = []
    src_cache = {}
    for r in recs:
        if r["verdict"] != "changed":
            continue
        qname = r["function"]
        path, _, short = qname.partition("::")
        if not path.endswith(".py"):
            continue
        key_b, key_a = (base, path), (sha, path)
        if key_b not in src_cache:
            src_cache[key_b] = functions_at(repo, base, path)
        if key_a not in src_cache:
            src_cache[key_a] = functions_at(repo, sha, path)
        before = src_cache[key_b].get(short)
        after = src_cache[key_a].get(short)
        if before is None or after is None:
            continue                      # added or removed: legitimately `changed`
        if before != after:
            continue                      # source moved: legitimately `changed`
        # Source identical. A changed CALLEE is the only honest explanation.
        callees = called_names(after)
        if callees & (changed_short - {short.split(".")[-1]}):
            continue                      # blast radius, working as designed
        if "." in short and short.rsplit(".", 1)[0] in ctor_changed_classes:
            continue                      # its own constructor changed under it
        if ctor_caused(r):
            continue                      # reported because construction now fails, not a lie
        suspects.append({
            "function": qname,
            "witness": r.get("witness"),
            "reason": r.get("reason"),
        })
    row["suspect_false_positives"] = suspects
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--commits", type=int, default=10)
    ap.add_argument("--budget", type=int, default=60)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    shas = pick_commits(repo, a.commits)
    print(f"{repo.name}: {len(shas)} commits, budget {a.budget}", flush=True)

    rows = []
    for i, sha in enumerate(shas, 1):
        row = audit_commit(repo, sha, a.budget, a.timeout)
        rows.append(row)
        flag = ""
        if row.get("error"):
            flag = f"  !! {row['error']}"
        elif row.get("suspect_false_positives"):
            flag = f"  !! {len(row['suspect_false_positives'])} SUSPECT"
        print(f"  [{i}/{len(shas)}] {row['sha']} {row['seconds']:>6}s "
              f"chg={row.get('changed', '-')} same={row.get('no_change', '-')} "
              f"abs={row.get('abstained', '-')}{flag}", flush=True)

    ok = [r for r in rows if not r.get("error")]
    tot = sum(r.get("total", 0) for r in ok)
    abst = sum(r.get("abstained", 0) for r in ok)
    susp = sum(len(r.get("suspect_false_positives", [])) for r in ok)
    chg = sum(r.get("changed", 0) for r in ok)
    summary = {
        "repo": repo.name,
        "commits_attempted": len(rows),
        "commits_completed": len(ok),
        "errors": [r for r in rows if r.get("error")],
        "functions_adjudicated": tot,
        "abstained": abst,
        "abstention_rate": round(abst / tot * 100, 1) if tot else None,
        "changed_total": chg,
        "ctor_caused_changes": sum(r.get("ctor_caused_changes", 0) for r in ok),
        "suspect_false_positives": susp,
        "median_seconds": sorted(r["seconds"] for r in ok)[len(ok) // 2] if ok else None,
        "max_seconds": max((r["seconds"] for r in ok), default=None),
    }
    print("\n" + json.dumps(summary, indent=2))

    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps({"summary": summary, "commits": rows}, indent=2),
                               encoding="utf-8")
        print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
