"""What actually changed between two released versions of a package.

    python runboth/versions.py arrow 1.3.0 1.4.0
    python runboth/versions.py <package> <old> <new> --budget 60 --json out.json

# Why this exists

Every other entry point in this project compares two commits, which means it needs the source
history and it answers a question the author already had an opinion about. This one needs neither.
It installs both released versions side by side and runs them, so it answers the question a
*consumer* has, and that consumer has no opinion because they wrote nothing:

    I am about to upgrade. What breaks?

Nobody can answer that today. A release changelog is written from memory by the person least able
to notice what they changed by accident, and a version bump in a lockfile has a one-line diff that
no code reviewer, human or otherwise, can evaluate. The only honest answer comes from running both.

Measured on 2026-09-13, before this existed: arrow 1.4.0 changed `Arrow.dst()` from
`timedelta(0)` to `None` and `timetuple().tm_isdst` from `0` to `-1`, on the default UTC timezone.
Its changelog says "Migrated Arrow to use ZoneInfo for timezones". Nothing about `dst()`. Both
facts were true of the published wheels and neither was written down anywhere.

# How it works

Two throwaway virtual environments, one per version, and the package directory inside each becomes
a "tree" in exactly the sense the commit path already means. From there it is the same engine:
match functions by qualified name, generate inputs from signatures and mined constants, execute
both in sandboxed subprocesses, compare seven channels, abstain with a reason when it cannot tell.

The verdict vocabulary is unchanged and so are its limits. Sampling finds differences and cannot
prove their absence.
"""
import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from adjudicate import adjudicate_pair, functions_in, is_noise_path  # noqa: E402


def _pip(env_dir):
    """The pip inside a venv, on either platform."""
    for rel in ("Scripts/pip.exe", "bin/pip"):
        p = Path(env_dir) / rel
        if p.exists():
            return str(p)
    raise RuntimeError(f"no pip inside {env_dir}")


def install(package, version, into, quiet=True):
    """Build a venv and install exactly one version into it. Returns the site-packages path."""
    venv.EnvBuilder(with_pip=True, clear=True).create(into)
    cmd = [_pip(into), "install", "--no-input", f"{package}=={version}"]
    if quiet:
        cmd.insert(2, "--quiet")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout)[-400:]

    for rel in ("Lib/site-packages", f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"):
        sp = Path(into) / rel
        if sp.exists():
            return sp, None
    # Fall back to asking the interpreter, since layouts vary.
    py = str(Path(into) / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    r = subprocess.run([py, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                       capture_output=True, text=True, timeout=120)
    sp = Path(r.stdout.strip())
    return (sp, None) if sp.exists() else (None, "could not locate site-packages")


def package_files(site_packages, package):
    """Every .py file belonging to the package, relative to site-packages.

    Relative, because the two installs live in different directories and the engine matches
    functions across trees by relative path.
    """
    root = site_packages / package.replace("-", "_")
    if not root.is_dir():
        cand = [p for p in site_packages.glob(f"{package.replace('-', '_')}*")
                if p.is_dir() and (p / "__init__.py").exists()]
        if not cand:
            single = site_packages / f"{package.replace('-', '_')}.py"
            return [single.name] if single.exists() else []
        root = cand[0]
    out = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(site_packages).as_posix()
        if is_noise_path(rel):
            continue
        out.append(rel)
    return out


def is_public(qname):
    """Underscore-prefixed pieces are private, and a consumer upgrading does not care."""
    short = qname.split("::", 1)[-1]
    return not any(part.startswith("_") and not part.startswith("__")
                   for part in short.split("."))


def compare_versions(package, old, new, budget=60, workers=4, limit=None, verbose=True):
    tmp = tempfile.mkdtemp(prefix=f"runboth_ver_{package}_")
    a_dir, b_dir = Path(tmp) / "old", Path(tmp) / "new"

    if verbose:
        print(f"installing {package}=={old} ...", flush=True)
    sp_old, err = install(package, old, a_dir)
    if err:
        return None, f"could not install {package}=={old}: {err}"
    if verbose:
        print(f"installing {package}=={new} ...", flush=True)
    sp_new, err = install(package, new, b_dir)
    if err:
        return None, f"could not install {package}=={new}: {err}"

    files = sorted(set(package_files(sp_old, package)) & set(package_files(sp_new, package)))
    if verbose:
        print(f"{len(files)} module(s) present in both versions", flush=True)

    jobs = []
    for rel in files:
        try:
            src_a = (sp_old / rel).read_text(encoding="utf-8", errors="replace")
            src_b = (sp_new / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fa, fb = functions_in(src_a, rel), functions_in(src_b, rel)
        for q in sorted(set(fa) & set(fb)):
            if not is_public(q):
                continue
            try:
                if ast.unparse(fa[q]) == ast.unparse(fb[q]):
                    # Identical source, but a DIFFERENT version underneath it. That is exactly
                    # the case worth running: the function did not change, its world did.
                    pass
            except Exception:
                pass
            jobs.append((q, fa[q], fb[q], src_a, src_b))

    if limit:
        jobs = jobs[:limit]
    if verbose:
        print(f"{len(jobs)} public function(s) to adjudicate", flush=True)

    import concurrent.futures as cf
    records = []

    def one(job):
        q, na, nb, sa, sb = job
        try:
            return adjudicate_pair(q, na, nb, budget, sa, sb, str(sp_old), str(sp_new))
        except BaseException as e:  # noqa: BLE001
            return {"function": q, "verdict": "abstained", "witness": None,
                    "reason": f"internal error: {type(e).__name__}: {e}"[:160]}

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for i, rec in enumerate(ex.map(one, jobs), 1):
            records.append(rec)
            if verbose and rec["verdict"] == "changed" and rec.get("witness"):
                w = rec["witness"]
                print(f"  CHANGED  {rec['function']}", flush=True)
                print(f"           {w.get('args')}", flush=True)
                print(f"           {str(w.get('before'))[:90]}  ->  "
                      f"{str(w.get('after'))[:90]}", flush=True)
            elif verbose and i % 40 == 0:
                print(f"  ... {i}/{len(jobs)}", flush=True)
    return records, None


def main():
    ap = argparse.ArgumentParser(description="What actually changed between two package versions")
    ap.add_argument("package")
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--budget", type=int, default=60)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--json", dest="json_out", default=None)
    a = ap.parse_args()

    records, err = compare_versions(a.package, a.old, a.new, a.budget, a.workers, a.limit)
    if err:
        print(err, file=sys.stderr)
        return 2

    changed = [r for r in records if r["verdict"] == "changed" and r.get("witness")]
    structural = [r for r in records if r["verdict"] == "changed" and not r.get("witness")]
    abst = [r for r in records if r["verdict"] == "abstained"]
    same = [r for r in records if r["verdict"] == "no_change"]

    print(f"\n{a.package} {a.old} -> {a.new}")
    print(f"  {len(changed)} behaviour change(s) with a witness")
    print(f"  {len(structural)} added or removed")
    print(f"  {len(same)} unchanged at budget {a.budget}")
    print(f"  {len(abst)} could not be checked\n")

    for r in changed[:25]:
        w = r["witness"]
        print(f"  {r['function']}")
        print(f"    {w.get('args')}")
        print(f"    was: {str(w.get('before'))[:110]}")
        print(f"    now: {str(w.get('after'))[:110]}")

    if a.json_out:
        Path(a.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json_out).write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {a.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
