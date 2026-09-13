"""Did we actually reach the code that changed?

    pytest tests/test_change_coverage.py -v

The flaw this closes, found 2026-09-13 while reading DiffTestGen (arXiv 2607.16024), whose
contribution is a "union coverage" metric over the changed lines of both versions:

    `no_change at budget 60` could mean "sixty inputs ran, not one of them reached the line you
    edited, and I am reporting that nothing changed".

That is the exact error this project forbids everywhere else. Silence that sounds like a verdict.
An unreached change is something the tool CANNOT TELL, so it is an abstention with a reason.

The formula is deliberately theirs, (covered_old + covered_new) / (changed_old + changed_new), so
that comparing against their published numbers is honest rather than a redefinition in our favour.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runboth"))

from adjudicate import adjudicate_pair, functions_in  # noqa: E402
from sandbox import changed_lines, union_coverage  # noqa: E402

# The guard is a SIZE condition, so there is no literal for constant mining to extract, and no
# generated input at this budget is a thousand-element list. The import makes the function
# non-harvestable, which routes it through the sandbox where coverage is measured.
UNREACHED_BEFORE = '''
import math


def summarise(items):
    if len(items) > 1000:
        return math.fsum(items) / len(items)
    return len(items)
'''
UNREACHED_AFTER = UNREACHED_BEFORE.replace("math.fsum(items) / len(items)",
                                           "math.fsum(items) * 2")

REACHED_BEFORE = '''
import math


def scale(n):
    return math.floor(n) + 1
'''
REACHED_AFTER = REACHED_BEFORE.replace("math.floor(n) + 1", "math.floor(n) + 2")


def _adjudicate(before_src, after_src, fname, budget=60):
    d = Path(tempfile.mkdtemp())
    (d / "old").mkdir()
    (d / "new").mkdir()
    (d / "old" / "m.py").write_text(before_src, encoding="utf-8")
    (d / "new" / "m.py").write_text(after_src, encoding="utf-8")
    fb, fa = functions_in(before_src, "m.py"), functions_in(after_src, "m.py")
    q = next(k for k in fb if k.endswith(f"::{fname}"))
    return adjudicate_pair(q, fb[q], fa[q], budget, before_src, after_src,
                           str(d / "old"), str(d / "new"))


def test_an_unreached_change_abstains_instead_of_claiming_no_change():
    """The whole point. Reporting no_change here would be a lie of omission."""
    rec = _adjudicate(UNREACHED_BEFORE, UNREACHED_AFTER, "summarise")
    assert rec["verdict"] != "no_change", (
        "claimed no_change while never executing the changed line:\n" + str(rec))
    assert rec["verdict"] == "abstained", rec
    assert rec.get("change_coverage") == 0.0, rec
    assert "executed the changed lines" in (rec.get("reason") or ""), rec


def test_a_reached_change_is_still_reported_normally():
    """Coverage must qualify silence, never suppress a real finding."""
    rec = _adjudicate(REACHED_BEFORE, REACHED_AFTER, "scale")
    assert rec["verdict"] == "changed", rec
    assert rec.get("witness"), rec


def test_changed_lines_finds_only_the_lines_that_moved():
    """A one-line edit is one changed line, not the whole function."""
    import ast
    before = ast.parse("def f(a):\n    x = 1\n    y = 2\n    return x + y\n").body[0]
    after = ast.parse("def f(a):\n    x = 1\n    y = 99\n    return x + y\n").body[0]
    b, a = changed_lines(before, after)
    assert len(b) == 1 and len(a) == 1, (b, a)


def test_identical_functions_have_nothing_to_cover():
    import ast
    node = ast.parse("def f(a):\n    return a + 1\n").body[0]
    b, a = changed_lines(node, node)
    assert b == set() and a == set()
    assert union_coverage(b, a, [], []) is None, "no changed lines means no coverage question"


def test_the_formula_matches_the_published_one():
    """(covered_old + covered_new) / (changed_old + changed_new), per DiffTestGen."""
    assert union_coverage({1, 2}, {1, 2}, [1, 2], [1, 2]) == 1.0
    assert union_coverage({1, 2}, {1, 2}, [1], [1]) == 0.5
    assert union_coverage({1, 2}, {1, 2}, [], []) == 0.0
    assert union_coverage({1}, set(), [1], []) == 1.0
