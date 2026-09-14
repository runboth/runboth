"""
runboth: one entry point. Everything else in this tree is a library or an experiment.

    runboth adjudicate   REPO BASE HEAD          did this change behaviour, and where
    runboth merge-check  REPO BASE BR_A BR_B     is there a defect that only exists in the merge
    runboth classes      ENTRY FILE...           partition N implementations by behaviour
    runboth hook         [REPO]                  post-edit check for an agent harness
    runboth install-hook [REPO]                  git pre-commit gate: agents cannot skip it
    runboth versions   PKG OLD NEW              what actually changed between two releases
    runboth audit        REPO                    drift audit across recent commits
    runboth selftest                             every control suite, exit non-zero on failure

Every command takes --json and emits the same vendor-neutral contract, so a CLI, an MCP server,
a GitHub Action and a REST endpoint are all thin wrappers on this and no single integration
partner's decision costs more than one wrapper.

# The one rule the whole surface enforces

It never reports "same". It reports `no_change at budget N`, because measured false-same is 12% at
20 inputs and 4% at 400. And a thing that could not be checked is `abstained` WITH ITS REASON,
never a pass. Six defects in this project's own history presented as success; the surface is shaped
so that cannot happen to a user.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

BANNER = "runboth: behaviour-level adjudication for AI-written code"


def cmd_adjudicate(args):
    import sys as _s
    from adjudicate import adjudicate, run_controls
    # UNDER --json, STDOUT BELONGS TO THE CONTRACT and nothing else may touch it. The controls
    # still RUN and still refuse the whole command if they fail; only their narration moves to
    # stderr, so a pipeline gets parseable output and a human watching still sees the suite.
    if not run_controls(200, stream=_s.stderr if args.json else None):
        print("  CONTROLS FAILED. Refusing to adjudicate: the comparison layer is not trustworthy.",
              file=_s.stderr if args.json else _s.stdout)
        return 1
    # Progress narrates to stderr in both modes, so --json still owns stdout exactly.
    records, err = adjudicate(args.repo, args.base, args.head, args.budget, progress=_s.stderr)
    if err:
        if args.json:
            # Even the error path must not put prose on stdout: a consumer that gets `[]` and a
            # message on stderr can act, one that gets prose where JSON was promised cannot.
            print(json.dumps([]))
            print(f"  {err}", file=_s.stderr)
        else:
            print(f"  {err}")
        return 0
    if args.json:
        print(json.dumps(records, indent=2))
        return 0
    piles = {"no_change": [], "changed": [], "abstained": []}
    for r in records:
        piles[r["verdict"]].append(r)
    print(f"\n  {args.repo}  {args.base}..{args.head}   budget {args.budget}\n")
    print(f"  NO BEHAVIOUR CHANGE   {len(piles['no_change']):>4}   "
          f"no difference found in {args.budget} inputs")
    print(f"  BEHAVIOUR CHANGED     {len(piles['changed']):>4}   with a witness input")
    print(f"  COULD NOT DETERMINE   {len(piles['abstained']):>4}   with a reason\n")
    for r in piles["changed"][:25]:
        w = r["witness"]
        print(f"  CHANGED   {r['function']}")
        print(f"            at {', '.join(w['args'])}:  {w['before']}  ->  {w['after']}"
              if w else f"            {r['reason']}")
    for r in piles["abstained"][:15]:
        print(f"  ABSTAIN   {r['function']}\n            {(r['reason'] or '')[:90]}")
    return 0


def cmd_merge_check(args):
    from mergecheck import merge_check, report
    rows, notes = merge_check(args.repo, args.base, args.branch_a, args.branch_b, args.budget)
    report(rows, notes, args.json)
    return 0


def cmd_classes(args):
    from engine import equivalence_classes, load_module
    fns, names = [], []
    for i, f in enumerate(args.files):
        mod = load_module(f, f"cand{i}")
        fn = getattr(mod, args.entry, None)
        if fn is None:
            print(f"  {f}: no function named {args.entry}")
            return 1
        fns.append(fn)
        names.append(Path(f).name)
    classes = equivalence_classes(fns, names, trials=args.budget)
    if args.json:
        print(json.dumps([[n for n in c] if isinstance(c, list) else c for c in classes],
                         indent=2, default=str))
        return 0
    print(f"\n  {len(args.files)} texts to review, {len(classes)} BEHAVIOURS to review\n")
    for i, c in enumerate(classes, 1):
        members = c if isinstance(c, list) else getattr(c, "members", [c])
        print(f"  BEHAVIOUR {i}  ({len(members)} candidate{'s' if len(members) != 1 else ''})"
              f"   {', '.join(map(str, members))}")
    return 0


def cmd_audit(args):
    """The drift audit: adjudicate a range of commits and write a client-ready report."""
    from audit import cmd_audit as _run
    return _run(args)


def cmd_hook(args):
    """The post-edit hook. Silent when nothing changed; exit 1 when behaviour did."""
    from hook import report, run
    code, notes = run(args.repo, args.budget, not args.no_downstream)
    report(notes, args.budget)
    return 0 if args.quiet_exit else code


def cmd_blast(args):
    """Which of the repository's functions actually behave differently after this edit."""
    from pathlib import Path as _P
    from blast import blast_from_edit, render
    rel = str(args.file).replace("\\", "/")
    pre = str(_P(args.repo).resolve()).replace("\\", "/") + "/"
    if rel.startswith(pre):
        rel = rel[len(pre):]
    hits, stats, meta, err = blast_from_edit(args.repo, rel, args.budget, args.deadline)
    if err:
        print(f"  {err}")
        return 0
    print(f"  edited: {', '.join(meta.get('edited', [])) or '(none)'}")
    for d in meta.get("direct", []):
        w = d.get("witness")
        if w:
            print(f"    {d['function'].split('::', 1)[-1]}   at {', '.join(w['args'])}:  "
                  f"{w['before']} -> {w['after']}")
    if meta.get("seeds"):
        print(f"  witness values carried into the walk: {meta['seeds']}")
    print()
    print(render(hits, stats))
    return 0


def cmd_install(args):
    """Both enforcement points at once. They catch different things and neither subsumes the other.

    The runtime hook reports the blast radius seconds after an edit, while the agent can still
    act on it, and never blocks. The git gate blocks a behaviour change at the commit, where a
    human is present and the change is finished. Installing only one leaves a real hole: the
    first can be ignored, and the second arrives after the context that would fix it is gone.
    """
    from pathlib import Path as _P
    from install_agent import install_claude_hook, uninstall_claude_hook
    from install_hook import install
    py_dir = _P(__file__).resolve().parent
    scope = "user" if args.user else "project"

    if args.uninstall:
        removed = uninstall_claude_hook(args.repo, scope)
        print(f"  runtime hook: {'removed' if removed else 'not installed'}")
        import subprocess as _sp
        r = _sp.run(["git", "-C", args.repo, "rev-parse", "--git-dir"],
                    capture_output=True, text=True)
        h = _P(args.repo) / r.stdout.strip() / "hooks" / "pre-commit"
        if h.exists() and "runboth" in h.read_text(encoding="utf-8", errors="ignore"):
            h.unlink()
            print("  git gate:     removed")
        else:
            print("  git gate:     not installed")
        return 0

    st, note = install_claude_hook(args.repo, py_dir, scope)
    print(f"  runtime hook  {st or 'SKIPPED'}   {note}")
    target, gnote = install(args.repo, py_dir)
    print(f"  git gate      {target or 'SKIPPED'}   {gnote}")
    if target is None:
        return 1
    print()
    print("  Every EDIT in this repository now reports which functions you did not touch")
    print("  changed anyway, and which callers elsewhere moved with them. Every COMMIT is")
    print("  blocked if behaviour changed, with the input that proves it.")
    print()
    print("  Neither requires an agent to remember runboth exists.")
    print("  Bypass a commit with --no-verify. Skip everything with RUNBOTH_SKIP=1.")
    return 0


def cmd_install_hook(args):
    """Make it unforgettable. The enforcement point is git, not the agent."""
    from install_hook import install
    from pathlib import Path as _P
    if args.uninstall:
        import subprocess as _sp
        r = _sp.run(["git", "-C", args.repo, "rev-parse", "--git-dir"],
                    capture_output=True, text=True)
        h = _P(args.repo) / r.stdout.strip() / "hooks" / "pre-commit"
        if h.exists() and "runboth" in h.read_text(encoding="utf-8", errors="ignore"):
            h.unlink()
            print(f"  removed {h}")
        else:
            print("  no runboth hook installed")
        return 0
    target, note = install(args.repo, _P(__file__).resolve().parent)
    if target is None:
        print(f"  {note}")
        return 1
    print(f"  installed {target}")
    if note:
        print(f"  {note}")
    print()
    print("  Every commit is now adjudicated, by any agent or human, whether or not they")
    print("  know runboth exists. Blocks only on a behaviour CHANGE, prints the witness, and")
    print("  never blocks on an abstention or on silence.")
    print()
    print("  Bypass: git commit --no-verify   |   Skip once: RUNBOTH_SKIP=1 git commit")
    return 0


def cmd_versions(args):
    """Delegates to versions.main so there is one implementation and one set of flags."""
    import versions
    argv = [args.package, args.old, args.new,
            "--budget", str(args.budget), "--workers", str(args.workers)]
    if args.limit:
        argv += ["--limit", str(args.limit)]
    if args.json_out:
        argv += ["--json", args.json_out]
    old_argv = sys.argv
    sys.argv = ["versions"] + argv
    try:
        return versions.main()
    finally:
        sys.argv = old_argv


def cmd_selftest(_args):
    """Every control suite in the project. This is what `production` means here.

    A suite that only demonstrates passing is not a suite, so each of these contains cases that
    MUST fail and MUST abstain, and any of them going quiet is itself the alarm.
    """
    ok = True
    print(f"\n  {BANNER}\n  SELFTEST: every control suite, including the ones that must fail\n")

    from determinism import run_controls as det_controls
    print("  [1/6] determinism gate")
    ok &= bool(det_controls())

    from adjudicate import run_controls as adj_controls
    print("  [2/6] comparison layer, known-answer pairs")
    ok &= bool(adj_controls(200))

    from interagent import run_controls as ia_controls
    print("  [3/6] inter-agent detection")
    ok &= bool(ia_controls(200))

    from sandbox import run_controls as sbx_controls
    print("  [4/6] sandbox: each case MUST be stopped")
    ok &= bool(sbx_controls())

    from methods import run_controls as meth_controls
    print("  [5/6] methods and classes")
    ok &= bool(meth_controls(40))

    from control_version_stub import run_controls as vs_controls
    print("  [6/6] generated _version.py stub")
    ok &= bool(vs_controls())

    print(f"\n  SELFTEST: {'ALL SUITES PASS' if ok else 'FAILURE'}")
    if not ok:
        print("  Do not trust any output from this build.")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(prog="runboth", description=BANNER)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("adjudicate", help="did this change behaviour, and where")
    a.add_argument("repo"); a.add_argument("base"); a.add_argument("head")
    a.add_argument("--budget", type=int, default=400)
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_adjudicate)

    m = sub.add_parser("merge-check", help="a defect that exists only in the merge")
    m.add_argument("repo"); m.add_argument("base")
    m.add_argument("branch_a"); m.add_argument("branch_b")
    m.add_argument("--budget", type=int, default=300)
    m.add_argument("--json", action="store_true")
    m.set_defaults(fn=cmd_merge_check)

    c = sub.add_parser("classes", help="partition N implementations by behaviour")
    c.add_argument("entry"); c.add_argument("files", nargs="+")
    c.add_argument("--budget", type=int, default=400)
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_classes)

    h = sub.add_parser("hook", help="post-edit check for an agent harness")
    h.add_argument("repo", nargs="?", default=".")
    h.add_argument("--budget", type=int, default=80)
    h.add_argument("--no-downstream", action="store_true")
    h.add_argument("--quiet-exit", action="store_true")
    h.set_defaults(fn=cmd_hook)

    ih = sub.add_parser("install-hook", help="install the git pre-commit behaviour gate")
    ih.add_argument("repo", nargs="?", default=".")
    ih.add_argument("--uninstall", action="store_true")
    ih.set_defaults(fn=cmd_install_hook)

    bl = sub.add_parser("blast", help="system-wide impact of an edit, by executing the callers")
    bl.add_argument("file", help="the edited file")
    bl.add_argument("--repo", default=".")
    bl.add_argument("--budget", type=int, default=60)
    bl.add_argument("--deadline", type=float, default=45)
    bl.set_defaults(fn=cmd_blast)

    ia = sub.add_parser("install", help="both gates: runtime edit hook AND git pre-commit")
    ia.add_argument("repo", nargs="?", default=".")
    ia.add_argument("--user", action="store_true",
                    help="register the runtime hook for every repo, not just this one")
    ia.add_argument("--uninstall", action="store_true")
    ia.set_defaults(fn=cmd_install)

    # No repository, no git, no source access: two version numbers and a package name. This is
    # the question a consumer has when a dependency bot opens a pull request, and the one nobody
    # can answer today, because a version bump's diff is one line in a lockfile and a changelog
    # is written from memory by the person least able to notice what they changed by accident.
    v = sub.add_parser("versions", help="what actually changed between two released versions")
    v.add_argument("package")
    v.add_argument("old")
    v.add_argument("new")
    v.add_argument("--budget", type=int, default=60)
    v.add_argument("--workers", type=int, default=4)
    v.add_argument("--limit", type=int, default=None)
    v.add_argument("--json", dest="json_out", default=None)
    v.set_defaults(fn=cmd_versions)

    au = sub.add_parser("audit", help="drift audit across recent commits, as a report")
    au.add_argument("repo")
    au.add_argument("--commits", type=int, default=30, help="how many recent commits to examine")
    au.add_argument("--budget", type=int, default=60, help="generated inputs per function")
    au.add_argument("--branch", default=None)
    au.add_argument("--out", default=None, help="write the Markdown report here")
    au.add_argument("--json", default=None, help="write the raw findings here")
    au.set_defaults(fn=cmd_audit)

    s = sub.add_parser("selftest", help="run every control suite")
    s.set_defaults(fn=cmd_selftest)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
