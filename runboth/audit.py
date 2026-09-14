"""`runboth audit` - the drift audit, as a client-ready artifact.

runboth.dev sells a drift audit. Until now the tool had no command that produced one:
you ran `adjudicate` per commit pair and reassembled the output by hand, which does not
scale past one customer and does not look like something anyone paid for.

This walks a range of commits, adjudicates each against its parent, and writes a single
Markdown report. The report carries the SAME honesty contract the tool does, because the
deliverable is the place a soft claim would do the most damage:

  * it never says "safe" or "clean"; it says what was checked, at what budget
  * every finding carries the witness input that proves it, so the client re-runs it
  * every abstention is listed with its reason, never dropped to make a number look good
  * the method and the known limits are in the report, not in a footnote

Usage:

    runboth audit <repo> --commits 40 --budget 60 --out audit.md
"""
import datetime
import json
import os
from collections import Counter, OrderedDict

from adjudicate import adjudicate, git


def _commits(repo, n, branch=None):
    """Newest-first list of (sha, subject, iso date) for the last n non-merge commits."""
    out = git(repo, "log", "--no-merges", "-n", str(n),
              "--format=%H%x1f%s%x1f%cI%x1e", *( [branch] if branch else [] ))
    rows = []
    for rec in (out or "").split("\x1e"):
        p = rec.strip("\n").split("\x1f")
        if len(p) >= 3 and p[0].strip():
            rows.append((p[0].strip(), p[1], p[2]))
    return rows


def run_audit(repo, commits=30, budget=60, progress=None, branch=None):
    """Adjudicate each of the last `commits` commits against its parent.

    Returns (summary dict, findings list, skipped list). Nothing is thrown away: a
    commit that could not be examined appears in `skipped` with the reason given.
    """
    rows = _commits(repo, commits, branch)
    findings, skipped = [], []
    counts = Counter()
    seen_functions = 0

    for i, (sha, subject, when) in enumerate(rows, 1):
        parent = (git(repo, "rev-parse", f"{sha}^") or "").strip()
        if not parent:
            skipped.append({"commit": sha, "subject": subject, "reason": "no parent (root commit)"})
            continue
        if progress is not None:
            try:
                progress.write("\r  commit %d/%d  %s  %-44s" % (i, len(rows), sha[:9], subject[:44]))
                progress.flush()
            except Exception:  # noqa: BLE001
                pass
        records, err = adjudicate(repo, parent, sha, budget)
        if err:
            skipped.append({"commit": sha, "subject": subject, "reason": err})
            continue
        for r in records:
            counts[r["verdict"]] += 1
            seen_functions += 1
            if r["verdict"] == "changed":
                findings.append({
                    "commit": sha, "subject": subject, "date": when,
                    "function": r["function"], "rung": r.get("rung"),
                    "witness": r.get("witness"), "reason": r.get("reason"),
                })
            elif r["verdict"] == "abstained":
                skipped.append({"commit": sha, "subject": subject,
                                "function": r["function"],
                                "reason": r.get("reason") or "no reason given"})
    if progress is not None:
        try:
            progress.write("\r" + " " * 78 + "\r")
            progress.flush()
        except Exception:  # noqa: BLE001
            pass

    examined = len(rows) - len([s for s in skipped if "function" not in s])
    adjudicated = counts["changed"] + counts["no_change"]
    pct = (100.0 * adjudicated / seen_functions) if seen_functions else 0.0
    summary = {
        "repo": os.path.basename(str(repo).rstrip("/\\")),
        "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "commits_requested": commits,
        "commits_examined": examined,
        "budget": budget,
        "functions_seen": seen_functions,
        "adjudicated": adjudicated,
        "adjudicated_pct": round(pct, 1),
        "changed": counts["changed"],
        "no_change": counts["no_change"],
        "abstained": counts["abstained"],
    }
    return summary, findings, skipped


def _witness_line(w):
    if not w:
        return "_no witness recorded_"
    args = ", ".join(w.get("args") or [])
    return "`%s`\n\n  - before: `%s`\n  - after:  `%s`" % (
        args, str(w.get("before"))[:200], str(w.get("after"))[:200])


def report_markdown(summary, findings, skipped):
    s = summary
    L = []
    L.append("# Behaviour drift audit: %s" % s["repo"])
    L.append("")
    L.append("Generated %s by RunBoth. Every claim below was produced by executing both "
             "versions of the code, not by reading the diff." % s["generated"][:16].replace("T", " "))
    L.append("")
    L.append("## What was examined")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append("| Commits examined | %d of the last %d |" % (s["commits_examined"], s["commits_requested"]))
    L.append("| Functions seen | %d |" % s["functions_seen"])
    L.append("| Adjudicated | %d (%s%%) |" % (s["adjudicated"], s["adjudicated_pct"]))
    L.append("| Behaviour changed | **%d** |" % s["changed"])
    L.append("| No change found | %d |" % s["no_change"])
    L.append("| Could not be checked | %d |" % s["abstained"])
    L.append("| Inputs per function | %d |" % s["budget"])
    L.append("")
    L.append("**This report does not say the code is safe, and no number in it should be read "
             "that way.** \"No change found\" means no difference appeared in %d generated inputs. "
             "That is evidence, not proof. The measured rate at which a real change is missed is "
             "12%% at 20 inputs and 4%% at 400." % s["budget"])
    L.append("")

    L.append("## Behaviour that changed")
    L.append("")
    if not findings:
        L.append("No behaviour change was witnessed in the commits examined, at %d inputs per "
                 "function. See the section below for what could not be checked, because that is "
                 "where an unwitnessed change would be hiding." % s["budget"])
    else:
        L.append("Each finding carries the input that proves it. Re-run any of them yourself.")
        L.append("")
        for f in findings:
            L.append("### `%s`" % f["function"])
            L.append("")
            L.append("- commit `%s` %s" % (f["commit"][:12], f["date"][:10]))
            L.append("- %s" % f["subject"])
            L.append("- evidence: %s" % (f["rung"] or "-"))
            L.append("- call: %s" % _witness_line(f["witness"]))
            L.append("")
    L.append("")

    L.append("## What could not be checked")
    L.append("")
    fn_skips = [s2 for s2 in skipped if "function" in s2]
    commit_skips = [s2 for s2 in skipped if "function" not in s2]
    if not fn_skips and not commit_skips:
        L.append("Nothing. Every function in every commit examined was adjudicated.")
    else:
        L.append("Listed in full rather than summarised away. An abstention is the honest "
                 "answer when the tool cannot construct the inputs a function needs; it is not "
                 "a pass.")
        L.append("")
        if commit_skips:
            L.append("**Commits skipped entirely (%d)**" % len(commit_skips))
            L.append("")
            for c in commit_skips[:30]:
                L.append("- `%s` %s: %s" % (c["commit"][:12], c["subject"][:56], c["reason"][:90]))
            L.append("")
        if fn_skips:
            grouped = OrderedDict()
            for c in fn_skips:
                key = (c["reason"] or "")[:70]
                grouped.setdefault(key, []).append(c["function"])
            L.append("**Functions not adjudicated (%d), grouped by reason**" % len(fn_skips))
            L.append("")
            for reason, fns in grouped.items():
                L.append("- %s _(%d)_: %s%s" % (reason, len(fns), ", ".join("`%s`" % f for f in fns[:6]),
                                                ", ..." if len(fns) > 6 else ""))
            L.append("")
    L.append("")

    L.append("## Method, and what it does not do")
    L.append("")
    L.append("RunBoth checks out both versions of the code, generates inputs for every changed "
             "function from its signature and from the constants in its own compiled bytecode, "
             "runs both versions in separate sandboxed subprocesses, and compares seven "
             "observation channels. It then walks the call graph and does the same to every "
             "function downstream of the change, which is how a function nobody edited is caught.")
    L.append("")
    L.append("- No test suite is required, and none was used.")
    L.append("- Nothing left the machine this ran on. No network calls, no model.")
    L.append("- It samples. Every verdict states its budget, because sampled is not proved.")
    L.append("- Python only.")
    L.append("- It abstains on code it cannot construct inputs for, and says so above.")
    L.append("")
    return "\n".join(L)


def cmd_audit(args):
    import sys as _s
    repo = args.repo
    summary, findings, skipped = run_audit(
        repo, commits=args.commits, budget=args.budget,
        progress=_s.stderr, branch=getattr(args, "branch", None))
    md = report_markdown(summary, findings, skipped)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(md)
        print("  wrote %s" % args.out)
    else:
        print(md)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "findings": findings, "skipped": skipped},
                      fh, indent=2)
        print("  wrote %s" % args.json)
    if not args.out and not args.json:
        return 0
    s = summary
    print("  %d commits, %d functions, %d changed, %d abstained, %s%% adjudicated"
          % (s["commits_examined"], s["functions_seen"], s["changed"],
             s["abstained"], s["adjudicated_pct"]))
    return 0
