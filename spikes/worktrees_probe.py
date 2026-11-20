"""Test-only probe for the worktree runs L1-L6 (not part of calu).

Installed into the calu tool environment of the desktop container only, through a .pth file.
It wraps three functions after their modules load and appends one JSON line per call to
$CALU_WTL_PROBE_DIR/<pid>.jsonl (only if that directory exists):

- calu.checkers.render.render_step_cut: the step's checkout lines as a checker reads them;
- calu.session.Session.step_checkouts: which checkouts calu assigned a step to;
- calu.honesty.HonestyState.snapshot: the honesty state of one checkout when it was asked.

It changes no return value and swallows its own errors.
"""

import importlib.abc
import json
import os
import sys
import time

_DIR = os.environ.get("CALU_WTL_PROBE_DIR", "/tmp/wtl-probe")


def _log(kind, data):
    try:
        if not os.path.isdir(_DIR):
            return
        data = {"t": round(time.time(), 3), "kind": kind, **data}
        with open(os.path.join(_DIR, f"{os.getpid()}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(data, default=str) + "\n")
    except Exception:
        pass


def _patch_render(mod):
    orig = mod.render_step_cut

    def render_step_cut(step, workspace):
        out = orig(step, workspace)
        try:
            head = [
                ln
                for ln in out.text.splitlines()[:6]
                if ln.startswith(("Tool call", "Working", "Checkout", "Other checkouts"))
            ]
            _log("render", {"tool": step.tool_name, "agent": step.agent_id, "head": head})
        except Exception:
            pass
        return out

    mod.render_step_cut = render_step_cut


def _patch_session(mod):
    cls = mod.Session
    orig = cls.step_checkouts

    def step_checkouts(self, step):
        out = orig(self, step)
        try:
            _log(
                "checkouts",
                {
                    "tool": step.tool_name,
                    "agent": step.agent_id,
                    "cwd": step.cwd,
                    "input": str(
                        step.tool_input.get("command") or step.tool_input.get("file_path") or ""
                    )[:200],
                    "checkouts": [self.checkouts.label(c.root) for c in out],
                },
            )
        except Exception:
            pass
        return out

    cls.step_checkouts = step_checkouts


def _patch_honesty(mod):
    cls = mod.HonestyState
    orig = cls.snapshot

    def snapshot(self):
        out = orig(self)
        try:
            _log(
                "honesty",
                {
                    "root": str(self.root),
                    "baseline": None
                    if out.baseline is None
                    else str(getattr(out.baseline, "id", out.baseline))[:60],
                    "marks": [f"{m.path}:{m.symbol}" for m in out.marks],
                    "unreviewed": str(out.unreviewed)[:600],
                    "review_required": out.review_required,
                    "clean": out.clean,
                    "excluded": list(self.excluded),
                },
            )
        except Exception:
            pass
        return out

    cls.snapshot = snapshot


_PATCHES = {
    "calu.checkers.render": _patch_render,
    "calu.session": _patch_session,
    "calu.honesty": _patch_honesty,
}


class _Loader(importlib.abc.Loader):
    def __init__(self, inner, name):
        self.inner, self.name = inner, name

    def create_module(self, spec):
        return self.inner.create_module(spec)

    def exec_module(self, module):
        self.inner.exec_module(module)
        try:
            _PATCHES[self.name](module)
        except Exception as exc:
            _log("probe-error", {"module": self.name, "error": repr(exc)})


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name not in _PATCHES:
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            spec = getattr(finder, "find_spec", lambda *a: None)(name, path, target)
            if spec is not None:
                spec.loader = _Loader(spec.loader, name)
                return spec
        return None


sys.meta_path.insert(0, _Finder())
