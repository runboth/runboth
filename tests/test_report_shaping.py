"""How a finding is WORDED is product surface, and every case here was a real defect.

    pytest tests/test_report_shaping.py -v

All three were found by red team on 2026-09-12 against repositories the project had never been
tuned on. None of them is a wrong verdict; all three are the tool being right in a way nobody
would keep installed.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runboth"))

from adjudicate import (  # noqa: E402
    call_text, collapse_constructor_findings, constructor_rollup_line, is_noise_path,
)


# --------------------------------------------------------------- call rendering

def test_method_call_is_rendered_as_someone_would_type_it():
    """The witness carries construction and call as two pieces meant to be concatenated.

    Every report surface comma-joined them and wrapped the result in the function name again,
    printing `FIFOCache.clear(FIFOCache(-2), .clear())`. That affected every method finding the
    tool has ever printed, which is the most convincing part of the product.
    """
    assert call_text("p.py::FIFOCache.clear", ["FIFOCache(-2)", ".clear()"]) == "FIFOCache(-2).clear()"


def test_constructor_is_rendered_as_a_construction():
    """`Cache(-2).__init__(-6, 2)` is not how anyone builds a Cache.

    The harness treats __init__ as a method on an already-built object, which is correct for
    measuring and wrong for printing. The arguments that matter are __init__'s own.
    """
    assert call_text("p.py::Cache.__init__", ["Cache(-2)", ".__init__(-6, 2)"]) == "Cache(-6, 2)"


def test_plain_function_call_is_unchanged():
    assert call_text("p.py::total", ["2.5", "100"]) == "total(2.5, 100)"
    assert call_text("discount", ["100"]) == "discount(100)"


# --------------------------------------------------------- constructor collapsing

def _ctor_fail(qname, cls):
    return {"function": qname, "verdict": "changed",
            "witness": {"args": [f"{cls}(-2)", ".m()"], "before": "['val', 'None']",
                        "after": "['ctor', 'ValueError']"}}


def test_constructor_failures_collapse_into_the_constructor():
    """cachetools dd181c5a72: a two-line commit produced 45 findings, 39 of them this shape.

    `FIFOCache.clear` did not change. `FIFOCache(-2)` stopped existing. Reporting the first is a
    category error, and 45 findings for one intended line is a tool that gets switched off.
    """
    recs = [
        _ctor_fail("p.py::FIFOCache.__init__", "FIFOCache"),
        _ctor_fail("p.py::FIFOCache.clear", "FIFOCache"),
        _ctor_fail("p.py::FIFOCache.pop", "FIFOCache"),
        {"function": "p.py::unrelated", "verdict": "changed",
         "witness": {"args": ["1"], "before": "['val', '1']", "after": "['val', '2']"}},
        {"function": "p.py::quiet", "verdict": "no_change", "witness": None},
    ]
    kept, rollup = collapse_constructor_findings(recs)
    names = [r["function"] for r in kept]

    assert "p.py::FIFOCache.clear" not in names, "a folded method must not be listed separately"
    assert "p.py::FIFOCache.pop" not in names
    assert "p.py::FIFOCache.__init__" in names, "the anchor must survive"
    assert "p.py::unrelated" in names, "an unrelated real change must never be folded away"
    assert "p.py::quiet" in names, "non-changed records pass through untouched"

    line = constructor_rollup_line(rollup, "p.py::FIFOCache.__init__")
    assert line and "2 methods" in line, line
    assert "FIFOCache" in line


def test_nothing_is_folded_without_an_anchor():
    """A folded finding with no constructor to hang it on would be a finding silently dropped.

    Silence is how this tool says "no difference found", so a disappearing finding is the one
    outcome that must never happen. With no changed __init__ anywhere, everything stays.
    """
    recs = [_ctor_fail("p.py::Thing.a", "Thing"), _ctor_fail("p.py::Thing.b", "Thing")]
    kept, rollup = collapse_constructor_findings(recs)
    assert len(kept) == 2, "with no anchor, every finding must survive"
    assert not rollup


def test_collapsing_is_a_no_op_when_there_are_no_constructor_failures():
    recs = [{"function": "p.py::f", "verdict": "changed",
             "witness": {"args": ["1"], "before": "['val', '1']", "after": "['val', '2']"}}]
    kept, rollup = collapse_constructor_findings(recs)
    assert kept == recs and not rollup


# ------------------------------------------------------------------ path filtering

def test_tests_and_benchmarks_are_not_product_findings():
    """Five of seven findings on sqlparse's ReDoS fix were helpers in `benchmarks/`.

    Three of eight more-itertools commits also burned a 700s timeout returning nothing, and all
    three touched only `tests/test_more.py`.
    """
    for noise in ("tests/test_more.py", "benchmarks/bench_x.py", "docs/conf.py",
                  "conftest.py", "a/b/examples/demo.py", "src/pkg/test_helpers.py"):
        assert is_noise_path(noise), noise


def test_build_and_task_runner_files_are_not_product_findings():
    """pypa/packaging: 51 of 55 abstentions were `No module named 'nox'` from `noxfile.py`.

    It holds 17 functions and appeared in three of six sampled commits, so those three commits
    produced nothing but abstentions. These files exist in a large fraction of Python repos and
    all of them import dev-only dependencies that the adjudicating environment does not have.
    """
    for noise in ("noxfile.py", "setup.py", "tasks.py", "fabfile.py", "manage.py",
                  "dodo.py", "tools/noxfile.py"):
        assert is_noise_path(noise), noise


def test_path_filtering_does_not_swallow_real_code():
    """Conservative on purpose, because skipping real code is worse than reporting noise.

    `scripts/` and `fixtures/` were in the skip list and were removed before shipping: plenty of
    projects keep product code under `scripts/`. Whole directory names only, so `contest/` and a
    module named `benchmarks.py` both survive, and `setup_helpers.py` is not `setup.py`.
    """
    for real in ("sqlparse/lexer.py", "src/contest/thing.py", "pkg/benchmarks.py",
                 "more_itertools/more.py", "src/cachetools/__init__.py",
                 "scripts/deploy.py", "fixtures/data.py", "pkg/setup_helpers.py"):
        assert not is_noise_path(real), real
