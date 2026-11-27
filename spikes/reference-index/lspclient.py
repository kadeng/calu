"""Minimal LSP stdio client: references for every sample function.

Usage: lspclient.py OUT.json [--root DIR] [--sample FILE] [--ids S01,S02] [--raw FILE]
                    [--settle SECONDS] -- SERVER ARGS...
Prints timings (measured, wall clock) and the server's resident memory to stderr.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from enclosing import enclosing


class Server:
    def __init__(self, cmd, root, log):
        self.p = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, cwd=root
        )
        self.id = 0
        self.t0 = time.time()
        self.pending = {}
        self.cv = threading.Condition()
        self.notes = []
        self.requests_seen = []
        threading.Thread(target=self._read, daemon=True).start()

    def _send(self, msg):
        body = json.dumps(msg).encode()
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.p.stdin.flush()

    def _read(self):
        f = self.p.stdout
        while True:
            length = None
            while True:
                line = f.readline()
                if not line:
                    with self.cv:
                        self.pending["__eof__"] = True
                        self.cv.notify_all()
                    return
                line = line.strip()
                if not line:
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            msg = json.loads(f.read(length))
            if "method" in msg and "id" in msg:  # request from the server
                self.requests_seen.append(msg["method"])
                if msg["method"] == "workspace/configuration":
                    result = [None for _ in msg["params"]["items"]]
                else:
                    result = None
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            elif "method" in msg:
                self.notes.append((round(time.time() - self.t0, 3), msg))
            else:
                with self.cv:
                    self.pending[msg["id"]] = msg
                    self.cv.notify_all()

    def request(self, method, params, timeout=600):
        self.id += 1
        i = self.id
        self._send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        end = time.time() + timeout
        with self.cv:
            while i not in self.pending:
                if "__eof__" in self.pending:
                    raise RuntimeError("server closed its output")
                left = end - time.time()
                if left <= 0:
                    raise TimeoutError(method)
                self.cv.wait(left)
            msg = self.pending.pop(i)
        if "error" in msg:
            raise RuntimeError(json.dumps(msg["error"]))
        return msg["result"]

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def rss_kb(self):
        """Sum of VmRSS and VmHWM of the server process and its children, in kB."""
        pids = [self.p.pid]
        try:
            kids = subprocess.run(
                ["pgrep", "-P", str(self.p.pid)], capture_output=True, text=True
            ).stdout.split()
            pids += [int(k) for k in kids]
        except OSError:
            pass
        rss = hwm = 0
        for pid in pids:
            try:
                for line in open(f"/proc/{pid}/status"):
                    if line.startswith("VmRSS:"):
                        rss += int(line.split()[1])
                    if line.startswith("VmHWM:"):
                        hwm += int(line.split()[1])
            except OSError:
                pass
        return rss, hwm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--root", default="/work/repo")
    ap.add_argument("--sample", default="/out/sample.json")
    ap.add_argument("--ids", default="")
    ap.add_argument("--raw", default="")
    ap.add_argument("--settle", type=float, default=0.0)
    ap.add_argument("--extra-root", default="")
    ap.add_argument("--method", default="textDocument/references")
    ap.add_argument("--include-declaration", action="store_true")
    ap.add_argument("--ready-log", default="", help="regex of a window/logMessage to wait for")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    root = os.path.abspath(a.root)
    sample = json.load(open(a.sample))
    if a.ids:
        want = set(a.ids.split(","))
        sample = [s for s in sample if s["id"] in want]

    t0 = time.time()
    srv = Server(cmd, root, open(a.out + ".stderr", "wb"))
    folders = [{"uri": Path(root).as_uri(), "name": "repo"}]
    if a.extra_root:
        folders.append({"uri": Path(a.extra_root).as_uri(), "name": "extra"})
    init = srv.request(
        "initialize",
        {
            "processId": os.getpid(),
            "rootUri": Path(root).as_uri(),
            "rootPath": root,
            "workspaceFolders": folders,
            "capabilities": {
                "workspace": {"configuration": True, "workspaceFolders": True},
                "textDocument": {"references": {}, "synchronization": {}, "publishDiagnostics": {}},
                "window": {"workDoneProgress": True},
            },
        },
    )
    srv.notify("initialized", {})
    t_init = time.time() - t0
    info = init.get("serverInfo")
    t_ready = None
    if a.ready_log:
        import re

        end = time.time() + 300
        while time.time() < end and t_ready is None:
            for t, m in list(srv.notes):
                if m["method"] == "window/logMessage" and re.search(
                    a.ready_log, m["params"]["message"]
                ):
                    t_ready = t
            time.sleep(0.01)
    if a.settle:
        time.sleep(a.settle)

    opened = set()
    refs, raw, times = {}, {}, []
    first = None
    for s in sample:
        path = os.path.join(root, s["path"])
        uri = Path(path).as_uri()
        if uri not in opened:
            srv.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": {".ts": "typescript", ".sh": "shellscript"}.get(
                            os.path.splitext(path)[1], "python"
                        ),
                        "version": 1,
                        "text": open(path, encoding="utf-8").read(),
                    }
                },
            )
            opened.add(uri)
        q0 = time.time()
        try:
            res = srv.request(
                a.method,
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": s["line"] - 1, "character": s["col"]},
                    "context": {"includeDeclaration": a.include_declaration},
                },
            )
        except (RuntimeError, TimeoutError) as e:
            print(s["id"], "ERROR", e, file=sys.stderr)
            refs[s["id"]] = None
            continue
        dt = time.time() - q0
        if first is None:
            first = time.time() - t0
        times.append(dt)
        raw[s["id"]] = res
        if res is None:
            refs[s["id"]] = None
            continue
        seen, out = set(), []
        for loc in res:
            fpath = loc["uri"].replace("file://", "")
            line = loc["range"]["start"]["line"] + 1
            rel = os.path.relpath(fpath, root)
            if rel == s["path"] and line == s["line"]:
                continue  # the definition itself
            key = (rel, line)
            if key in seen:
                continue
            seen.add(key)
            out.append({"file": rel, "line": line, "in": enclosing(fpath, line)})
        out.sort(key=lambda r: (r["file"], r["line"]))
        refs[s["id"]] = out
    rss, hwm = srv.rss_kb()
    json.dump(refs, open(a.out, "w"), indent=1)
    if a.raw:
        json.dump(raw, open(a.raw, "w"), indent=1)
    ts = sorted(times)
    stats = {
        "server": info,
        "initialize_s": round(t_init, 3),
        "start_to_first_answer_s": round(first, 3) if first else None,
        "ready_log_at_s": t_ready,
        "queries": len(times),
        "query_median_s": round(ts[len(ts) // 2], 4) if ts else None,
        "query_max_s": round(ts[-1], 3) if ts else None,
        "query_mean_after_first_s": round(sum(times[1:]) / max(1, len(times) - 1), 4),
        "total_s": round(time.time() - t0, 3),
        "server_rss_kb": rss,
        "server_peak_rss_kb": hwm,
        "server_requests_seen": sorted(set(srv.requests_seen)),
        "total_refs": sum(len(v) for v in refs.values() if v is not None),
        "null": [k for k, v in refs.items() if v is None],
    }
    print(json.dumps(stats, indent=1), file=sys.stderr)
    json.dump(stats, open(a.out + ".stats", "w"), indent=1)
    with open(a.out + ".notes", "w") as f:
        for t, m in srv.notes:
            if m["method"] != "textDocument/publishDiagnostics":
                f.write(f"{t} {json.dumps(m)[:300]}\n")
    try:
        srv.request("shutdown", None, timeout=10)
        srv.notify("exit", None)
    except Exception:
        pass
    srv.p.kill()


if __name__ == "__main__":
    main()
