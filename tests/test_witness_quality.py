"""The witness is the product. Which true example gets printed decides whether anyone believes it.

    pytest tests/test_witness_quality.py -v

Every differing input is equally true and they are NOT equally convincing. This is the real case
that produced the rule, a frontier model's refactor of `toolz.tail` submitted as
"behaviour unchanged":

    tail(-6, 2)         raised ValueError   ->  returned ()
    tail(-2, [1, 2, 3]) returned [3]        ->  returned []

The first invites an argument about whether anyone passes an int where a sequence goes. The
second is a silently wrong answer on an ordinary list and ends the argument. The engine used to
print whichever the generator happened to emit first.

Nothing is hidden by ranking: the verdict and the counts are identical either way. It only
chooses which true example to show.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runboth"))

from sandbox import best_witness, differs_only_by_a_function_name  # noqa: E402

# The exact observation flask's history produced: an inner helper renamed from
# `register_template` to `register_template_test`, seen through the closure stored in
# `deferred_functions`.
FLASK_BEFORE = ("['eff', ['val', 'None'], [['self', [['deferred_functions', '[]', "
                "'[<function Blueprint.add_app_template_test.<locals>.register_template "
                "at 0xADDR>]']]]]]")
FLASK_AFTER = ("['eff', ['val', 'None'], [['self', [['deferred_functions', '[]', "
               "'[<function Blueprint.add_app_template_test.<locals>.register_template_test "
               "at 0xADDR>]']]]]]")


def test_a_renamed_local_function_is_cosmetic():
    """Renaming an inner helper must not read as a behaviour change worth blocking.

    Found by running the drift hunt over flask's own history. Three `Blueprint` methods were
    reported as changed because a closure's repr carries its qualified name. Strictly true,
    since `deferred_functions[0].__name__` really did change, and completely useless: a gate
    that blocks a commit for renaming a local function gets uninstalled the same afternoon.
    """
    assert differs_only_by_a_function_name(FLASK_BEFORE, FLASK_AFTER)


def test_the_cosmetic_rule_stays_narrow():
    """It must never collapse a difference that is not purely a name.

    A qualified name contains `>` inside `.<locals>.`, so the pattern that erases function
    reprs has to survive that without swallowing the rest of the observation.
    """
    two = "[<function a at 0xADDR>, <function b at 0xADDR>]"
    assert differs_only_by_a_function_name(two, "[<function a at 0xADDR>, <function z at 0xADDR>]")

    # A function replaced by something else, removed, or added is a real difference.
    assert not differs_only_by_a_function_name("[<function a at 0xADDR>]", "[None]")
    assert not differs_only_by_a_function_name(two, "[<function a at 0xADDR>]")
    assert not differs_only_by_a_function_name("[]", "[<function a at 0xADDR>]")
    assert not differs_only_by_a_function_name("['val', '[3]']", "['val', '[]']")
    assert not differs_only_by_a_function_name("same", "same")


def test_a_wrong_value_beats_a_changed_exception_type():
    """Both are real differences. Only one survives a sceptical reader."""
    inputs = [[-6, 2], [-2, [1, 2, 3]]]
    before = ["['exc', 'ValueError']", "['val', '[3]']"]
    after = ["['val', '()']", "['val', '[]']"]

    w = best_witness(inputs, before, after)
    assert w["args"] == ["-2", "[1, 2, 3]"], w
    assert w["before"] == "['val', '[3]']"


def test_an_error_becoming_a_value_beats_one_exception_type_becoming_another():
    """A caller's `except ValueError` still fires for a different error; it never fires for a
    silently returned value. So the value case is the one worth printing."""
    inputs = [[1], [2]]
    before = ["['exc', 'ValueError']", "['exc', 'ValueError']"]
    after = ["['exc', 'TypeError']", "['val', 'None']"]

    w = best_witness(inputs, before, after)
    assert w["args"] == ["2"], w
    assert w["after"] == "['val', 'None']"


def test_the_plainer_argument_wins_a_tie():
    """Same kind of difference, so prefer the call a reader recognises over fuzzer output."""
    inputs = [[list(range(60))], [3]]
    before = ["['val', 'a']", "['val', 'a']"]
    after = ["['val', 'b']", "['val', 'b']"]

    w = best_witness(inputs, before, after)
    assert w["args"] == ["3"], w


def test_no_difference_yields_no_witness():
    """The ranking must never manufacture a finding out of agreement."""
    inputs = [[1], [2]]
    keys = ["['val', '1']", "['val', '2']"]
    assert best_witness(inputs, keys, list(keys)) is None


def test_a_single_difference_is_still_reported():
    """Ranking must not lose the only witness there is."""
    inputs = [[1], [2]]
    before = ["['val', '1']", "['val', '2']"]
    after = ["['val', '1']", "['val', '99']"]

    w = best_witness(inputs, before, after)
    assert w is not None and w["args"] == ["2"], w
