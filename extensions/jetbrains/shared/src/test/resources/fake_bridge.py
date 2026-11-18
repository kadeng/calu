#!/usr/bin/env python3
"""A scripted `calu ide` for the plugin's tests: JSON-RPC 2.0 over NDJSON on stdin and stdout.

    plugin ──ide.initialize, ide.status, ide.sessions, ide.attach──► answers from fixed data
           ──session.call chat.send──► user.input, assistant.delta/text, tool.use (Bash),
                                        check.result ESCALATE, escalation.request   (notifications)
           ──session.call escalation.decide──► escalation.resolved, tool.result, turn.result

Options: `--cwd DIR` (reported as the workspace), `--locked` (requests that read the database
answer -32010 until `ide.unlock {password: "secret"}`), `--no-session` (`ide.attach` answers
-32011 and `ide.start` -32013), `--exit-after N` (exit with code 3 after N answered requests).
Not part of calu: it implements only what the tests ask.
"""

import argparse
import json
import os
import sys


def main() -> None:
    """Read requests from stdin until it closes and answer each on stdout."""
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="ide")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--locked", action="store_true")
    parser.add_argument("--no-session", action="store_true")
    parser.add_argument("--exit-after", type=int, default=0)
    args = parser.parse_args()
    state = {"seq": 0, "unlocked": [], "answered": 0, "events": [], "open": None,
             "checker": {"name": "command-default", "route": "command", "model": "", "version": 3,
                         "enabled": True, "instructions": "Deny what deletes files."}}

    def send(message: dict) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def event(method: str, **params: object) -> None:
        state["seq"] += 1
        recorded = {"method": method, "params": {**params, "seq": state["seq"]}}
        state["events"].append(recorded)
        send({"jsonrpc": "2.0", "method": "session.event", "params": recorded})

    def snapshot() -> dict:
        return {
            "state": "escalated" if state["open"] else "idle",
            "calu_session_id": "s-1",
            "workspace": args.cwd,
            "last_seq": state["seq"],
            "escalations": [state["open"]] if state["open"] else [],
            "agents": [],
            "queue": [],
            "auto_escalate": {"commands": False, "edits": False, "subagents": False},
        }

    def session_call(method: str, params: dict) -> dict:
        if method == "chat.send":
            event("session.state", state="running")
            event("user.input", text=params["text"], source="user", uuid="u-1")
            event("assistant.delta", text="I will ", message_id="m-1")
            event("assistant.delta", text="run a command.", message_id="m-1")
            event("assistant.text", text="I will run a command.", message_id="m-1")
            event("tool.use", tool_use_id="t-0", name="Edit", input={"file_path": args.cwd + "/a.py"})
            event("edit.diff", tool_use_id="t-0", path="a.py", before="x = 1\n", after="x = 2\n",
                  start_line=1, end_line=1)
            event("check.result", tool_use_id="t-0", tool_name="Edit", route="edit", decision="ALLOW",
                  step_summary="Changes x")
            event("tool.result", tool_use_id="t-0", content="edited", is_error=False)
            event("tool.use", tool_use_id="t-1", name="Bash", input={"command": "make test"})
            event("check.result", tool_use_id="t-1", tool_name="Bash", route="command",
                  decision="ESCALATE", reason="runs project code", step_summary="Runs the tests")
            state["open"] = {"escalation_id": "e-1", "tool_use_id": "t-1", "tool_name": "Bash",
                             "tool_input": {"command": "make test"}, "reason": "runs project code",
                             "rendered": "```\nmake test\n```"}
            event("escalation.request", **state["open"])
            return {}
        if method == "escalation.decide":
            state["open"] = None
            event("escalation.resolved", escalation_id=params["escalation_id"],
                  decision=params.get("decision", "deny"), note=params.get("note"))
            event("tool.result", tool_use_id="t-1", content="ok", is_error=False)
            event("turn.result", subtype="success", is_error=False, cost_usd=0.01, num_turns=1)
            event("session.state", state="idle")
            return {}
        if method == "usage.get":
            return {"context": {"tokens": 1000, "max_tokens": 200000, "percent": 0.5, "model": "fable"},
                    "rate_limits": [], "cost_usd": 0.01, "num_turns": 1}
        if method == "session.history":
            return {"turns": [], "steps": []}
        return {}

    def handle(method: str, params: dict) -> dict:
        needs_login = method in ("ide.sessions", "ide.attach", "ide.start", "ide.settings.get")
        if args.locked and needs_login and not state["unlocked"]:
            raise RpcError(-32010, "the 'operator' credential needs its password",
                           {"needs": "operator", "unlocked": []})
        if method == "ide.initialize":
            return {"bridge_version": "0.0-fake", "protocol_version": 1, "workspace": args.cwd,
                    "capabilities": ["cwd"]}
        if method == "ide.status":
            return {"findings": [{"name": "tmux", "ok": True, "level": "ok", "message": "", "advice": ""}],
                    "database": {"reachable": True, "place": "fake", "problem": ""},
                    "server": {"sessions_running": 0 if args.no_session else 1},
                    "locked": args.locked and not state["unlocked"], "unlocked": state["unlocked"]}
        if method == "ide.unlock":
            if params.get("password") != "secret":
                raise RpcError(-32010, "wrong password for the 'operator' credential",
                               {"needs": "operator", "unlocked": []})
            state["unlocked"] = ["operator", "admin"]
            return {"unlocked": state["unlocked"], "reading": True, "changing": True}
        if method == "ide.sessions":
            running = [] if args.no_session else [
                {"id": "s-1", "name": "", "directory": args.cwd, "state": "idle",
                 "claude_session_id": None, "open_escalations": 0, "started_at": "", "stale": False,
                 "here": True}]
            return {"running": running, "stored": []}
        if method == "ide.attach":
            if args.no_session:
                raise RpcError(-32011, "no session runs in this directory")
            after = int(params.get("after_seq") or 0)
            events = [e for e in state["events"] if e["params"]["seq"] > after]
            return {"attached": True, "session_id": "s-1", "initialize": {"workspace": args.cwd},
                    "snapshot": snapshot(), "events": events, "complete": True}
        if method == "ide.settings.get":
            listed = {k: v for k, v in state["checker"].items() if k != "instructions"}
            return {"schema": {}, "rows": [], "checkers": [listed], "projects": []}
        if method == "ide.checker.get":
            return dict(state["checker"])
        if method == "ide.checker.save":
            if params.get("base_version") not in (None, state["checker"]["version"]):
                raise RpcError(-32016, "the checker was changed elsewhere",
                               {"version": state["checker"]["version"],
                                "instructions": state["checker"]["instructions"]})
            state["checker"]["version"] += 1
            state["checker"]["instructions"] = params["instructions"]
            return {"version": state["checker"]["version"]}
        if method == "ide.start":
            raise RpcError(-32013, "no calu server answered; start one with `calu serve`")
        if method == "session.call":
            return session_call(params.get("method", ""), params.get("params") or {})
        return {}

    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        try:
            result = handle(request.get("method", ""), request.get("params") or {})
            send({"jsonrpc": "2.0", "id": request["id"], "result": result})
        except RpcError as error:
            body = {"code": error.code, "message": error.message}
            if error.data is not None:
                body["data"] = error.data
            send({"jsonrpc": "2.0", "id": request["id"], "error": body})
        state["answered"] += 1
        if request.get("method") == "ide.shutdown":
            return
        if args.exit_after and state["answered"] >= args.exit_after:
            sys.exit(3)


class RpcError(Exception):
    """An error answer: code, message and optional data."""

    def __init__(self, code: int, message: str, data: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


if __name__ == "__main__":
    main()
