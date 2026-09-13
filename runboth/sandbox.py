"""
THE SANDBOX: run a repository's real functions, imports and all, without trusting them.

Every abstention in `adjudicate` that reads "not constructible in isolation" is this file's fault
for not existing. Real code imports. A tool that only adjudicates functions reachable with
builtins alone will abstain on most of a customer's repository, and an abstention rate that high
is indistinguishable from not working.

# What this IS, stated before what it does

A **process-level sandbox**: a separate interpreter with enforced CPU, memory, file-size and
file-descriptor limits, network calls blocked, filesystem writes blocked, and a hard wall-clock
kill. It raises the cost of an accident by a lot.

It is **NOT a security boundary against hostile code**. Python cannot be made safe against a
determined attacker in-process; `ctypes`, `os.fork`, C extensions and a dozen other routes exist.
Anyone claiming otherwise about a pure-Python sandbox is wrong, and saying so here is cheaper than
being found out later. For untrusted input this must run inside a container or microVM, which is
exactly the layer an integration partner already operates.

What it IS good for, and what it is built for: running the code of a repository its owner already
trusts, with accidents contained. A test-suite fixture that deletes a directory, a benchmark that
allocates 40 GB, a helper that opens a socket in CI. Those are the realistic failures and all of
them are stopped.

# Why a subprocess and not an in-process guard

Three reasons, each learned rather than assumed:

* **Resource limits need a process.** RLIMIT_AS and RLIMIT_CPU apply to a process, and a runaway
  loop in-process takes the whole tool down with it.
* **Cross-process nondeterminism is invisible in-process.** The determinism gate proved this: hash
  ordering and object identity are stable inside one run and vary between runs. A fingerprint used
  across two CI runs is exactly the cross-process case, so measuring it needs two processes.
* **Import side effects do not accumulate.** Importing a module runs its top level. Doing that
  repeatedly in one interpreter means the tenth measurement sees state from the first nine.

# The controls

A sandbox that blocks nothing looks identical to one that works, right up until it matters. So the
suite deliberately runs code that MUST be stopped: a filesystem write, a network connection, an
infinite loop, and a large allocation. If any of those succeed, the sandbox is not a sandbox and
the harness says so instead of reporting results.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

DEFAULT_LIMITS = {
    "cpu_seconds": 10,
    "memory_mb": 1024,
    "wall_seconds": 30,
    "file_size_mb": 1,
    "open_files": 64,
}

# The worker. Runs in a fresh interpreter, applies its own limits, blocks what it can, imports the
# module under test for real, and reports outcomes on stdout as one JSON line.
_WORKER = r'''
import builtins, json, os, sys

try:
    import resource                  # POSIX only, and its absence is survivable. See _apply_limits.
except ImportError:
    resource = None

LIM = json.loads(sys.argv[1])
MOD_PATH = sys.argv[2]
ENTRY = sys.argv[3]
INPUTS = json.loads(sys.argv[4])
ROOT = sys.argv[5] if len(sys.argv) > 5 else ""
DOTTED = sys.argv[6] if len(sys.argv) > 6 else ""
EXTRA_ROOTS = json.loads(sys.argv[7]) if len(sys.argv) > 7 else []
TRACE = (len(sys.argv) > 8 and sys.argv[8] == "1")

# Line coverage of the module under test, recorded only when asked. `sys.settrace` costs real
# time, so the callers that only want observation keys never pay for it.
_COVERED = set()
_TRACE_FILE = os.path.realpath(MOD_PATH)


def _tracer(frame, event, arg):
    # Return the tracer only for the file we care about, so every other frame is dropped after
    # one call rather than traced line by line. That is the difference between "slower" and
    # "unusably slow" on a function that calls into a large library.
    if frame.f_code.co_filename == _TRACE_FILE or os.path.realpath(
            frame.f_code.co_filename) == _TRACE_FILE:
        if event == "line":
            _COVERED.add(frame.f_lineno)
        return _tracer
    return None


if ROOT:
    # A module extracted to a temp file loses its package. `from toolz.utils import ...` then
    # fails with ModuleNotFoundError and the function abstains for a reason that has nothing to
    # do with its behaviour. Putting the tree root on the path restores the package exactly.
    sys.path.insert(0, ROOT)
# ONE ROOT IS NOT ENOUGH UNDER THE src/ LAYOUT. A test at `tests/test_app.py` gets the repo root,
# and then `import flask` fails because flask lives at `src/flask`. The test file is not in the
# package and never will be, so its own root can never be the package's root. Every directory
# that CONTAINS a package goes on the path, which is what an installed checkout looks like and
# what pytest arranges for a developer. Two flask files abstained on exactly this.
for _r in EXTRA_ROOTS:
    if _r and _r not in sys.path:
        sys.path.insert(0, _r)

# ---- limits, applied to THIS process before anything from the repo is touched ----
#
# TWO PLATFORMS, ONE HONEST REPORT.
#
# `resource` does not exist on Windows, and this worker used to import it at the top, so on
# Windows the sandbox died before executing a single line of the repository's code. RunBoth did
# not run on Windows AT ALL, which is both the developer's own machine and a whole segment.
#
# The Windows path is a real Job Object, not a shrug. JOB_OBJECT_LIMIT_PROCESS_MEMORY makes an
# oversized allocation fail the same way RLIMIT_AS does, so the control that MUST stop a huge
# allocation passes on its own merits on both platforms instead of being excused on one.
#
# Whatever is actually applied is reported back in `enforced`. A limit believed to be on and
# silently absent is worse than no limit, because it is the same failure as every defect in this
# project: it presents as success.
ENFORCED = []

if resource is not None:
    _m = LIM["memory_mb"] * 1024 * 1024
    _f = LIM["file_size_mb"] * 1024 * 1024
    for _name, _res, _pair in (
            ("cpu",        resource.RLIMIT_CPU,    (LIM["cpu_seconds"], LIM["cpu_seconds"])),
            ("memory",     resource.RLIMIT_AS,     (_m, _m)),
            ("file_size",  resource.RLIMIT_FSIZE,  (_f, _f)),
            ("open_files", resource.RLIMIT_NOFILE, (LIM["open_files"], LIM["open_files"])),
            ("no_fork",    resource.RLIMIT_NPROC,  (0, 0)),
            ("no_core",    resource.RLIMIT_CORE,   (0, 0))):
        try:
            resource.setrlimit(_res, _pair)
            ENFORCED.append(_name)
        except Exception:
            pass                          # report the gap; never claim it
elif os.name == "nt":
    try:
        import ctypes
        from ctypes import wintypes

        class _BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _IO(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                        ("WriteOperationCount", ctypes.c_uint64),
                        ("OtherOperationCount", ctypes.c_uint64),
                        ("ReadTransferCount", ctypes.c_uint64),
                        ("WriteTransferCount", ctypes.c_uint64),
                        ("OtherTransferCount", ctypes.c_uint64)]

        class _EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BASIC),
                        ("IoInfo", _IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        # restypes are NOT optional here. A HANDLE is 64-bit and ctypes defaults to c_int, so
        # without these the handle is silently truncated and every call fails for a reason that
        # looks like a permissions problem.
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.CreateJobObjectW.restype = wintypes.HANDLE
        _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        _k32.GetCurrentProcess.restype = wintypes.HANDLE
        _k32.SetInformationJobObject.restype = wintypes.BOOL
        _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 wintypes.LPVOID, wintypes.DWORD]
        _k32.AssignProcessToJobObject.restype = wintypes.BOOL
        _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

        JOB = _k32.CreateJobObjectW(None, None)   # module-level: closing it drops the limits
        if not JOB:
            raise OSError("CreateJobObject failed")
        _info = _EXT()
        _info.ProcessMemoryLimit = LIM["memory_mb"] * 1024 * 1024
        _info.BasicLimitInformation.ActiveProcessLimit = 1            # no forking
        _info.BasicLimitInformation.PerProcessUserTimeLimit = LIM["cpu_seconds"] * 10000000
        #                             PROCESS_MEMORY | PROCESS_TIME | ACTIVE_PROCESS
        _info.BasicLimitInformation.LimitFlags = 0x00000100 | 0x00000002 | 0x00000008
        if not _k32.SetInformationJobObject(JOB, 9, ctypes.byref(_info), ctypes.sizeof(_info)):
            raise OSError("SetInformationJobObject failed")
        if not _k32.AssignProcessToJobObject(JOB, _k32.GetCurrentProcess()):
            raise OSError("AssignProcessToJobObject failed")
        ENFORCED += ["cpu", "memory", "no_fork"]
    except Exception:
        pass                              # ENFORCED stays empty and the caller is told so

# ---- policy: no writes, no network, no subprocesses ----
# RESOLVE THE TEMP DIRECTORY BEFORE WRITES ARE BLOCKED. `tempfile.gettempdir()` probes each
# candidate directory by CREATING a file in it; with writes blocked every probe raises, and the
# result is `FileNotFoundError: No usable temporary directory found`, raised at IMPORT time in
# any module that calls gettempdir() at module level (15 functions in click's tests, 2026-09-12),
# and the abstention blamed the repository. Resolving it once here caches `tempfile.tempdir`, so
# later calls return the cached path without probing. Writing INTO it is still blocked by
# `_guarded_open`, which is the property the sandbox control checks.
import tempfile as _tf
if not os.environ.get("RUNBOTH_CONTROL_NO_TEMPDIR_PRERESOLVE"):
    try:
        _tf.gettempdir()
    except Exception:
        pass
_real_open = builtins.open


def _guarded_open(file, mode="r", *a, **k):
    if any(c in mode for c in "wax+"):
        raise PermissionError("sandbox: filesystem writes are blocked")
    return _real_open(file, mode, *a, **k)


builtins.open = _guarded_open

import socket as _socket


def _no_net(*a, **k):
    raise PermissionError("sandbox: network access is blocked")


_socket.socket.connect = _no_net
_socket.socket.bind = _no_net
_socket.create_connection = _no_net

import subprocess as _sp


class _NoPopen(_sp.Popen):
    # A CLASS, NOT A FUNCTION. Replacing Popen with a bare function meant any module that
    # SUBCLASSES it failed to import with "function() argument 'code' must be code, not str",
    # and on Windows `asyncio.windows_utils` does exactly that at import time. Hypothesis
    # imports asyncio, so every test module in attrs (309 functions) and requests (242) came
    # back "import failed" and the tool blamed the repository. Found 2026-09-12 once the
    # abstention reason named its frame. Subclassing still works; constructing one still
    # raises, which is the property the sandbox control actually checks.
    def __init__(self, *a, **k):
        raise PermissionError("sandbox: subprocesses are blocked")


_sp.Popen = _NoPopen
os.system = _no_net
for _n in ("remove", "unlink", "rmdir", "rename", "mkdir", "makedirs"):
    if hasattr(os, _n):
        setattr(os, _n, _no_net)

# ---- load the module under test, with its real imports ----
import importlib
import importlib.util
try:
    if DOTTED:
        # BY DOTTED NAME, from inside the tree. `from . import x` needs a parent package, and a
        # module loaded from a temp file by spec_from_file_location has none: it fails with
        # "attempted relative import with no known parent package". That disqualified most modern
        # Python packages, which is a cap on every number this tool produces. Importing
        # `pkg.sub.mod` with the tree root on sys.path gives exactly the semantics a checkout has.
        mod = importlib.import_module(DOTTED)
    else:
        spec = importlib.util.spec_from_file_location("_under_test", MOD_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
except Exception as e:
    # NAME THE FRAME. "TypeError: function() argument 'code' must be code, not str" abstained
    # 309 functions on attrs and 242 on requests and said nothing about where; the innermost
    # frame is what turns that from a mystery into a fix. Path is shortened to its last two
    # parts so the reason string stays short and stable across machines.
    import traceback as _tb
    _frames = _tb.extract_tb(e.__traceback__)
    _where = ""
    if _frames:
        _f = _frames[-1]
        _where = " at %s:%s" % ("/".join(_f.filename.replace("\\", "/").split("/")[-2:]),
                                _f.lineno)
    print(json.dumps({"error": "import failed: %s: %s%s" % (type(e).__name__, e, _where)}))
    sys.exit(0)

# ENTRY is either a free function name, or `Class.method`. A method needs an OBJECT, and the
# object must be built the same way on both sides or the comparison compares nothing.
IS_METHOD = "." in ENTRY
if IS_METHOD:
    _cls_name, _meth_name = ENTRY.rsplit(".", 1)
    _cls = getattr(mod, _cls_name, None)
    if _cls is None:
        print(json.dumps({"error": "no class named %s" % _cls_name}))
        sys.exit(0)
    fn = None
else:
    fn = getattr(mod, ENTRY, None)
    if fn is None:
        print(json.dumps({"error": "no function named %s" % ENTRY}))
        sys.exit(0)


def observe(call, args=None, obj=None):
    """Run the call and capture EVERY OBSERVABLE CHANNEL, not just what it returns.

    # This is the definition of "behaviour", and getting it too narrow is how a checker lies

    A commit study measured runboth against the real test suites of real repositories and runboth
    LOST: on four commits the suite caught a regression runboth reported as `no_change`. The failing
    tests named the gap between them:

        test_file_args, test_file_atomics, test_file_lazy_mode, test_file_option
        test_iter_keepopenfile, test_iter_lazyfile
        test_echo_stdin_stream, test_runner_with_stream
        (and one commit whose only change was adding a DeprecationWarning)

    Files, streams, warnings. Not one of them is the return value, and the return value was all
    this looked at. A function that prints something different, mutates its argument differently,
    or leaves its object in a different state has CHANGED BEHAVIOUR, and calling that `no_change`
    is the single error this project cannot afford.

    # The channels, and why each is here rather than assumed harmless

        return value / exception    the original, and the only one most tools check
        WARNINGS                    a DeprecationWarning broke 16 click tests invisibly
        STDOUT and STDERR           a CLI library's entire output is here
        ARGUMENT MUTATION           `sort(xs)` versus `sorted(xs)` is invisible in the return
        OBJECT STATE                a method that leaves `self` different has done something

    Anything unequal on any channel is a difference. Channels that are empty on both sides cost
    nothing and collapse away, so ordinary pure functions produce exactly the key they did before
    and no existing verdict moves.
    """
    import io as _io
    import warnings as _w

    before_args = None
    if args is not None:
        try:
            before_args = [_state_of(a) for a in args]
        except Exception:
            before_args = None
    before_obj = _state_of(obj) if obj is not None else None

    out_buf, err_buf = _io.StringIO(), _io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    with _w.catch_warnings(record=True) as caught:
        _w.simplefilter("always")
        try:
            sys.stdout, sys.stderr = out_buf, err_buf
            value = call()
            k = key_of(value)           # inside, so a generator's prints are captured too
        finally:
            sys.stdout, sys.stderr = real_out, real_err

    extra = {}
    if caught:
        seen, tags = set(), []
        for item in caught:
            # Category and message only, never file or line: those move whenever anyone edits
            # above the call, and would report every reformat as a behaviour change.
            t = (item.category.__name__, str(item.message)[:120])
            if t not in seen:
                seen.add(t)
                tags.append(list(t))
        extra["warn"] = tags
    o, e = out_buf.getvalue()[:2000], err_buf.getvalue()[:2000]
    if o:
        extra["out"] = o
    if e:
        extra["err"] = e
    if before_args is not None:
        try:
            after_args = [_state_of(a) for a in args]
            if after_args != before_args:
                extra["mutated"] = [[b, a] for b, a in zip(before_args, after_args) if a != b][:4]
        except Exception:
            pass
    if obj is not None:
        after_obj = _state_of(obj)
        if after_obj != before_obj:
            # THE DELTA, NOT THE SNAPSHOTS. Recording [before, after] in full meant that when a
            # commit added one attribute in __init__, every method on that class came back
            # `changed`, because the baseline `self` differed between versions even though the
            # method did the identical thing to it. On real flask (HEAD~29..HEAD, 2026-09-12)
            # that produced 9 of 17 witnesses: add_template_global, teardown_appcontext, three
            # Blueprint.add_app_template_* methods, all blamed for `_got_first_request` and
            # friends. What a METHOD did to self is the set of attributes it changed, so only
            # those are recorded; a constructor's differences are the constructor's verdict.
            extra["self"] = _state_delta(before_obj, after_obj)

    if not extra:
        return k                        # the common case: identical key shape as before
    return ["eff", k, sorted(extra.items())]


def _state_delta(before, after):
    """Only the attributes the call changed. Falls back to both snapshots for non-objects."""
    if (isinstance(before, tuple) and isinstance(after, tuple) and len(before) == 2
            and len(after) == 2 and before[0] == "obj" and after[0] == "obj"):
        db, da = dict(before[1]), dict(after[1])
        return [[k, db.get(k, "<absent>"), da.get(k, "<absent>")]
                for k in sorted(set(db) | set(da)) if db.get(k) != da.get(k)]
    return [before, after]


def _state_of(v):
    """A comparable snapshot of a value's observable state. Never raises, never consumes."""
    import re as _re
    try:
        if hasattr(v, "__next__"):
            # Reading a generator to snapshot it would CONSUME the thing under test.
            return "<iterator>"
        if isinstance(v, (int, float, str, bytes, bool, type(None))):
            return repr(v)[:400]
        if isinstance(v, dict):
            return ("dict", sorted((repr(k)[:80], repr(x)[:80]) for k, x in list(v.items())[:64]))
        if isinstance(v, (list, tuple, set, frozenset)):
            return (type(v).__name__, [repr(x)[:80] for x in list(v)[:64]])
        d = getattr(v, "__dict__", None)
        if isinstance(d, dict):
            return ("obj", sorted((str(k)[:60], _re.sub(r"0x[0-9a-fA-F]+", "0xADDR",
                                                        repr(x)[:120]))
                                  for k, x in list(d.items())[:48]
                                  if not str(k).startswith("__")))
        return _re.sub(r"0x[0-9a-fA-F]+", "0xADDR", repr(v)[:400])
    except Exception as ex:
        return f"<unreadable {type(ex).__name__}>"


def key_of(v, _depth=0):
    """Same shape as Outcome.key in the parent: lazy values materialised, addresses removed."""
    import re
    # DEPTH IS CAPPED, because the parent has to json.loads whatever this emits. A value whose
    # iterator yields iterators (or a manager whose __enter__ returns another manager) recursed
    # here without limit, produced a payload nested thousands of levels deep, and the PARENT died
    # with RecursionError inside the JSON decoder, taking the whole adjudication run with it.
    # Found on real flask, 2026-09-12. Beyond this depth the shape is still recorded, so two
    # sides that differ only deeper than this come back as the same key: that is a bounded,
    # named blind spot, which is the trade this project always makes over a crash.
    if _depth > 12:
        return ["deep", type(v).__name__]
    # A CONTEXT MANAGER IS A DEFERRED COMPUTATION, exactly like a generator, and calling the
    # function that returns one runs NONE of its body. click's `isolated_filesystem` is a
    # @contextmanager; the commit that added a DeprecationWarning to it broke 16 tests; and runboth
    # reported no_change because it received the manager object, reprd it, scrubbed the address,
    # and got the same string on both sides. The body never ran, so there was nothing to compare.
    #
    # Entering it is not optional politeness, it is the only way the code under test executes.
    # Always exited in a finally, because leaving a manager open leaks whatever it acquired into
    # the next measurement, and cross-measurement leakage is the thing the subprocess exists to
    # prevent.
    if (hasattr(v, "__enter__") and hasattr(v, "__exit__")
            and not isinstance(v, (str, bytes, type))):
        try:
            inner = v.__enter__()
        except BaseException as e:
            return ["ctx_enter_exc", type(e).__name__]
        inner_key = None
        if inner is v:
            # `__enter__` returning self is the common idiom (Flask's AppContext, most
            # RequestContext-shaped objects). Recursing into it re-enters the SAME manager
            # again and again until the depth cap, pushing twelve nested contexts as a side
            # effect and producing a witness that reads ['ctx', ['ctx', ['ctx', ... on both
            # sides. Found on real flask 2026-09-12. The manager is recorded as entered, once.
            inner_key = ["self"]
        else:
            try:
                inner_key = key_of(inner, _depth + 1)
            except BaseException as e:
                inner_key = ["exc", type(e).__name__]
        try:
            v.__exit__(None, None, None)
        except BaseException as e:
            return ["ctx", inner_key, ["exit_exc", type(e).__name__]]
        return ["ctx", inner_key]
    if hasattr(v, "__next__") and not isinstance(v, (str, bytes)):
        items = []
        try:
            for i, item in enumerate(v):
                if i >= 512:
                    items.append("<truncated>")
                    break
                items.append(key_of(item, _depth + 1))
        except Exception as e:
            items.append(["exc", type(e).__name__])
        return ["lazy", items]
    if isinstance(v, float) and v != v:
        return ["float", "nan"]
    try:
        r = repr(v)
    except Exception:
        return ["val", "<unreprable>"]
    if re.search(r" at 0x[0-9a-fA-F]+", r):
        return ["opaque", type(v).__name__]
    return ["val", r]


out = []
if IS_METHOD:
    # Each trial is [ctor_args, method_args]. A FRESH INSTANCE per call, because a method may
    # mutate self and the second call on one object would see different state, which reads as
    # nondeterminism and is not.
    for pair in INPUTS:
        ctor_args, meth_args = pair[0], pair[1]
        try:
            # A ctor argument written as ["__build__", "ClassName"] means: construct that class
            # with no arguments and pass the instance. The parameter was named after its type and
            # a class of that name exists; a literal would have produced an object that never
            # resembled the real one.
            # THE OBJECT MAY NOT LIVE IN THIS MODULE, and insisting that it does was the single
            # biggest remaining abstention: `AppContext(app)` needs a Flask, which is defined two
            # files away. The extended form carries the module to import it from and the arguments
            # to build it with, both mined from real calls in the repository rather than invented.
            #   ["__build__", "Name"]                        same module, no arguments
            #   ["__build__", "Name", "dotted.mod", [args]]  anywhere in the tree
            built = []
            for _a in ctor_args:
                if isinstance(_a, list) and len(_a) >= 2 and _a[0] == "__build__":
                    _src = mod
                    if len(_a) >= 3 and _a[2]:
                        _src = importlib.import_module(_a[2])
                    _bargs = _a[3] if len(_a) >= 4 and _a[3] else []
                    built.append(getattr(_src, _a[1])(*_bargs))
                else:
                    built.append(_a)
            obj = _cls(*built)
        except BaseException as e:
            # Construction failing IS observable behaviour, tagged so the caller can tell a
            # class that never built from a method that never differed.
            out.append(["ctor", type(e).__name__])
            continue
        try:
            out.append(observe(lambda: getattr(obj, _meth_name)(*meth_args),
                               args=meth_args, obj=obj))
        except BaseException as e:
            out.append(["exc", type(e).__name__])
else:
    if TRACE:
        sys.settrace(_tracer)
    try:
        for args in INPUTS:
            try:
                out.append(observe(lambda a=args: fn(*a), args=args))
            except BaseException as e:
                out.append(["exc", type(e).__name__])
    finally:
        if TRACE:
            sys.settrace(None)

# DID WE ACTUALLY REACH THE CODE THAT CHANGED?
#
# Without this, `no_change at budget 60` can mean "sixty inputs ran, not one of them reached the
# line you edited, and I am reporting that nothing changed". That is false confidence, and it is
# the exact error this project forbids everywhere else: never conflate "cannot tell" with "no".
#
# Tracing is off unless the caller asks for it, because sys.settrace is slow and most callers
# only want the keys. When it is on, the tracer is installed around the SAME batch, so the lines
# reported are the lines these inputs really executed.
if TRACE:
    _covered = sorted(_COVERED)
else:
    _covered = None
print(json.dumps({"keys": out, "enforced": ENFORCED, "covered": _covered}))
'''


def package_roots(root, max_depth=3):
    """Every directory in the tree that CONTAINS a top-level package, plus the root itself.

    This is what an installed checkout looks like on sys.path, and what pytest arranges for a
    developer running the suite. Shallow by design: a package nested four levels down is vendored
    or a fixture, and putting its parent on the path would shadow real modules with test doubles.
    """
    skip = {"node_modules", "__pycache__", ".git", "build", "dist", ".tox", ".venv"}
    out = [str(Path(root).resolve())]

    def holds_package(d):
        try:
            return any((c / "__init__.py").exists() for c in d.iterdir() if c.is_dir())
        except OSError:
            return False

    frontier = [(Path(root), 0)]
    while frontier:
        cur, depth = frontier.pop()
        if depth >= max_depth:
            continue
        try:
            kids = [c for c in cur.iterdir()
                    if c.is_dir() and not c.name.startswith(".") and c.name not in skip]
        except OSError:
            continue
        for d in kids:
            # NEVER PUT A PACKAGE ITSELF ON sys.path. A directory with its own __init__.py is a
            # package, and adding it promotes every module INSIDE it to top level, where they
            # shadow the standard library. `src/flask` qualified under the old rule because
            # `src/flask/sansio/` is a package, and the result was flask's own `json/` and
            # `globals.py` masking the real ones: eight functions abstained with "partially
            # initialized module 'typing'", a circular import that had nothing to do with them.
            #
            # The correct set is directories that CONTAIN a package without BEING one, which is
            # exactly what an installed checkout puts on the path.
            if holds_package(d) and not (d / "__init__.py").exists():
                out.append(str(d.resolve()))
            frontier.append((d, depth + 1))

    seen, uniq = set(), []
    for r in out:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq[:8]


# What the last worker actually enforced. Diagnostic only, never part of a verdict, but the
# controls read it so that "the sandbox is weaker here" is something the tool SAYS instead of
# something a user discovers.
LAST_ENFORCED = None


def _worker_env():
    """A deliberately bare environment for the worker, plus what Windows cannot run without.

    A minimal env is the point: the repository's code should not inherit credentials or proxies.
    But on Windows an interpreter with no SYSTEMROOT fails to import `socket` and `ssl`, and that
    surfaces as an abstention blaming the repository for a fault that is entirely ours.
    """
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    # A HOME DIRECTORY MUST EXIST OR IMPORTS DIE. `Path.home()` and `expanduser("~")` raise
    # RuntimeError("Could not determine home directory") with these unset, and a surprising
    # number of libraries resolve a config or cache path at import time. Found 2026-09-12 on
    # A real ML training repo, where it abstained 18 of 36 functions with an import error
    # that blamed the repository. Same shape as the `subprocess.Popen` and `tempfile.gettempdir`
    # defects: OUR hardening broke THEIR import. These are paths, not credentials, and the
    # stated threat model is already "contains accidents, not attackers", so passing them
    # changes nothing about what a hostile repo could reach.
    for _k in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA"):
        if _k in os.environ:
            env[_k] = os.environ[_k]
    # Lets the selftest re-break the over-blocking defects on purpose, to prove those three
    # controls discriminate. A control nobody has watched fail is an assumption.
    for _k in ("RUNBOTH_CONTROL_NO_TEMPDIR_PRERESOLVE",):
        if os.environ.get(_k):
            env[_k] = os.environ[_k]
    if os.name == "nt":
        for k in ("SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "PATHEXT", "COMSPEC",
                  "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"):
            if k in os.environ:
                env[k] = os.environ[k]
    return env


def run_module_fn(module_path, entry, inputs, limits=None, root=None, dotted=None,
                  extra_roots=None, trace=False):
    """Call `entry` in `module_path` on each input, inside the sandbox.

    Returns (keys, error), or (keys, error, covered_lines) when `trace` is set. Tracing is opt-in
    because sys.settrace is slow, and only the coverage-qualified verdict needs it.

    One subprocess for the whole batch: import cost is paid once, and no state from a previous
    measurement can leak into the next because the process is new.
    """
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    with tempfile.NamedTemporaryFile("w", suffix="_worker.py", delete=False) as w:
        w.write(_WORKER)
        worker = w.name
    try:
        proc = subprocess.run(
            [sys.executable, worker, json.dumps(lim), str(module_path), entry,
             json.dumps(inputs, default=repr), str(root or ""), str(dotted or ""),
             json.dumps(extra_roots or []), "1" if trace else "0"],
            capture_output=True, text=True, timeout=lim["wall_seconds"],
            cwd=tempfile.gettempdir(), env=_worker_env())
    except subprocess.TimeoutExpired:
        return None, f"killed at the {lim['wall_seconds']}s wall clock"
    except OSError as e:
        return None, f"could not start the sandbox: {type(e).__name__}"
    finally:
        try:
            os.unlink(worker)
        except OSError:
            pass
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return None, f"sandbox produced no output (exit {proc.returncode})"
    try:
        payload = json.loads(line[-1])
    except json.JSONDecodeError:
        return None, "sandbox output was not JSON"
    except (RecursionError, ValueError, MemoryError) as e:
        # The worker caps its own nesting now, but the parent must never die on a payload it did
        # not produce. One function's output is one abstention, never the end of the run.
        return None, f"sandbox output could not be decoded ({type(e).__name__})"
    global LAST_ENFORCED
    LAST_ENFORCED = payload.get("enforced")
    if "error" in payload:
        return None, payload["error"]
    if trace:
        return payload.get("keys"), "", payload.get("covered")
    return payload.get("keys"), ""


# ---------------------------------------------------------------------------------------
# CONTROLS. A sandbox that blocks nothing looks exactly like one that works.
# ---------------------------------------------------------------------------------------

_CASES = [
    ("ordinary function with a real import",
     "import math\n\n\ndef f(n):\n    return math.floor(n) + 1\n", "f", [[3.7]], "allowed"),
    # __ESCAPE__ is substituted with a real path literal at run time. It used to be hardcoded
    # to /tmp, so on Windows the write control tested nothing that could have succeeded anyway.
    ("filesystem WRITE must be blocked",
     "def f(n):\n    open(__ESCAPE__, 'w').write('x')\n    return 1\n",
     "f", [[1]], "blocked"),
    ("network MUST be blocked",
     "import socket\n\n\ndef f(n):\n    s = socket.socket()\n    s.connect(('1.1.1.1', 80))\n"
     "    return 1\n", "f", [[1]], "blocked"),
    ("subprocess MUST be blocked",
     "import subprocess\n\n\ndef f(n):\n    subprocess.Popen(['ls'])\n    return 1\n",
     "f", [[1]], "blocked"),
    ("file deletion MUST be blocked",
     "import os\n\n\ndef f(n):\n    os.remove(__ESCAPE__)\n    return 1\n",
     "f", [[1]], "blocked"),
    ("infinite loop MUST be killed",
     "def f(n):\n    while True:\n        pass\n", "f", [[1]], "killed"),
    ("huge allocation MUST be stopped",
     "def f(n):\n    return len(bytearray(8 * 1024 * 1024 * 1024))\n", "f", [[1]], "blocked"),
    # THE OTHER DIRECTION, AND IT IS THE DANGEROUS ONE. Every case above asks whether the
    # sandbox stops something. These ask whether it stops TOO MUCH, because over-blocking does
    # not announce itself: the module fails to IMPORT, or the function raises identically in
    # both versions, and the verdict comes back `no_change` over a measurement that never
    # reached the code. That is a FALSE SAME, the one error this project cannot afford, and it
    # is what `tempfile.gettempdir()` and a function-shaped `subprocess.Popen` each produced
    # for a month (2026-09-12: 15 click modules and 551 attrs+requests functions respectively).
    ("reading a temp dir path MUST still work",
     "import tempfile\n\n\ndef f(n):\n    return len(tempfile.gettempdir()) > 0\n",
     "f", [[1]], "allowed"),
    ("SUBCLASSING Popen MUST still work (asyncio does it at import)",
     "import subprocess\n\n\nclass _P(subprocess.Popen):\n    pass\n\n\n"
     "def f(n):\n    return _P.__name__\n", "f", [[1]], "allowed"),
    ("importing asyncio MUST still work (hypothesis pulls it in)",
     "def f(n):\n    import asyncio\n    return asyncio.__name__\n", "f", [[1]], "allowed"),
    ("a home directory MUST resolve (libraries read config at import)",
     "from pathlib import Path\n\n\ndef f(n):\n    return len(str(Path.home())) > 0\n",
     "f", [[1]], "allowed"),
]


def run_controls():
    print("  SANDBOX CONTROLS: each of these MUST be stopped, or this is not a sandbox\n")
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="runboth_sbx_"))
    escape = Path(tempfile.gettempdir()) / "runboth_sandbox_escape"
    if escape.exists():
        escape.unlink()
    for label, src, entry, inputs, expected in _CASES:
        p = tmp / "m.py"
        # repr(), not quoting by hand: a Windows path is full of backslashes and pasting one
        # into source as a bare literal produces a different path or a syntax error.
        p.write_text(textwrap.dedent(src).replace("__ESCAPE__", repr(str(escape))),
                     encoding="utf-8")
        # A TIGHT CLOCK IS PART OF THE TEST for the cases that must be STOPPED (the infinite
        # loop has to be killed by something), and pure noise for the cases that must be
        # ALLOWED, where the only question is whether ordinary code still runs. Giving the
        # allowed cases the same 8 seconds made them lose CPU races: on 2026-09-12 the
        # "subclassing Popen" control failed with "killed at the 8s wall clock" purely because
        # a measurement run was using every core. A control that fails under load is flaky, and
        # a flaky control is worse than no control, because it teaches people to ignore red.
        lim = ({"cpu_seconds": 20, "wall_seconds": 60, "memory_mb": 512}
               if expected == "allowed"
               else {"cpu_seconds": 3, "wall_seconds": 8, "memory_mb": 512})
        keys, err = run_module_fn(p, entry, inputs, lim)
        if expected == "allowed":
            good = err == "" and keys is not None and keys[0][0] == "val"
            detail = err or f"returned {keys[0] if keys else None}"
        elif expected == "killed":
            good = keys is None or (keys and keys[0][0] == "exc")
            detail = err or f"returned {keys[0] if keys else None}"
        else:
            raised = keys is not None and keys and keys[0][0] == "exc"
            good = raised or keys is None
            detail = err or f"raised {keys[0][1] if raised else keys}"
        ok = ok and good
        print(f"  {'PASS' if good else 'FAIL':<5} {label:<42} {detail[:44]}")
    if escape.exists():
        print("\n  FAIL: the escape file EXISTS. A write got through and nothing else here counts.")
        ok = False
        escape.unlink()
    # SAY WHAT WAS ACTUALLY ENFORCED, per platform. The cases above prove the POLICY blocks,
    # and those are pure Python that behaves the same everywhere. The resource limits are the
    # operating system's job, they differ by platform, and their absence is invisible until
    # something runs away. So the level is reported instead of assumed.
    if LAST_ENFORCED is not None:
        print(f"\n  resource limits enforced on {sys.platform}: "
              f"{', '.join(LAST_ENFORCED) or 'NOTHING'}")
        if not LAST_ENFORCED:
            print("  FAIL: not one resource limit could be applied on this platform.")
            ok = False
    print()
    return ok



# ---------------------------------------------------------------------------------------
# Whole-module comparison. This is what turns an abstention into a verdict on real code.
# ---------------------------------------------------------------------------------------

def dotted_name(root, rel_path):
    """Return (dotted_module_name, sys_path_root), or (None, None).

    THE PACKAGE ROOT IS NOT THE REPO ROOT. Flask lives at `src/flask/...`, so naming the module
    `src.flask.sansio.app` is wrong and importing it fails exactly as a temp file did. The real
    root is found by walking UP from the module while `__init__.py` keeps existing; the last
    directory that still has one is the top of the package, and its PARENT is what belongs on
    sys.path.
    """
    p = str(rel_path).replace("\\", "/")
    if not p.endswith(".py"):
        return None, None
    full = Path(root) / p
    if not full.exists():
        return None, None
    parts = p[:-3].split("/")
    is_init = parts[-1] == "__init__"
    if is_init:
        parts = parts[:-1]
    if not parts:
        return None, None
    # FIND THE HIGHEST ANCESTOR THAT IS A PACKAGE, not the first one that is not.
    #
    # The previous version walked up while `__init__.py` kept existing and stopped at the first
    # directory without one. That is wrong under PEP 420: `src/flask/sansio/` has NO __init__.py
    # and is an implicit namespace package, so the walk stopped instantly and produced the module
    # name `app` with `.../sansio` on sys.path. Importing `app` then fails on its own
    # `from .helpers import ...`, which is exactly the error this whole fix was meant to remove.
    #
    # The package top is the HIGHEST directory in the chain that still has an __init__.py. Its
    # PARENT is the sys.path root, and everything below it is the dotted name. For
    # src/flask/sansio/app.py that gives `flask.sansio.app` rooted at `src`, which is what a
    # checkout of Flask actually looks like.
    ancestors = []
    d = full.parent
    root_p = Path(root)
    while True:
        ancestors.append(d)
        if d == root_p or d.parent == d:
            break
        d = d.parent
    top = None
    for a in ancestors:                      # nearest first
        if (a / "__init__.py").exists():
            top = a                          # keep going: we want the HIGHEST such directory
    if top is None:
        return parts[-1], str(full.parent)
    # OFF BY ONE FOR `__init__.py`, and it cost the whole of markupsafe. `depth` counts
    # DIRECTORIES from the sys.path root down to the module's folder, so the dotted name needs
    # depth+1 trailing parts when the last one is a module basename, and exactly depth when it is
    # not, which is the case after `__init__` has been stripped. Getting this wrong produced
    # `src.markupsafe` and every function in the package abstained with
    # `ModuleNotFoundError: No module named 'src'`. The src/ layout is the modern Python default,
    # so this was not an edge case, it was most of the ecosystem.
    depth = len(full.parent.relative_to(top.parent).parts)
    take = depth if is_init else depth + 1
    dotted = ".".join(parts[len(parts) - take:])
    return dotted, str(top.parent)


_TREE_SENTINEL = "<tree>"


def _path_variants(p):
    """Every spelling one path can wear inside an observed value.

    A path reaches a key through whatever the function did with it, so the same directory shows up
    as a native string, as posix (`Path.as_posix()`, which is what most code writing a shell line
    uses), and as a backslash-escaped form when it has been through `repr()`. Missing any one
    spelling leaves the artifact in place, which is the whole bug.
    """
    p = str(p)
    if not p:
        return []
    fwd, back = p.replace("\\", "/"), p.replace("/", "\\")
    return [v for v in dict.fromkeys([p, fwd, back, back.replace("\\", "\\\\")]) if v]


def _strip_measurement_paths(obj, roots):
    """Replace THIS side's own materialisation directories with a fixed sentinel.

    Why this exists, and why it is not cheating. The two versions of a function are executed out of
    two different temporary trees, because that is the only way to have both versions on disk at
    once. Any function whose observable output embeds its own location therefore differs between
    the runs NO MATTER WHAT ITS CODE DOES: the difference is manufactured by the measurement, not
    found by it. Reporting it is a false positive of the worst kind, one the tool creates itself.

    Found on 2026-09-12 by the CI job that runs RunBoth on RunBoth: it reported `pre_hook_script`
    as changed on a commit that never touched it, because the function bakes its package root into
    the hook it writes. The whole class is "function returns something derived from __file__",
    which in a repository of developer tooling is not rare at all.

    Only the root PREFIX is replaced, so everything underneath still compares. A genuine change
    from `{root}/ledger` to `{root}/lib` survives normalisation and is still reported.
    """
    # BOTH SPELLINGS OF EVERY ROOT. On Windows a path can reach the observed value in its
    # RESOLVED long form while the harness recorded the 8.3 short form it got from mkdtemp, and
    # then no substring matches and nothing is stripped. Found 2026-09-12 by this project's own
    # CI, on the GitHub Windows runner and nowhere else: the runner's user is `runneradmin`,
    # which is over eight characters and therefore has a `RUNNER~1` alias, while the developer's
    # own `info` does not and never reproduced it.
    expanded = []
    for r in roots:
        if not r:
            continue
        expanded.append(str(r))
        try:
            expanded.append(str(Path(r).resolve()))
        except (OSError, ValueError):
            pass

    pairs = []
    for r in dict.fromkeys(expanded):
        pairs.extend((v, _TREE_SENTINEL) for v in _path_variants(r))
    # Longest first: a tree root and its own parent can both be in the list, and replacing the
    # short one first would leave a half-substituted path that never matches the other side.
    pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
    if not pairs:
        return obj

    def walk(o):
        if isinstance(o, str):
            for needle, repl in pairs:
                if needle in o:
                    o = o.replace(needle, repl)
            return o
        if isinstance(o, list):
            return [walk(x) for x in o]
        if isinstance(o, tuple):
            return tuple(walk(x) for x in o)
        if isinstance(o, dict):
            return {walk(k): walk(v) for k, v in o.items()}
        return o

    return walk(obj)


# `.*?` and not `[^>]*`, because a qualified name contains `>` itself: the repr of a closure is
# `<function Blueprint.add_app_template_test.<locals>.register_template at 0xADDR>`, and a
# character class excluding `>` stops dead at `<locals>`. The trailing address anchor is what
# keeps the non-greedy match from running past the end of one function's repr.
_FUNC_REPR_RE = __import__("re").compile(
    r"<(?:function|bound method|built-in function|built-in method)\s+.*?\s+at\s+0xADDR>")


def differs_only_by_a_function_name(before, after):
    """True when two observations are identical except for the NAME of a function object.

    Found by running the hunt over flask's history. A commit renamed an inner helper from
    `register_template` to `register_template_filter`, and the object-state channel reported a
    behaviour change on three `Blueprint` methods, because the closure gets stored in
    `deferred_functions` and its `repr` carries its qualified name.

    Strictly, that IS observable: `deferred_functions[0].__name__` really did change. But no
    caller compares it, and a gate that blocks a commit for renaming a local function is a gate
    that gets uninstalled the same afternoon. So it takes the same treatment this project already
    gives `0` versus `0.0`: still REPORTED, never blocking.

    Deliberately narrow. It only collapses when the two sides match after erasing function names
    entirely, so a stored function being replaced by a different KIND of value, or one appearing
    or disappearing, is untouched.
    """
    b, a = str(before), str(after)
    if b == a:
        return False
    return _FUNC_REPR_RE.sub("<function>", b) == _FUNC_REPR_RE.sub("<function>", a)


def changed_lines(before_node, after_node):
    """Absolute line numbers, in each version, of the lines that actually differ.

    Diffing the two function bodies line by line and mapping back to file line numbers, so the
    result is directly comparable with what the tracer reports. Only the lines that MOVED count:
    a function with one edited line has one changed line, not forty.
    """
    import ast as _ast
    import difflib as _dl

    def body(node):
        try:
            src = _ast.unparse(node)
        except Exception:
            return None, None
        return src.splitlines(), getattr(node, "lineno", None)

    b_lines, b_start = body(before_node)
    a_lines, a_start = body(after_node)
    if b_lines is None or a_lines is None or b_start is None or a_start is None:
        return set(), set()

    b_changed, a_changed = set(), set()
    sm = _dl.SequenceMatcher(None, b_lines, a_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for i in range(i1, i2):
            b_changed.add(b_start + i)
        for j in range(j1, j2):
            a_changed.add(a_start + j)
    return b_changed, a_changed


def union_coverage(before_changed, after_changed, before_covered, after_covered):
    """(covered_old + covered_new) / (changed_old + changed_new), or None when nothing changed.

    Deliberately the same formula DiffTestGen defines, so a comparison against their published
    numbers is honest rather than a redefinition in our favour.

    Why this exists at all: without it `no_change at budget 60` can mean "sixty inputs ran, not
    one of them reached the line you edited, and I am reporting that nothing changed". That is the
    one error this project forbids everywhere else, and it was unmeasured until 2026-09-13.
    """
    total = len(before_changed) + len(after_changed)
    if total == 0:
        return None
    hit = len(before_changed & set(before_covered or ())) + \
        len(after_changed & set(after_covered or ()))
    return hit / total


def _witness_rank(before, after, args):
    """Lower is more convincing. The witness is the product's entire persuasive surface.

    Every differing input is equally true, and they are NOT equally legible. Measured on a real
    frontier-model refactor of `toolz.tail` that claimed behaviour preservation:

        tail(-6, 2)        raised ValueError   ->  returned ()
        tail(0, [1, 2, 3]) returned [1, 2, 3]  ->  returned []

    Both are real. The first invites an argument about whether anyone calls `tail` with an int
    for a sequence. The second is a silently wrong answer on the most ordinary input imaginable,
    and it ends the argument. The engine used to report whichever came first in the generated
    order, which was the weaker one roughly whenever the generator happened to emit it earlier.

    Nothing is dropped or hidden by this: the verdict is `changed` either way, and the count is
    unaffected. It only decides WHICH true example gets printed.
    """
    b, a = str(before), str(after)
    b_exc = b.startswith("['exc") or b.startswith("['ctor")
    a_exc = a.startswith("['exc") or a.startswith("['ctor")

    if not b_exc and not a_exc:
        rank = 0        # a wrong VALUE, the case nobody can wave away
    elif b_exc != a_exc:
        rank = 1        # an error became a value, or a value became an error
    else:
        rank = 2        # one exception type became another

    # Within a rank, prefer the argument list a reader recognises: short, plain literals.
    # `tail(0, [1, 2, 3])` reads as something a caller would write; a 400-character nested
    # structure reads as fuzzer output, which is the reaction that loses the argument.
    shape = len(repr(args))
    return (rank, shape)


def best_witness(inputs, before_keys, after_keys):
    """The most legible differing input, or None when nothing differs."""
    diffs = [(args, x, y) for args, x, y in zip(inputs, before_keys, after_keys) if x != y]
    if not diffs:
        return None
    args, x, y = min(diffs, key=lambda d: _witness_rank(d[1], d[2], d[0]))
    return {"args": [repr(a) for a in args], "before": str(x), "after": str(y)}


def compare_in_sandbox(before_src, after_src, entry, params, budget=200, limits=None,
                       before_root=None, after_root=None, rel_path=None, extra=None,
                       before_node=None, after_node=None):
    """Compare one function across two versions of its MODULE, imports and all.

    `adjudicate` builds a function in isolation with only builtins, which abstains the moment the
    module imports anything. Here the whole module is written out and imported for real inside the
    sandbox, so the function under test gets its actual dependencies.

    Returns a verdict dict in the same contract as everywhere else. Both sides are run in the SAME
    input order in two separate processes, so a difference in the key sequence is a difference in
    behaviour and nothing else.
    """
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).parent))
    from engine import make_inputs, mine_from_source

    # Kept BEFORE the dotted-import block below reassigns them to the sys.path roots. Both the
    # tree root and the import root have to be stripped, and they are not always the same
    # directory, so each side collects every path it is measured under.
    _before_paths = [p for p in (before_root,) if p]
    _after_paths = [p for p in (after_root,) if p]

    rec = {"function": entry, "rung": f"sampled({budget})", "budget": budget,
           "witness": None, "reason": None}

    # DID WE REACH WHAT CHANGED? Tracing costs real time, so it is only switched on when the
    # caller supplied both AST nodes, which is the only case where "the changed lines" is a
    # question that can be answered. Set RUNBOTH_NO_COVERAGE=1 to turn it off entirely.
    b_changed, a_changed = (changed_lines(before_node, after_node)
                            if (before_node is not None and after_node is not None)
                            else (set(), set()))
    want_coverage = bool(b_changed or a_changed) and os.environ.get("RUNBOTH_NO_COVERAGE") != "1"
    cov_b = cov_a = None

    # WITNESS PROPAGATION. When a callee was proven to differ at a specific input, the values in
    # that witness are the most informative constants in existence for testing this caller, and
    # generating blindly around them wastes the one piece of hard evidence already in hand. They
    # are merged into the mined pool rather than replacing it: a caller usually transforms its
    # arguments before passing them down, so the exact value may not reach the callee, and
    # narrowing the search to it would trade a blind generator for a superstitious one.
    mined = mine_from_source(before_src, after_src)
    if extra:
        ei, ef, es = mined
        xi = [v for v in extra if isinstance(v, int) and not isinstance(v, bool)]
        xf = [v for v in extra if isinstance(v, float)]
        xs = [v for v in extra if isinstance(v, str)]
        mined = (tuple(ei) + tuple(xi), tuple(ef) + tuple(xf), tuple(es) + tuple(xs))
    inputs = [list(t) for t in make_inputs(params, budget, 0, mined)]
    if not inputs:
        return {**rec, "verdict": "abstained", "reason": "no inputs could be generated"}

    # When the module's own path in the tree is known, use the tree copy directly and import by
    # dotted name. The materialised trees ALREADY hold the right version of every file, so no temp
    # copy is needed and the package structure is intact.
    dotted = sys_b = sys_a = None
    if rel_path and before_root and after_root:
        dotted, sys_b = dotted_name(before_root, rel_path)
        _d2, sys_a = dotted_name(after_root, rel_path)
        if dotted and _d2 == dotted:
            pb = Path(before_root) / rel_path
            pa = Path(after_root) / rel_path
            if not (pb.exists() and pa.exists()):
                dotted = None
        else:
            dotted = None
    if dotted:
        before_root, after_root = sys_b, sys_a
    if not dotted:
        d = tempfile.mkdtemp(prefix="runboth_cmp_")
        pb, pa = Path(d) / "before.py", Path(d) / "after.py"
        pb.write_text(before_src, encoding="utf-8")
        pa.write_text(after_src, encoding="utf-8")
    # The two sides are literally named before.py and after.py on this path, so a function that
    # reports its own __file__ would differ on the filename alone. Same artifact, same treatment.
    _before_paths += [p for p in (before_root, pb) if p]
    _after_paths += [p for p in (after_root, pa) if p]

    rb = package_roots(before_root) if before_root else []
    ra = package_roots(after_root) if after_root else []

    # THE WALL CLOCK COVERS THE WHOLE BATCH, so a slow function abstains on the input COUNT
    # rather than on anything about the function. That was 179 of 367 abstentions in the
    # 2026-09-12 measurement, 123 of them click alone, whose tests drive CLI runners.
    #
    # Fewer inputs is weaker evidence, never wrong evidence, and this project already has the
    # vocabulary for that: the verdict carries its budget. So on a timeout the comparison is
    # retried with a fraction of the inputs and the rung says exactly how many actually ran.
    # `sampled(8)` is an honest, useful verdict; an abstention on a function nobody could check
    # is neither. BOTH SIDES ARE ALWAYS RESTARTED ON THE SAME SLICE, because comparing 80
    # before-inputs against 8 after-inputs would be meaningless.
    attempts = [inputs]
    if len(inputs) > 8:
        attempts.append(inputs[:max(8, len(inputs) // 8)])
    if len(inputs) > 40:
        attempts.append(inputs[:4])

    kb = ka = None
    for attempt, use in enumerate(attempts):
        # run_module_fn returns two values without tracing and three with it, and every other
        # caller in the tree relies on the two-value form. Unpacking three unconditionally broke
        # every case where the source is IDENTICAL, which is precisely the blast-radius case the
        # product exists for. Caught by the suite, not by inspection.
        _r = run_module_fn(pb, entry, use, limits, before_root, dotted, rb, trace=want_coverage)
        kb, eb = _r[0], _r[1]
        cov_b = _r[2] if len(_r) > 2 else None
        if eb:
            if "wall clock" in eb and attempt + 1 < len(attempts):
                continue
            return {**rec, "verdict": "abstained", "reason": f"before: {eb}"}
        _r = run_module_fn(pa, entry, use, limits, after_root, dotted, ra, trace=want_coverage)
        ka, ea = _r[0], _r[1]
        cov_a = _r[2] if len(_r) > 2 else None
        if ea:
            if "wall clock" in ea and attempt + 1 < len(attempts):
                continue
            return {**rec, "verdict": "abstained", "reason": f"after: {ea}"}
        if len(use) != len(inputs):
            rec = {**rec, "rung": f"sampled({len(use)})", "budget": len(use),
                   "slow": f"reduced from {len(inputs)} inputs to fit the "
                           f"{(limits or {}).get('wall_seconds', DEFAULT_LIMITS['wall_seconds'])}s budget"}
        inputs = use
        break
    if kb is None or ka is None or len(kb) != len(ka):
        return {**rec, "verdict": "abstained", "reason": "sandbox returned mismatched results"}

    # DETERMINISM, CROSS-PROCESS, FOR FREE. The before-version is run a second time in a fresh
    # process. If it disagrees with itself the comparison is meaningless, and this is the tier the
    # in-process gate provably cannot reach: hash ordering and identity are stable within a run.
    kb2, eb2 = run_module_fn(pb, entry, inputs, limits, before_root, dotted, rb)
    if eb2 or kb2 != kb:
        return {**rec, "verdict": "abstained",
                "reason": "before version differs from itself across processes "
                          "(hash ordering, identity, or ambient state)"}

    # Each side's own measurement paths come out before the comparison, never the other side's:
    # stripping a path that only one version produces would hide a real change.
    nb = _strip_measurement_paths(kb, _before_paths)
    na = _strip_measurement_paths(ka, _after_paths)

    best = best_witness(inputs, nb, na)
    if best is not None:
        # A witness PROVES the changed code was reached, whatever the tracer says, so coverage
        # is only ever used to qualify silence.
        return {**rec, "verdict": "changed", "witness": best,
                "reason": "behaviour differs"}

    cov = union_coverage(b_changed, a_changed, cov_b, cov_a) if want_coverage else None
    if cov is not None:
        rec = {**rec, "change_coverage": round(cov, 3)}
        if cov == 0.0:
            # NEVER REACHED THE CHANGE. Reporting `no_change` here would be the exact error this
            # project forbids: silence that sounds like a verdict. It is an abstention.
            return {**rec, "verdict": "abstained",
                    "reason": f"none of the {len(inputs)} generated inputs executed the changed "
                              f"lines, so nothing was compared where it matters"}
        return {**rec, "verdict": "no_change",
                "reason": f"no difference found in {len(inputs)} inputs (sandboxed), "
                          f"covering {cov:.0%} of the changed lines"}
    return {**rec, "verdict": "no_change",
            "reason": f"no difference found in {len(inputs)} inputs (sandboxed)"}


if __name__ == "__main__":
    sys.exit(0 if run_controls() else 1)
