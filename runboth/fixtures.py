"""
MINE THE REPO FOR REAL CONSTRUCTOR CALLS. The last capability limit, and the asset nobody used.

`App(...) could not be constructed on any generated input; signature is (import_name,
static_url_path, static_folder, ...)` is where Flask stopped. No generator invents a plausible
`import_name`, and it should not try: guessing at a constructor is how you get a green result over
an object that never resembled the real thing.

But the repository already knows. Somewhere in its tests and examples, someone wrote `Flask(
"test")` or `App(__name__)`, and that call is a fact about how the class is really built. Mining
those is strictly better than generating, because they are the arguments the library's own authors
use.

# What is mined, and what is refused

Only calls whose arguments are LITERALS or trivially reconstructible names. `Flask(__name__)`
becomes `Flask("<mined>")`, because `__name__` at the call site is a module name and any string
serves. `App(config.load())` is refused: reproducing it would mean executing arbitrary setup, and
a constructor argument obtained by running unknown code is not a fixture, it is a side effect.

# Why this is not cheating

The comparison still runs both versions on the SAME constructed object. Mining only decides which
objects get built. A mined argument that fails to construct is discarded exactly like a generated
one, and if nothing constructs, the verdict is still an abstention naming the signature.
"""

import ast
import pathlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Names that appear as constructor arguments constantly and mean "some module name here".
_SYNTHETIC = {"__name__": "<mined>", "__file__": "<mined>.py", "__package__": "<mined>"}


def _literal(node):
    """A constructor argument this can reproduce, or None if it cannot."""
    if isinstance(node, ast.Constant):
        return node.value if not isinstance(node.value, (bytes, complex)) else None
    if isinstance(node, ast.Name) and node.id in _SYNTHETIC:
        return _SYNTHETIC[node.id]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        vals = [_literal(e) for e in node.elts]
        if any(v is None for v in vals):
            return None
        return list(vals) if isinstance(node, ast.List) else vals
    if isinstance(node, ast.Dict):
        ks = [_literal(k) for k in node.keys]
        vs = [_literal(v) for v in node.values]
        if any(x is None for x in ks + vs):
            return None
        return dict(zip(ks, vs))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _literal(node.operand)
        return -inner if isinstance(inner, (int, float)) else None
    return None


def mine_constructions(root, class_name, limit=12, max_files=1500):
    """Every reproducible `ClassName(...)` call in the tree, as a list of positional arg lists.

    Keyword arguments are dropped and only the positionals kept, because a constructor called with
    keywords in the wild is still usually satisfiable positionally, and threading kwargs through
    the sandbox boundary adds a serialisation format for very little gain.
    """
    found, seen = [], set()
    for i, f in enumerate(sorted(Path(root).rglob("*.py"))):
        if i > max_files or len(found) >= limit:
            break
        try:
            src = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if class_name not in src:
            continue
        try:
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            continue
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == class_name):
                continue
            args = [_literal(a) for a in n.args]
            if any(a is None for a in args):
                continue
            key = repr(args)
            if key in seen:
                continue
            seen.add(key)
            found.append(args)
            if len(found) >= limit:
                break
    return found


def subclasses_of(root, class_name, max_files=1500):
    """(subclass_name, path_relative_to_root) for classes inheriting `class_name`.

    THE PATH IS THE POINT. Flask's `Flask(App)` lives in `src/flask/app.py`, while the method under
    test is in `src/flask/sansio/app.py`. Returning only a name is useless: to construct the
    subclass the sandbox must import ITS module, which transitively imports the changed one from
    the materialised tree, so the modified method is the one that runs.
    """
    out = []
    for i, p in enumerate(sorted(pathlib.Path(root).rglob("*.py"))):
        if i > max_files:
            break
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError, OSError):
            continue
        for n in tree.body:
            if isinstance(n, ast.ClassDef) and any(
                    isinstance(b, ast.Name) and b.id == class_name for b in n.bases):
                try:
                    rel = str(p.relative_to(root)).replace("\\", "/")
                except ValueError:
                    continue
                out.append((n.name, rel))
    return out


def mine_for_class(root, class_name, limit=12):
    """Constructions of the class, falling back to its SUBCLASSES when it is never built directly.

    Flask's `App` has zero direct constructions in its own repository: nobody instantiates the
    base, they instantiate `Flask`, which subclasses it with the same signature. Refusing to look
    at subclasses would leave every abstract-ish base class unconstructible forever, which is the
    common shape in real object-oriented code.

    A subclass argument list is a HINT, not a guarantee: the subclass may have widened the
    signature. It is tried, and if construction fails it is discarded like any other candidate.
    """
    direct = mine_constructions(root, class_name, limit)
    if direct:
        return direct, class_name
    for sub, _rel in subclasses_of(root, class_name)[:4]:
        got = mine_constructions(root, sub, limit)
        if got:
            return got, sub
    return [], None


def find_class(root, class_name, max_files=2000):
    """(rel_path, node) for a class defined ANYWHERE in the tree, nearest the top first."""
    hits = []
    for i, p in enumerate(sorted(Path(root).rglob("*.py"))):
        if i > max_files:
            break
        if any(part in (".git", "__pycache__", "node_modules") for part in p.parts):
            continue
        # A DEFINITION FOUND IN A TEST FILE IS THE WRONG DEFINITION. flask's tests/test_config.py
        # defines `class Flask(flask.Flask)` as a fixture and click's tests define `class Color`,
        # and because test files sit one directory down while the package sits under src/, the
        # "nearest the top" sort picked the fixture over the real class every time. The plan then
        # said "import test_config and build Flask", which is not the object the code ships.
        # Mining CONSTRUCTIONS from tests is right (those calls are facts about usage); resolving
        # a DEFINITION to a test file is not. Matched against the path relative to the root, so a
        # repository that itself lives under a directory called tests is unaffected.
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            rel_parts = p.parts
        if any(part in ("tests", "test", "testing") or part.startswith("test_")
               or part.endswith("_test.py") for part in rel_parts):
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if f"class {class_name}" not in src:
            continue
        try:
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef) and n.name == class_name:
                try:
                    rel = str(p.relative_to(root)).replace("\\", "/")
                except ValueError:
                    continue
                hits.append((rel, n))
                break
    hits.sort(key=lambda h: (h[0].count("/"), len(h[0])))
    return hits[0] if hits else (None, None)


def cross_module_plan(root, module_src, class_name):
    """Build each object-shaped constructor argument from ANYWHERE in the repository.

    This is the biggest single abstention left on a real codebase, and the reason is mundane:
    `AppContext(app)` needs a Flask, `Lexer(environment)` needs an Environment, and both are
    defined in a different file. `object_arg_plan` could only see classes in the module under
    test, so every one of those came back "could not be constructed on any generated input".

    Each unsatisfiable parameter is matched BY NAME to a class in the tree, which is the naming
    convention in essentially all Python, and the class is then built from arguments MINED from
    real calls, falling back to a subclass when the base is never instantiated directly. Nothing
    is invented: an argument that cannot be reproduced from a literal is refused, exactly as
    before, because an object assembled from guesses is not evidence about anything.

    Returns [["__build__", Name, dotted, args], ...] or None. One level deep, deliberately: if
    the candidate itself needs an object, this gives up. Recursion here means constructing an
    arbitrary object graph out of guesses, and the depth at which that stops being evidence is
    exactly one.
    """
    from sandbox import dotted_name
    try:
        tree = ast.parse(module_src)
    except (SyntaxError, ValueError):
        return None
    params = None
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == class_name:
            for it in n.body:
                if isinstance(it, ast.FunctionDef) and it.name == "__init__":
                    # REQUIRED PARAMETERS ONLY, the same rule `ctor_params` applies. The first
                    # version planned over every parameter, so click's `Context(command,
                    # parent=None, info_name=None, ... color=None)` produced a 16-slot plan with
                    # two objects and fourteen Nones, and the Nones were passed POSITIONALLY over
                    # the author's defaults. A parameter with a default already has the value its
                    # author chose; the plan's job is the ones that have to be supplied.
                    a = it.args
                    pos = list(a.posonlyargs) + list(a.args)
                    pos = pos[1:] if pos and pos[0].arg in ("self", "cls") else pos
                    required = pos[:len(pos) - len(a.defaults)] if a.defaults else pos
                    params = [p.arg for p in required]
            break
    if not params:
        return None

    plan, any_hit = [], False
    for p in params:
        want = p.lstrip("_").replace("_", "")
        rel, node = None, None
        for cand in (want, want.capitalize(), "".join(w.capitalize() for w in p.split("_"))):
            rel, node = find_class(root, cand)
            if rel:
                want = cand
                break
        if not rel:
            plan.append(None)
            continue
        args, via = mine_for_class(root, want)
        # A base class nobody instantiates directly is normal in real code; `mine_for_class`
        # already falls back to a subclass, and the SUBCLASS IS WHAT GETS BUILT. It lives in its
        # own file, so the dotted name has to be recomputed for it: importing `flask.sansio.app`
        # and asking for `Flask` finds nothing, because Flask is defined in `flask.app`.
        name = via or want
        if name != want:
            sub_rel, _ = find_class(root, name)
            rel = sub_rel or rel
        dotted, _sysroot = dotted_name(root, rel)
        if args:
            plan.append(["__build__", name, dotted or "", list(args[0])])
            any_hit = True
        elif node is not None and _no_arg_ctor(node):
            plan.append(["__build__", name, dotted or "", []])
            any_hit = True
        else:
            plan.append(None)
    return plan if any_hit else None


def _no_arg_ctor(node):
    init = next((i for i in node.body
                 if isinstance(i, ast.FunctionDef) and i.name == "__init__"), None)
    return init is None or len(init.args.args) == 1


def object_arg_plan(module_src, class_name, root=None):
    """For a constructor whose arguments are OBJECTS, name a class that could supply each one.

    `Lexer(environment)` cannot be built from literals, and refusing to guess is right: passing a
    string where an Environment belongs would construct something that never resembled the real
    object. But the parameter is NAMED after its type, which is overwhelmingly the convention in
    Python, and a class with that name usually sits in the same module or package.

    So each unsatisfiable parameter gets a CANDIDATE CLASS, matched by name, and the sandbox is
    told to build that first. One level only: if the candidate itself needs an object, this gives
    up. Recursion here would mean constructing an arbitrary object graph from guesses, and the
    depth at which that stops being evidence is exactly one.
    """
    import ast as _ast
    try:
        tree = _ast.parse(module_src)
    except (SyntaxError, ValueError):
        return None
    classes = {n.name: n for n in _ast.walk(tree) if isinstance(n, _ast.ClassDef)}
    params = None
    for n in _ast.walk(tree):
        if isinstance(n, _ast.ClassDef) and n.name == class_name:
            for it in n.body:
                if isinstance(it, _ast.FunctionDef) and it.name == "__init__":
                    params = [p.arg for p in it.args.args[1:]]
            break
    if not params:
        return None
    plan = []
    for p in params:
        want = p.lstrip("_").replace("_", "")
        hit = None
        for cname, cnode in classes.items():
            if cname.lower() == want.lower():
                # only if IT can be built with no arguments
                init = next((i for i in cnode.body
                             if isinstance(i, _ast.FunctionDef) and i.name == "__init__"), None)
                if init is None or len(init.args.args) == 1:
                    hit = cname
                break
        plan.append(hit)
    return plan if any(plan) else None


if __name__ == "__main__":
    import json
    root, cls = sys.argv[1], sys.argv[2]
    out, via = mine_for_class(root, cls)
    print(f"(mined via {via})" if via and via != cls else "")
    print(json.dumps(out, indent=2)[:900])
    print(f"{len(out)} reproducible constructions of {cls} found in {root}")
