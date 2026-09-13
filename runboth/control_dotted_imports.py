"""Known-answer controls for dotted_name. Every real layout, and each one must be exact."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/opt/runboth/py")
from sandbox import dotted_name  # noqa: E402

CASES = [
    # (files to create, module under test, expected dotted, expected sys.path suffix)
    (["src/markupsafe/__init__.py"], "src/markupsafe/__init__.py", "markupsafe", "src"),
    (["src/markupsafe/__init__.py", "src/markupsafe/_native.py"],
     "src/markupsafe/_native.py", "markupsafe._native", "src"),
    (["src/flask/__init__.py", "src/flask/sansio/app.py"],
     "src/flask/sansio/app.py", "flask.sansio.app", "src"),
    (["src/flask/__init__.py", "src/flask/sansio/__init__.py", "src/flask/sansio/app.py"],
     "src/flask/sansio/app.py", "flask.sansio.app", "src"),
    (["toolz/__init__.py", "toolz/itertoolz.py"], "toolz/itertoolz.py", "toolz.itertoolz", ""),
    (["toolz/__init__.py", "toolz/curried/__init__.py"],
     "toolz/curried/__init__.py", "toolz.curried", ""),
    (["click/__init__.py", "click/core.py"], "click/core.py", "click.core", ""),
    (["standalone.py"], "standalone.py", "standalone", ""),
    (["pkg/sub/deep/mod.py", "pkg/__init__.py", "pkg/sub/__init__.py", "pkg/sub/deep/__init__.py"],
     "pkg/sub/deep/mod.py", "pkg.sub.deep.mod", ""),
]

fails = 0
for files, target, want_dotted, want_root_suffix in CASES:
    d = tempfile.mkdtemp()
    for f in files:
        p = Path(d) / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    got, root = dotted_name(d, target)
    root_suffix = str(Path(root).relative_to(d)).replace("\\", "/") if root else "?"
    root_suffix = "" if root_suffix == "." else root_suffix
    ok = (got == want_dotted and root_suffix == want_root_suffix)
    fails += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {target:<38} -> {str(got):<22} root=+{root_suffix or '.'}"
          + ("" if ok else f"   WANTED {want_dotted} root=+{want_root_suffix or '.'}"))

print(f"\n  {len(CASES) - fails}/{len(CASES)} layouts correct")
sys.exit(1 if fails else 0)
