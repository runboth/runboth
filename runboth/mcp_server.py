"""
MCP SERVER: the one adapter that reaches every agent harness worth reaching.

    vibe mcp add runboth --transport stdio --command python3 --arg /path/to/runboth/mcp_server.py

Agent harnesses speak MCP, so one server reaches all of them: no per-harness plugin, no
partnership required, and the same vendor-neutral contract every other entry point in this tree
emits.

# Why this is stdlib-only, deliberately

An MCP server that needs `pip install` before an agent can call it will not be installed. This is
JSON-RPC 2.0 over stdin and stdout, which is a hundred lines of `json` and a read loop, and it
runs on any Python 3.12 with no wheel, no lockfile and no network.

# The three tools, and why exactly three

    runboth_check_edit    working tree against HEAD. THE ONE AN AGENT CALLS after it edits.
    runboth_adjudicate    two revisions. For a PR gate.
    runboth_merge_check   two branches against a base. The defect that only exists in the merge.

Anything else an agent might want is a different question with a different answer, and a tool
surface that sprawls is one an agent picks from badly.

# What every response carries, and what it never says

Verdict, rung, budget, and either a witness input or a reason. It never returns "same": it returns
`no_change` with the budget attached, because measured false-same is 12% at 20 inputs and 4% at
400. An agent that reads "safe" will stop checking; one that reads "no difference found in 80
inputs" has been told the truth.
"""

import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PROTOCOL = "2024-11-05"
NAME = "runboth"
VERSION = "0.1.0"

TOOLS = [
    {
        "name": "runboth_check_edit",
        "description": (
            "Check whether uncommitted edits in a git repository changed observable BEHAVIOUR, "
            "and report which downstream callers change too. Call this after editing code. "
            "Returns a witness input for every behaviour change. Never claims code is safe: it "
            "reports 'no difference found in N inputs', which is evidence and not proof."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "path to the git repository"},
                "budget": {"type": "integer", "default": 80,
                           "description": "inputs per comparison; higher is slower and stronger"},
                "downstream": {"type": "boolean", "default": True,
                               "description": "also check callers of anything that changed"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "runboth_adjudicate",
        "description": (
            "Adjudicate every changed function between two git revisions. Each result is "
            "no_change, changed (with the input that proves it), or abstained (with the reason "
            "it could not be checked). Use for a pull-request gate."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "base": {"type": "string", "description": "base revision"},
                "head": {"type": "string", "description": "head revision"},
                "budget": {"type": "integer", "default": 200},
            },
            "required": ["repo", "base", "head"],
        },
    },
    {
        "name": "runboth_merge_check",
        "description": (
            "Find a defect that exists in NEITHER branch against the base and only appears in the "
            "merge. Two agents editing concurrently can each be individually correct while their "
            "merged result is wrong; no per-PR review examines that state."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "base": {"type": "string"},
                "branch_a": {"type": "string"},
                "branch_b": {"type": "string"},
                "budget": {"type": "integer", "default": 200},
            },
            "required": ["repo", "base", "branch_a", "branch_b"],
        },
    },
]


def _summary(records):
    piles = {"no_change": 0, "changed": 0, "abstained": 0}
    for r in records:
        piles[r.get("verdict", "abstained")] = piles.get(r.get("verdict", "abstained"), 0) + 1
    return piles


def call_tool(name, args):
    """Every tool returns the same contract. Nothing here may raise: an agent gets a result."""
    try:
        if name == "runboth_check_edit":
            from hook import run
            code, notes = run(args["repo"], int(args.get("budget", 80)),
                              bool(args.get("downstream", True)))
            return {"behaviour_changed": bool(notes), "changes": notes,
                    "rung": f"sampled({args.get('budget', 80)})",
                    "note": ("no difference found at this budget; that is evidence, not proof"
                             if not notes else
                             f"{len(notes)} function(s) changed behaviour, each with a witness")}
        if name == "runboth_adjudicate":
            from adjudicate import adjudicate
            recs, err = adjudicate(args["repo"], args["base"], args["head"],
                                   int(args.get("budget", 200)))
            return {"error": err} if err else {"summary": _summary(recs), "results": recs}
        if name == "runboth_merge_check":
            from mergecheck import merge_check
            rows, notes = merge_check(args["repo"], args["base"], args["branch_a"],
                                      args["branch_b"], int(args.get("budget", 200)))
            hits = [r for r in rows if r.get("interagent")]
            return {"inter_agent_regressions": len(hits), "hits": hits,
                    "adjudicated": len(rows), "notes": notes}
        return {"error": f"no such tool: {name}"}
    except Exception as e:
        # A tool that raises out of an MCP server takes the agent's turn with it. The failure is
        # data, like every other abstention in this project.
        return {"error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc(limit=3).splitlines()[-1]}


def respond(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid, params = req.get("method"), req.get("id"), req.get("params") or {}

        if method == "initialize":
            respond(rid, {"protocolVersion": PROTOCOL,
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": NAME, "version": VERSION}})
        elif method == "notifications/initialized":
            continue                      # a notification has no id and takes no reply
        elif method == "tools/list":
            respond(rid, {"tools": TOOLS})
        elif method == "tools/call":
            out = call_tool(params.get("name", ""), params.get("arguments") or {})
            respond(rid, {"content": [{"type": "text", "text": json.dumps(out, indent=2)}],
                          "isError": "error" in out})
        elif method == "ping":
            respond(rid, {})
        elif rid is not None:
            respond(rid, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
