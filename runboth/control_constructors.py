"""Known-answer controls for ctor_params. Half of these MUST still refuse, or the widening is
just a way of pretending unconstructible classes are constructible."""
import sys

sys.path.insert(0, "/opt/runboth/py")
from methods import ctor_params  # noqa: E402

CASES = [
    ("plain required params",
     "class C:\n    def __init__(self, a, b):\n        pass\n", "C", ["a", "b"]),
    ("defaults are omitted, not generated",
     "class C:\n    def __init__(self, a, b=1, c=2):\n        pass\n", "C", ["a"]),
    ("*args and **kwargs alongside a required param",
     "class C:\n    def __init__(self, app, *args, **kwargs):\n        pass\n", "C", ["app"]),
    ("*args only",
     "class C:\n    def __init__(self, *args):\n        pass\n", "C", []),
    ("**kwargs only",
     "class C:\n    def __init__(self, **kw):\n        pass\n", "C", []),
    ("keyword-only WITH a default is fine",
     "class C:\n    def __init__(self, a, *, mode=1):\n        pass\n", "C", ["a"]),
    ("keyword-only with NO default must still REFUSE",
     "class C:\n    def __init__(self, a, *, mode):\n        pass\n", "C", None),
    ("no __init__ at all: no arguments",
     "class C:\n    def f(self):\n        pass\n", "C", []),
    ("nested inside another class must be FOUND",
     "class Outer:\n    class C:\n        def __init__(self, a):\n            pass\n", "C", ["a"]),
    ("defined inside a function must be FOUND",
     "def make():\n    class C:\n        def __init__(self, a, b):\n            pass\n    return C\n",
     "C", ["a", "b"]),
    ("positional-only params",
     "class C:\n    def __init__(self, a, /, b):\n        pass\n", "C", ["a", "b"]),
    ("class genuinely absent must REFUSE",
     "class D:\n    pass\n", "C", None),
    ("unparseable source must REFUSE",
     "class C:\n    def __init__(self,\n", "C", None),
]

fails = 0
for label, src, cls, want in CASES:
    got = ctor_params(src, cls)
    got_names = None if got is None else [n for n, _ in got]
    ok = got_names == want
    fails += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<48} -> {got_names}"
          + ("" if ok else f"   WANTED {want}"))

print(f"\n  {len(CASES) - fails}/{len(CASES)} correct "
      f"({sum(1 for c in CASES if c[3] is None)} of them are REFUSALS that must stay refusals)")
sys.exit(1 if fails else 0)
