# Changelog

All notable changes to RunBoth. Dates are the day the work was measured, not the day it
was released.

## Unreleased

### Added

- **Progress output** during adjudication, on stderr. A run on a real repository takes
  minutes and used to print nothing at all, which is indistinguishable from a hang. stdout
  is untouched, so `--json` consumers see exactly what they saw before.
- **Control suite for the generated-`_version.py` stub** (`[6/6]` in `runboth selftest`),
  including the cases that must NOT fire.
- `RUNBOTH_NO_VERSION_STUB=1` to disable that stub.

### Fixed

- **A missing `_version.py` made whole packages unadjudicable.** setuptools_scm and similar
  tools generate `pkg/_version.py` at build time and gitignore it, so `git archive` never
  carries it, the package import raises `ModuleNotFoundError`, and every function in it
  abstains for a reason that has nothing to do with its behaviour. `materialise()` now
  writes the file the build would have written.

  Measured 2026-09-14 on humanize: **13.0% of functions adjudicated before, 82.6% after**,
  and the run went from zero findings to correctly catching `fractional(0)` changing from
  `'0/1'` to `'0'`. urllib3 could not be imported at all before, and imports now.

  Deliberately narrow: only a directory that is already a package, only when `_version.py`
  is absent, and only when a sibling imports it **outside** a `try/except`. A package that
  guards the import and falls back is already working, and stubbing it would change an
  answer that was never broken. dateutil does exactly that, and is left alone.

- **`scripts/hunt_regressions.py` could not see past 60 commits.** `previous_change` walked
  the last 60 commits touching a path and compared unparsed sources. On a file that changes
  constantly that window never reaches the commit that moved the function, so it returned
  "nothing found" rather than "I ran out of room". It now asks git directly with
  `-L <lines>:<path>`, taking line ranges from `ast`, so it never depends on git's funcname
  regex and needs no `.gitattributes` change in someone else's repository.

  Measured on more-itertools (more.py, 5,633 lines): the walk found nothing for five
  functions; `-L` found the real commit for all five and was **25x faster** (12.18s to
  0.49s). One of them, `only`, was changed 211 commits back along that path.

- **A timed-out suspect was reported as a clean one.** The hunt counted attempts, not
  verdicts, and `if not recs: continue` swallowed timeouts and empty results identically. A
  run could print "adjudicated 10 suspects, 0 leads" when nine of them never produced a
  verdict. It now names each one and separates attempted from adjudicated, because a count
  that hides its own failures is the thing this project exists to refuse.

### Performance

- `funcs_at` unparsed every function in a file to answer a question about one, and cached
  nothing. It now takes an `only` argument and memoises. Isolated benchmark on
  more-itertools: **66.01s to 28.92s** for three lookups.

## 0.1.0 - 2026-09-13

First public release.
