"""Run the regression hunt across several repositories, unattended.

    python scripts/hunt_overnight.py                 # clone + hunt the default set
    python scripts/hunt_overnight.py --work D:/hunt  # somewhere with space

Why a runner. hunt_regressions.py is correct but it is a long job, and it is very sensitive to
REPOSITORY SHAPE. funcs_at() parses whole files with ast at two revisions for every fix commit,
so a project that is one enormous module costs orders of magnitude more per fix than a project
of many small ones. Measured on this machine 2026-09-14: more-itertools (one 5,633-line module,
65 fix commits in 400) did not finish a SINGLE adjudication in 115 seconds. The red-team table
says the same thing from the other side: boltons 7.9s median, arrow 118s.

So the default targets below are all many-small-modules projects with active fix histories.
Add to them freely, but check the shape first:

    find <repo> -name '*.py' | xargs wc -l | sort -rn | head

If the largest module is over ~2,000 lines, expect it to be slow and give it its own run.

Results land in <work>/<name>.json and <work>/<name>.log. Leads are printed at the end of each
repo's log, and collected into <work>/LEADS.md for reading in the morning.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HUNT = Path(__file__).resolve().parent / "hunt_regressions.py"

# name, url. Chosen for many small modules, pure Python, real fix histories.
TARGETS = [
    ("werkzeug", "https://github.com/pallets/werkzeug"),
    ("dateutil", "https://github.com/dateutil/dateutil"),
    ("jsonschema", "https://github.com/python-jsonschema/jsonschema"),
    ("markdown", "https://github.com/Python-Markdown/markdown"),
    ("urllib3", "https://github.com/urllib3/urllib3"),
    ("humanize", "https://github.com/python-humanize/humanize"),
]


def run(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=str(Path.home() / "hunt"))
    ap.add_argument("--scan", type=int, default=600)
    ap.add_argument("--budget", type=int, default=60)
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--depth", type=int, default=1200)
    ap.add_argument("--only", default=None, help="comma-separated subset of target names")
    a = ap.parse_args()

    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    targets = TARGETS
    if a.only:
        keep = {s.strip() for s in a.only.split(",")}
        targets = [t for t in TARGETS if t[0] in keep]

    print("work dir: %s" % work)
    print("targets : %s" % ", ".join(n for n, _ in targets))
    print()

    all_leads = []
    for name, url in targets:
        repo = work / name
        if not repo.exists():
            print("cloning %s ..." % name, flush=True)
            run(["git", "clone", "-q", "--depth", str(a.depth), url, str(repo)])
        log = work / ("%s.log" % name)
        out = work / ("%s.json" % name)
        print("hunting %s (log: %s)" % (name, log.name), flush=True)
        t0 = time.time()
        with open(log, "w", encoding="utf-8") as fh:
            run([sys.executable, "-u", str(HUNT), str(repo),
                 "--scan", str(a.scan), "--budget", str(a.budget),
                 "--max", str(a.max), "--timeout", str(a.timeout),
                 "--out", str(out)], stdout=fh, stderr=subprocess.STDOUT)
        el = time.time() - t0
        leads = []
        if out.exists():
            try:
                leads = json.loads(out.read_text(encoding="utf-8"))
            except Exception:
                leads = []
        for L in leads:
            L["repo"] = name
        all_leads.extend(leads)
        print("  %s: %d leads in %.0f min" % (name, len(leads), el / 60), flush=True)

    md = work / "LEADS.md"
    with open(md, "w", encoding="utf-8") as fh:
        fh.write("# Regression leads\n\n")
        fh.write("%d leads across %d repositories.\n\n" % (len(all_leads), len(targets)))
        fh.write("Every lead below is a commit RunBoth says changed behaviour, where the project\n")
        fh.write("itself later shipped a fix touching the same function. The fix is their\n")
        fh.write("confirmation; the witness is the proof. A human reads both before anyone says\n")
        fh.write("the word regression out loud.\n\n")
        for L in all_leads:
            w = L.get("witness") or {}
            fh.write("## %s  %s\n\n" % (L.get("repo"), L.get("function")))
            fh.write("- suspect `%s` %s  %s\n" % (L.get("suspect", "")[:12],
                                                  str(L.get("suspect_date"))[:10],
                                                  L.get("suspect_subject")))
            fh.write("- fix     `%s` %s  %s\n" % (L.get("fix", "")[:12],
                                                  str(L.get("fix_date"))[:10],
                                                  L.get("fix_subject")))
            fh.write("- rung: %s\n" % L.get("rung"))
            fh.write("- args:   `%s`\n" % (w.get("args"),))
            fh.write("- before: `%s`\n" % (str(w.get("before"))[:200],))
            fh.write("- after:  `%s`\n\n" % (str(w.get("after"))[:200],))
    print()
    print("TOTAL %d leads. Written to %s" % (len(all_leads), md))


if __name__ == "__main__":
    main()
