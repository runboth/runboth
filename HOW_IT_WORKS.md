# How RunBoth works

An AI changed your code. RunBoth runs both versions and tells you what actually behaves
differently, including the functions nobody touched.

This page is the honest version of the mechanism, written for someone deciding whether to trust
it. Nothing here is novel as a technique, and the project does not claim it is. What is unusual is
that it is wired together to run on a commit, on a real repository, with no test suite required.

## The short answer

It runs your code. Both versions of it. On the same inputs. In separate processes. Then it
compares what came out, across seven different channels, and hands you the exact input that made
them differ.

There is no AI in the product, no network call, and no dependencies. The engine is standard
library Python.

## What it is not

Two things get assumed, and neither is right.

**It is not a model checker.** Kani, CBMC and friends work by not running your code. They
translate it into logic, let each input be an unconstrained symbol (the "havoc" step), and ask a
solver whether a bad state is reachable within some bound. You get a proof. You pay for it in
modelling effort, in solver time, and in the language subset the tool supports. RunBoth executes
the real function, on concrete values, in a real interpreter. It can find a difference and show
you one. It can never prove there isn't one.

**It is not mutation testing.** Mutation testing damages your code on purpose to see whether your
test suite notices, and scores the suite. RunBoth damages nothing. The two versions both come out
of your git history. It needs no test suite at all, which is the entire point for code that was
written fast and does not have one yet.

| | Method | What you get | Needs |
|---|---|---|---|
| Kani / CBMC | Symbolic, solver-backed | A proof within a bound | Modelling effort, a supported subset |
| Mutation testing | Injects faults into your code | A score for your tests | An existing test suite |
| RunBoth | Executes both real versions | A reproducible witness | Two commits |

## The pipeline

### 1. Put both versions on disk

Both commits are materialised into separate directories, so both versions of the program exist at
the same time, imports and packages intact. This matters: a function is rarely self-contained, and
running it against the wrong version of its own package would measure the wrong thing.

### 2. Decide what is worth running

Both sides are parsed and functions are matched by name. A function is skipped as unchanged only
when its source text is identical **and** it is a leaf that touches no imported or module-level
name.

That second condition is the one people get wrong, and it is worth stating plainly:

> Identical source is not identical behaviour.

A function whose text did not change still changes when something it calls changes. That is the
regression class no diff reader can see, because there is no diff to read. Any tool that decides
"unchanged text, therefore unchanged behaviour" is reading the diff for the one case where reading
the diff cannot work.

### 3. Invent inputs

Inputs come from two places: the parameter types in the signature, and constants mined out of the
function's own bytecode and source literals.

The mined constants are what make this useful rather than academic. A function containing
`if units >= 100` gets tested at 99, 100 and 101, because 100 is sitting right there in its own
bytecode. Blind random draws almost never land on a boundary; the boundary is where the bugs are.

The random seed is derived from the function's name, so the same function gets the same inputs on
every run, on every machine. A witness you saw yesterday reproduces today.

### 4. Run both, under restraint

Each version runs in its own fresh subprocess, on the same inputs in the same order.

A subprocess, rather than a call in the current process, for three measured reasons:

- Resource limits apply to a process. A runaway loop in-process takes the whole tool down.
- Import side effects do not accumulate. Importing a module runs its top level; doing that ten
  times in one interpreter means the tenth measurement sees state from the first nine.
- Some nondeterminism is only visible across processes. Hash ordering and object identity are
  stable inside a single run and vary between runs, so seeing it requires two runs.

Each worker gets CPU, memory, file-size and open-file limits, a hard wall-clock kill, network
calls blocked and filesystem writes blocked. On Linux that is `setrlimit`; on Windows it is a real
Job Object. Whatever the OS actually accepted is reported back, because a limit believed to be on
and silently absent is worse than no limit.

To be clear about what that is and is not: it contains accidents. A fixture that deletes a
directory, a benchmark that allocates 40 GB, a helper that opens a socket. It is **not** a security
boundary against hostile code, and no pure-Python sandbox is. For untrusted input, run it in a
container.

### 5. Watch seven channels

"Behaviour" is not just the return value. A narrow definition does not under-report, it lies,
because whatever sits outside the definition comes back as `no_change`.

1. The return value
2. The exception raised, if any
3. Warnings emitted
4. Anything written to stdout
5. Anything written to stderr
6. Mutation of the arguments you passed in
7. Changes to the object's own state, for methods

Each observation is reduced to a comparable key, with a depth cap so a self-referencing structure
cannot spin forever. Lazy things are forced: a generator that is never consumed compares equal to
any other generator, so comparing them without forcing compares nothing while reporting success.

### 6. Check the instrument before trusting the reading

The *before* version is run a second time, in a third fresh process, and compared against itself.

If it disagrees with itself, the comparison is meaningless and the tool abstains with that reason
rather than reporting a difference. This is what catches `uuid4()`, `os.getpid()`, `time.monotonic()`
and unseeded randomness. Those functions genuinely differ between any two runs, and a tool without
this gate would report every one of them as a regression on every commit.

There is a matching rule on the other side, learned the hard way: the measurement must not show up
in the answer. The two versions run out of two different temporary directories, so any function
that returns something derived from `__file__` differs no matter what its code does. Each side's
own paths are stripped before the comparison, and only its own, so a genuine change still reports.

### 7. Follow the blast radius

Starting from the functions that changed, RunBoth walks outward through the call graph and
executes the callers too, carrying the concrete values that were already proven to differ. Callers
that show no difference are pruned so the search does not explode.

This is the part with no equivalent in a diff-based reviewer. The caller's source did not change.
There is nothing to review. It still broke.

### 8. Say one of three things

- **`changed`**, with a witness: the exact arguments, what it used to do, what it does now.
- **`no_change at budget N`**: N generated inputs found no difference across seven channels.
- **`abstained`**, with a reason: it could not be checked, and here is why.

Three verdicts rather than two is the deliberate part. "Cannot tell" and "no difference" are
different claims, and collapsing them into a green check is how tools end up lying. RunBoth never
says "safe". It says what it measured and how hard it looked.

## What this means in practice

**The honest limit.** Sampling finds differences; it cannot prove their absence. `no_change at
budget 60` means sixty generated inputs, chosen around the function's own constants, produced
identical behaviour on seven channels. That is evidence. It is not a proof, and the report says so
in those words.

**Why it abstains.** Some functions cannot be honestly checked: they need an object the tool
cannot construct, they are nondeterministic, or they are too slow to run inside the time budget.
Those are reported as abstentions with reasons, never counted as passing. The abstention rate is a
real coverage number and the project publishes it rather than hiding it.

**Why there is no AI in it.** Every verdict is backed by an execution you can re-run yourself. A
witness is a fact about your code, not an opinion about it. That also means the marginal cost of
running it is close to zero, and nothing about your source leaves your machine.
