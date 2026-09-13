"""
ONE COMMAND, AND EVERY EDIT IN THIS REPOSITORY IS ADJUDICATED FOREVER.

    runboth install .

That installs both enforcement points, because they catch different things at different times
and neither one subsumes the other:

    runtime hook   fires on every file write, reports the blast radius into the agent's context
                   seconds after the edit, while the agent can still act on it. Never blocks.
    git gate       fires on every commit, from any agent or human, and BLOCKS a behaviour change
                   with the input that proves it. This is the one that cannot be forgotten.

# Why the runtime hook is per-harness and the git gate is not

The git gate needs nothing from anyone: `git commit` is `git commit`. The runtime hook has to be
registered with whatever is doing the editing, and each harness spells that differently.

Claude Code's PostToolUse contract is implemented here and verified. For everything else the
honest answer is that the MCP server plus the git gate already cover it, and inventing a config
format for a harness whose schema has not been checked would be a lie shaped like a feature.

# It merges. It does not clobber.

An existing `.claude/settings.json` is read, appended to, and written back with its other keys
intact. A tool that overwrites a team's settings file has done real damage, and the fact that it
also installed something useful will not be what anyone remembers.
"""

import json
import sys
from pathlib import Path

CLAUDE_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"


def _python():
    """The interpreter the hook should run under, which is THIS one and not whatever is on PATH."""
    return sys.executable or "python3"


def install_claude_hook(repo, py_dir, scope="project"):
    """Register agent_hook.py as a Claude Code PostToolUse hook. Returns (path, note)."""
    if scope == "user":
        settings = Path.home() / ".claude" / "settings.json"
    else:
        settings = Path(repo) / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)

    data = {}
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            # Refuse rather than guess. Rewriting a settings file this cannot parse would destroy
            # whatever the user actually had in it.
            return None, f"{settings} exists and is not valid JSON; not touching it"
    if not isinstance(data, dict):
        return None, f"{settings} is not a JSON object; not touching it"

    cmd = f'"{_python()}" "{Path(py_dir) / "agent_hook.py"}" --stdin'
    hooks = data.setdefault("hooks", {})
    post = hooks.setdefault("PostToolUse", [])
    if not isinstance(post, list):
        return None, "hooks.PostToolUse is not a list; not touching it"

    for entry in post:
        for h in (entry.get("hooks") or []):
            if "agent_hook.py" in str(h.get("command", "")):
                h["command"] = cmd                      # already installed: refresh the path
                settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                return settings, "already installed; path refreshed"

    post.append({"matcher": CLAUDE_MATCHER,
                 "hooks": [{"type": "command", "command": cmd, "timeout": 20}]})
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return settings, ""


def uninstall_claude_hook(repo, scope="project"):
    settings = (Path.home() / ".claude" / "settings.json") if scope == "user" \
        else (Path(repo) / ".claude" / "settings.json")
    if not settings.exists():
        return False
    try:
        data = json.loads(settings.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return False
    post = (data.get("hooks") or {}).get("PostToolUse") or []
    keep = [e for e in post
            if not any("agent_hook.py" in str(h.get("command", ""))
                       for h in (e.get("hooks") or []))]
    if len(keep) == len(post):
        return False
    data["hooks"]["PostToolUse"] = keep
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True
