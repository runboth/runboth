# What it caught: a frontier model quietly breaking `toolz`

A short, reproducible case. No maintainer made a mistake here. The library is fine. The AI asked
to refactor it was not, and it said so in the commit message.

## The setup

`toolz` is a real, widely used Python library. `tail(n, seq)` returns the last `n` elements:

```python
def tail(n, seq):
    try:
        return seq[-n:]
    except (TypeError, KeyError):
        return tuple(collections.deque(seq, n))
```

A frontier model was asked to refactor it, with instructions to preserve behaviour. It produced
this, and described it as behaviour-preserving:

```python
def tail(n, seq):
    if n <= 0:
        return type(seq)() if hasattr(seq, '__getitem__') else tuple()
    if hasattr(seq, '__getitem__'):
        try:
            return seq[len(seq) - n:]
        except (TypeError, KeyError):
            pass
    result = []
    for item in seq:
        result.append(item)
        if len(result) > n:
            result.pop(0)
    return tuple(result)
```

Read the diff the way a reviewer would. It is longer, it handles the non-subscriptable case
explicitly, and it opens with a guard clause that looks like exactly the kind of defensive check
a careful engineer adds. There is nothing in it that reads as a bug.

## What actually changed

```
tail(0, [1, 2, 3])       toolz: [1, 2, 3]      refactor: []
tail(-1, [1, 2, 3])      toolz: [2, 3]         refactor: []
tail(-2, [1, 2, 3, 4, 5])toolz: [3, 4, 5]      refactor: []
tail(2, [10,20,30,40,50])toolz: [40, 50]       refactor: [40, 50]      (agrees)
```

The guard `if n <= 0: return empty` looks obviously correct and is not. In the original,
`seq[-n:]` with `n = 0` is `seq[0:]`, the **whole sequence**. With `n = -1` it is `seq[1:]`. The
original never treated a non-positive `n` as "return nothing", and the refactor does.

So the failure is the worst shape a change can take: **a loud error and a correct answer both
became a silently empty result.** A caller gets `[]`, no exception, and carries on with wrong data.

## Why the usual defences miss it

- **The docstring example still passes.** `tail(2, [10, 20, 30, 40, 50])` returns `[40, 50]` in
  both versions, so the doctest is green.
- **A reviewer reads intent.** The diff looks like hardening. Nothing about it says "this changes
  results for an entire class of input".
- **A test suite covers what someone thought to cover.** `n = 0` is an edge case that a library's
  own tests may exercise, but AI-written code in a prototype usually has no suite at all.

## What RunBoth does with it

Committing that refactor, with the message the model itself would write:

```
$ git commit -m "refactor(itertoolz): simplify tail, behaviour unchanged"

  BLOCKED: your commit message says the behaviour did not change.
  It did.

    tail(-2, [1, 2, 3])
      used to:  return [3]
      now:      return []

  That call is the proof. Anything relying on the old result behaves
  differently now, and your test suite did not stop this commit.

  Checked 80 inputs per function. Evidence, not proof.
  Meant to change it?  git commit --no-verify
```

No test was written. No annotation was added. The inputs came from the function's own signature
and from the constants in its own bytecode.

## It is not one function

The same audit ran a frontier model over 36 functions from `toolz` and `markupsafe`, each time
asking for a behaviour-preserving refactor and then measuring the result.

| | |
|---|---|
| Refactors adjudicated | 36 |
| Behaviour preserved | 20 |
| **Behaviour changed** | **16** |

**44% of the refactors the model called behaviour-preserving were not.** Several were exception
types quietly swapped, `ValueError` becoming `TypeError`, which passes every test that only checks
"does it raise" and breaks every caller that catches the specific one.

Raw data, including the before and after source of every failure, is in
`results/agent_refactor_audit_2026-09-12.json`.

## The honest caveats

- One model, one prompt style, 36 functions. It is a measurement, not a law, and a different model
  or a gentler refactoring brief would give a different number.
- RunBoth cannot tell you the refactor is *wrong*, only that it **behaves differently** from what
  it replaced while claiming not to. Deciding which version is correct stays yours. In this case
  the original is the specified behaviour and the refactor is the deviation.
- Sampling finds differences, it cannot prove their absence. Every verdict carries its budget.
