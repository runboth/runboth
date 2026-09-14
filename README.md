<p align="center">
  <img src="https://raw.githubusercontent.com/runboth/runboth/main/brand/mark-128.png" width="72" alt="RunBoth">
</p>

<h1 align="center">RunBoth</h1>

<p align="center">
  <b>An AI changed your code. RunBoth runs both versions and tells you what actually behaves
  differently, including the functions nobody touched.</b>
</p>

<p align="center">
  <a href="https://github.com/runboth/runboth/actions/workflows/ci.yml"><img
    src="https://github.com/runboth/runboth/actions/workflows/ci.yml/badge.svg"
    alt="CI"></a>
  <a href="https://pypi.org/project/runboth/"><img
    src="https://img.shields.io/pypi/v/runboth.svg?color=2f6f4f" alt="PyPI"></a>
  <a href="https://pypi.org/project/runboth/"><img
    src="https://img.shields.io/pypi/pyversions/runboth.svg" alt="Python versions"></a>
  <a href="https://github.com/runboth/runboth/blob/main/LICENSE.md"><img
    src="https://img.shields.io/badge/licence-FSL--1.1--Apache--2.0-3b5bdb.svg"
    alt="Licence: FSL-1.1-Apache-2.0"></a>
</p>

<p align="center">
  <a href="https://runboth.dev">runboth.dev</a> ·
  <a href="https://github.com/runboth/runboth/blob/main/HOW_IT_WORKS.md">How it works</a> ·
  <a href="https://github.com/runboth/runboth/blob/main/RED_TEAM_2026-09-12.md">Red team results</a>
</p>

---

```
$ git commit -m "refactor: tidy up the rates module"

  BLOCKED: the behaviour changed and your message does not say so.

    rate(100)
      used to:  return 0.1
      now:      return 0.0

  AND 1 function you did NOT touch now behaves differently,
  because it calls what you changed:

    total(2.5, 100)          pkg/invoice.py
      used to:  return 225.0
      now:      return 250.0
```

## Why this exists

An AI refactors a module and reports that the behaviour is unchanged. Usually that is true.
Measurably often it is not: Dristi and Dwyer tested six models across three datasets and two
refactoring types and found LLM refactorings **functionally non-equivalent 19 to 35% of the
time** ([arXiv:2602.15761](https://arxiv.org/abs/2602.15761), February 2026).

Review does not catch it. A diff is an honest record of the *edit*, not of the *effect*, and
those are different documents. The function that broke is often in a file the diff never
mentions, because it only calls what changed.

Tests do not close the gap either. A test asserts what somebody previously thought to assert,
and the code most likely to move silently is the code nobody wrote a test for. If an AI wrote
the tests as well, you have asked one system to mark its own homework.

So RunBoth does the boring thing that works: it runs both versions and compares what actually
comes out.

## What it does

It checks out both versions of your code, generates inputs for every changed function from its
signature and from the constants mined out of its own bytecode, runs both versions in separate
sandboxed subprocesses, and compares seven observation channels. When they disagree it hands you
the exact input that separates them.

No test suite required. No network calls. No AI model. No dependencies.

## Install

```bash
pip install runboth                                  # once the first release is on PyPI
pip install git+https://github.com/runboth/runboth   # works today
runboth install-hook          # a commit-msg gate, silent unless behaviour moved
```

As a GitHub Action, running on your own runners:

```yaml
- uses: runboth/runboth@v0.1.0
  with:
    budget: 60
```

## Three verdicts, never two

| verdict | meaning |
|---|---|
| `changed` | with a witness: the arguments, the old result, the new result |
| `no_change at budget N` | N generated inputs found no difference across seven channels |
| `abstained` | it could not be checked, and here is the reason |

"Cannot tell" and "no difference" are different claims, and collapsing them into a green check is
how tools end up lying. **RunBoth never says safe.**

## The seven channels

Return value · exception raised · warnings · stdout · stderr · argument mutation · object state.

A narrow definition of behaviour does not under-report, it lies, because whatever sits outside the
definition comes back as `no_change`.

## What it is not

**Not a model checker.** Kani and CBMC translate code into logic, let inputs be unconstrained
symbols, and ask a solver whether a bad state is reachable within a bound. They return a proof.
RunBoth executes real code on concrete values. It finds differences and reproduces them; it
cannot prove absence, and never claims to.

**Not mutation testing.** Mutation testing damages your code to score your test suite. RunBoth
damages nothing; both versions come from your git history, and no test suite is needed.

## Measured

Red-teamed against eight public repositories it had never been tuned on, with an automated oracle
built to catch the tool lying. **2,548 functions, zero false positives.** Full method and numbers
in [RED_TEAM_2026-09-12.md](https://github.com/runboth/runboth/blob/main/RED_TEAM_2026-09-12.md).

| repo | layout | functions | abstained | median/commit |
|---|---|---|---|---|
| boltons | flat | 268 | 0.0% | 7.9s |
| sqlparse | flat | 230 | 0.9% | 14.3s |
| arrow | flat | 283 | 1.4% | 118s |
| cachetools | src/ | 325 | 1.8% | 60.9s |
| more-itertools | flat | 843 | 4.4% | 102.5s |
| packaging | src/ | 78 | 5.1% | 0.2s |
| pluggy | src/ | 157 | 6.4% | 33.5s |
| tenacity | async | 364 | 14.3% | 192.8s |

An adversarial corpus of 22 functions written specifically to induce false positives (object
addresses in default `repr`, `datetime.now`, unseeded `random`, `uuid4`, `os.getpid`, set
iteration order, mutable defaults, generators, `__file__` paths) produced none.

## Environment variables

| variable | effect |
|---|---|
| `RUNBOTH_SKIP=1` | let a commit through without checking it |
| `RUNBOTH_BUDGET` | generated inputs per function (gate default 80) |
| `RUNBOTH_WORKERS` | parallel adjudications, default 4 |
| `RUNBOTH_ALL_PATHS=1` | also check tests, benchmarks, docs and task runners |
| `RUNBOTH_ENGINE` | engine directory, if the hook cannot resolve it |
| `RUNBOTH_NO_VERSION_STUB=1` | do not synthesise a missing generated `_version.py` |

`git commit --no-verify` also bypasses the gate, and the gate says so itself when it blocks.

## Honest limits

- Function-level checking is **Python only**. Changed files in other languages are named
  explicitly rather than passed over quietly.
- Sampling finds differences; it cannot prove their absence.
- Nondeterministic, too-slow, or unconstructible functions abstain **with a reason**, and are
  never counted as passing.
- The sandbox contains accidents: resource limits, network blocked, filesystem writes blocked. It
  is **not** a security boundary against hostile code, and no pure-Python sandbox is.
- A `_version.py` that the build generates is synthesised as `0.0.0` so the package can be
  imported at all. A commit that changes how a version string is **derived** is therefore not
  measured. Both sides get the same stub, so it can never manufacture a `changed`, and
  `RUNBOTH_NO_VERSION_STUB=1` turns it off.

## Development

```bash
pip install -e .
pytest tests/ -q
runboth selftest          # the control suites, half of which must fail
```

## Licence

[FSL-1.1-Apache-2.0](https://github.com/runboth/runboth/blob/main/LICENSE.md). Free for every use except building a competing product, and it
converts to plain Apache 2.0 two years after each release.

Built by Kyle Clouthier at Clouthier Simulation Labs.
