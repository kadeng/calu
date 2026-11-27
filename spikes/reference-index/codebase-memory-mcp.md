# Spike: codebase-memory-mcp as a reference index for calu

All numbers are **measured** in the container `calu-spike-cbm` (python:3.13-slim, 24 CPUs,
187 GB RAM, no limits) unless marked *estimated*. Date 2026-11-27.

## Versions and set-up

- Tool: codebase-memory-mcp **0.12.1**, `codebase-memory-mcp-linux-amd64.tar.gz` from
  `github.com/DeusData/codebase-memory-mcp/releases/download/v0.12.1/`.
  SHA-256 `8453015145f072c1b9b019cbdd02cea8cc6f18cc5e116036dd20992631c638c7`; equal to the
  line in the release's `checksums.txt` (`sha256sum -c`: OK; `checksums.txt` itself:
  `103ddf75ccbe4e4de9faa57f074ee5f16b88edab6fd2f7dfd34e50d147dddffe`). The `.bundle` signature
  files were not verified. The archive holds one binary of 331 MB, `install.sh`, LICENSE and
  notices.
- Only the binary was unpacked to `/opt/cbm/`. `install`, `install.sh`, `update` were never run.
- `CBM_CACHE_DIR=/cbmcache`. Repository copy: `/work/repo` (rsync without `.venv`,
  `node_modules`; HEAD 9532ef3 plus an uncommitted change of `src/calu/session.py` that the
  host checkout had at copy time, 11:11 UTC).
- Commands (all with `cd /tmp`):

      codebase-memory-mcp cli index_repository --repo-path /work/repo
      codebase-memory-mcp cli search_graph --project work-repo --name-pattern '^NAME$' --file-pattern PATH --format json
      codebase-memory-mcp cli trace_path --project work-repo --function-name QN --direction inbound --depth 1 --include-tests true --include-evidence true --format json
      codebase-memory-mcp cli query_graph --project work-repo --format json --query 'MATCH (a)-[r:CALLS|CALL_REFERENCE|USAGE|OVERRIDE|DECORATES|IMPORTS|INHERITS]->(f) WHERE f.qualified_name = "QN" RETURN type(r), a.label, a.qualified_name, a.file_path, a.start_line, r.line, r.strategy, r.confidence'

  Scripts: `scripts/refs.py` (builds `refs.json`), `scripts/timed_index.sh`, `scripts/tally.py`.
  Raw answers: `raw/`.

## A. Referrers

**How.** `search_graph` (name pattern + file pattern) gives the node; its qualified name goes
to `trace_path` and to `query_graph`. A bare name that is not unique makes `trace_path` answer
`"status":"ambiguous"` with a list of qualified names (18 for `close`); with the qualified
name it answers for that node only. All 50 samples have a node (no `null` in `refs.json`).

**Identity.** Every function, method and class is its own node:
`<project>.<path with dots>.<Class>.<name>`, e.g. `work-repo.src.calu.call_locks.CallLocks.close`
and `work-repo.src.calu.traces.writer.TraceWriter.close` are distinct. `UNIQUE(project,
qualified_name)` in the store. **Nested functions have no node**; a call inside one is
attributed to the **File node** (`…cli.py.__file__`), not to the enclosing function
(`cmd_config` exists as a node, the call at `cli.py:5847` in its inner `work()` hangs on the
file). 21 of the 176 entries in `refs.json` have `in = "<module>"` for this reason or for
real module-level code.

**Granularity.**

| | file | line | enclosing function |
|---|---|---|---|
| `trace_path` | no (only the caller's qualified name) | no | yes |
| `query_graph` on CALLS | yes | yes (`r.line`), **one line per caller/callee pair**: five calls in one test give one edge with one line | yes |
| `query_graph` on CALL_REFERENCE, USAGE | yes | **no** (the edge stores only `callee`) | yes |

`refs.json` is built from `query_graph`; extra keys per entry: `edge`, `qn`, `label`,
`in_start_line`, `strategy`, `confidence`. `trace_path` returned exactly the CALLS referrers
for all 50 samples (compared in `scripts/refs.py`; `raw/trace_path.json`); it does not follow
CALL_REFERENCE or USAGE by default.

**Proven or not.** Three edge types (README): `CALLS` = invocation; `CALL_REFERENCE` = a
callable used as a value with one exact target; `USAGE` = an identifier used, target not
proven. In the store a USAGE edge is still **one edge to one node chosen by bare name**:
`left.close` (a `TraceWriter`) in `Session._start_traces` is a USAGE of `CallLocks.close`,
`self.workspace.name` a USAGE of `tui.commands.Command.name`. So USAGE is "some symbol of
that name", not "all candidates": it is a false positive for one node and nothing for the
right one. CALLS carry `strategy` and `confidence` (`--include-evidence`), and that is the
second distinction: `lsp_*`, `import_map`, `same_module` (0.85 to 0.97) are resolved through
imports and types; `unique_name` (0.38 to 0.75) and `suffix_match` (0.01 to 0.55) are name
matches. `admin.enable_offer(...)` after `from calu import admin` is `unique_name` 0.38
although it is right.

Counts, whole graph of `/work/repo` (16 495 nodes, 96 748 edges): CALLS 26 244,
USAGE 22 202, CALL_REFERENCE 407, IMPORTS 5 184, DECORATES 1 258, INHERITS 212, OVERRIDE 135,
TESTS 5 702, MENTIONS (Markdown to code) 1 331. CALLS by strategy: resolved (`lsp_*`,
`import_map`, `same_module`, `qualified_suffix`, `field_type_hint`) about 21 000; `unique_name`
2 500; `suffix_match` 2 566; 141 without strategy.

Counts in `refs.json` (50 samples, 176 entries): CALLS 100 (of them `unique_name` 41,
`lsp_method` 30, `lsp_direct` 12, `lsp_callable_alias` 10, `import_map` 6, `suffix_match` 1),
USAGE 63, IMPORTS 12, CALL_REFERENCE 1. Nine samples have no referrer at all: S22, S23,
S41 to S47.

**Recall (the main finding).** A call the resolver cannot resolve gets **no edge of any
type**, not a USAGE. Checked by hand:
- S22 `LockingScheme.run_command_in_worktree`: called in `tests/fuzz/locking_harness.py:455`,
  `tests/unit/test_call_locks.py:353`, `test_locking.py:480`; tool: 0 referrers.
- S23 `Transcript.all_text`: `run.app.transcript.all_text()` in four test files; tool: 0.
- S03 `servers.record_start(...)` in `tests/db/test_serve.py:615` and others; tool: 3 of 6 files.
- S30 `Secrets.from_environment(names)` at `cli.py:6764` (function-local import); missing.
- S44 `web_password.load()` at `cli.py:4582`; S42 `self.store.distrust_file(...)` in
  `session.py`; both missing.
*Estimate* from a plain word search over the `.py` files (`scripts/tally.py`; it also counts
mentions in docstrings, so it is an upper bound): for the 40 samples with distinctive names,
17 have every word-search file among the tool's referrers (18 without my own test edit of
`homes.py`); for the 10 common names the tool
reports 0 to 1 files.

**Raw example** (`trace_path`, tree format, and `query_graph` for S49):

    function: work-repo.src.calu.pause.Pause.info
    direction: inbound
    callers_total: 1
    callers_total_relation: eq
    callers: 1  (cols: qn hop test strategy confidence)
      work-repo.src.calu.session.Session.snapshot 1 false lsp 0.9

    {"columns":["type(r)","a.label","a.qualified_name","a.file_path","a.start_line","r.line","r.strategy","r.confidence"],
     "rows":[["CALLS","Method","work-repo.src.calu.session.Session._run","src/calu/session.py","3217","3259","lsp_method","0.90"],
             ["USAGE","Method","work-repo.src.calu.session.Session._start_traces","src/calu/session.py","5595","","",""],
             ["USAGE","Method","work-repo.src.calu.session.Session._switch_traces","src/calu/session.py","5898","","",""]],
     "returned":3,"total":3,"total_relation":"eq","has_more":false,"truncated":false}

**A node's own fields** (S33, `raw/node_fields_S33.json`): label `Method`, name `info`,
qualified name `work-repo.src.calu.pause.Pause.info`, file `src/calu/pause.py`, lines 65 to 73,
signature `(self)`, return type `PauseInfo`, docstring (the full text, **with the triple
quotes**), `is_test` false. A longer signature is stored verbatim with its line breaks
(`enable_offer`: `(\n    db: Db, key: str, value: Any, project: str | None = None\n)`).
Also stored: complexity figures, `param_names`/`param_types` (incomplete: one of four for
`enable_offer`), a body fingerprint.

## B. Freshness

Update command: `index_repository` again. Both new referrers of S02 `_private_file_handler`
were reported after it:

| Change in /work/repo | reported | wall time |
|---|---|---|
| call appended to tracked `src/calu/homes.py`, uncommitted | yes (`homes.spike_tracked_probe`, lsp 0.97) | 19.0 s cold, 8.7 s with a running daemon |
| new untracked `src/calu/spike_untracked.py` | yes (`spike_untracked.spike_untracked_probe`) | 19.0 s cold |

- The tool reads the **working tree**, not commits: uncommitted and untracked files are
  indexed (files ignored by `.gitignore` are not).
- The update is incremental in the store: node ids of unchanged files stay the same, only the
  changed file's nodes and edges are replaced (the database file itself is rewritten, new
  inode). It is not cheap: the index worker peaked at 269 to 486 MB for a one-line edit.
- **Watcher: not usable from the CLI.** `index_status` says `"watched": false, "reason":
  "not_session_project"`; README: the watcher follows only the project of an open MCP
  session, a CLI call "never registers with the background watcher". For calu's own use
  (CLI or library, no MCP session) `index_repository` after each step is the update.
- **`detect_changes` is not an update.** It lists the files that differ from the merge base
  with `origin/main` (tracked, modified and untracked: it named a file created seconds
  before) and walks the *existing* graph from them; a new file not yet indexed gave
  `seed_symbols: 0, impacted: []`, and `trace_path` did not show its call until
  `index_repository` ran. It is not needed for freshness.
- `indexed_at` in `index_status` did not advance on the incremental runs observed.

## C. Time and size

Every CLI call starts a temporary daemon when none runs; that costs about 8 s and 5 CPU
seconds per call. `daemon start` keeps one (it then stays until `daemon stop`).

| Run | wall | peak RSS |
|---|---|---|
| full index, cold (first run) | 20.3 s | client 16 MB; worker not sampled in this run |
| full index after `delete_project`, daemon running | 13.2 s | worker 510 MB, daemon 68 MB, client 19 MB |
| full index, cold, network cut, under strace | 21.4 s | - |
| repeated, nothing changed, cold | 11.3 s | worker 17 MB, daemon 33 MB |
| repeated, nothing changed, daemon running | 5.7 s | worker 17 MB, daemon 58 MB |
| after a one-file edit, cold | 19.0 s | worker 449 MB |
| after a one-file edit, daemon running | 8.7 s | worker 269 MB |
| one query (`trace_path`, `query_graph`), cold | 8.4 s (1253 s for 150 calls) | - |
| one query, daemon running | 2.9 s | - |

`/usr/bin/time -v` sees only the thin client (16 to 19 MB); the worker and daemon figures are
`VmHWM` from `/proc`, sampled every 50 ms (`scripts/timed_index.sh`), so a short peak can be
missed. Index: `/cbmcache/work-repo.db`, 71 to 75 MB (SQLite, with FTS and vectors) for 816
files; plus `_config.db`, `activity.db`, `logs/`. A direct `sqlite3 -readonly` query on that
file takes milliseconds; the 2.9 s is the CLI.

## D. Location, writes, hand-edited store

- Everything lies in `CBM_CACHE_DIR` (default `~/.cache/codebase-memory-mcp`): one
  `<project>.db` per indexed root, `_config.db`, `activity.db`, `logs/`. Chosen by the
  environment variable only; there is no flag. One account can use one cache root at a time
  (README). A call without the variable created `/root/.cache/codebase-memory-mcp/_config.db`.
- The project name is derived from the path (`/work/repo` -> `work-repo`); `--name` overrides.
- Runtime files outside the cache: `/tmp/cbm-daemon-<uid>/cbm-*.sock` and `.anc`.
- **Nothing was written into the project directory** (`git status` shows only my test edits).
  `--persistence true` would write `.codebase-memory/graph.db.zst` there; it is off by default.
- **A hand-edited store is not repaired by the normal update.** I deleted two CALLS edges and
  the node `Pause.info` with `sqlite3`. Then:
  - `index_repository`, no file changed: both edges and the node still missing.
  - after a content change of the caller's file (`homes.py`): that file's edge is back; the
    edge from unchanged `logfile.py` and the node in unchanged `pause.py` still missing.
  - after `touch` (mtime only) of `pause.py` and `logfile.py`: still missing (hash compared).
  - `delete_project` + `index_repository` (13 s): all back.
  No integrity check noticed the edit. The store is a plain SQLite file owned by the user
  (mode 0600).

## E. Network and code execution

- With the network cut (`docker network disconnect bridge calu-spike-cbm`; only `lo` left,
  `curl` fails to resolve), `delete_project` and a full `index_repository` ran normally
  (`indexed`, 16 499 nodes, 96 772 edges, 21.4 s), and so did all later runs (toy, worktrees).
- `strace -f -e trace=connect,bind,listen,sendto,socket` over that run
  (`raw/strace_offline_index.txt`): no outbound `connect`; one TCP `bind` to
  **127.0.0.1:9749** and two Unix sockets. No model, no key asked for.
- Processes: the client `cli …`; a detached daemon `--cbm-daemon-internal` (temporary: exits
  when the last client leaves; `--cbm-daemon-permanent` after `daemon start`); one
  `cli --index-worker …` per index. In a container whose PID 1 does not reap, each temporary
  daemon stays as a zombie.
- `ss -ltnp` / `ss -lxp` while the daemon ran:

      LISTEN 127.0.0.1:9749  users:(("codebase-memory",pid=26688,fd=18))
      u_str LISTEN /tmp/cbm-daemon-0/cbm-c888acc3ae367a1e.anc  users:(("codebase-memory",pid=26688,fd=15))
      (plus /tmp/cbm-daemon-0/cbm-c888acc3ae367a1e.sock)

  The TCP port is the built-in web UI ("CBM Lens"), on by default (`ui_enabled = true`,
  `ui_port = 9749`; `--ui=false` or `config set` persists it off; not tried). The temporary
  daemon of a single CLI call binds it too.
- Programs started (`strace -e execve`, `raw/strace_exec.txt`): only itself and `git`
  (`rev-parse`, `symbolic-ref`, `merge-base HEAD @{upstream}`, `ls-files -z`), some through
  `sh -c`. No code of the repository is run, no build, no interpreter.
- The binary has an `update` command and `CBM_DOWNLOAD_URL`; neither was run.

## F. Languages

Supported per README: 162 tree-sitter grammars; type-aware resolution ("Hybrid LSP", its own
C code, no language server) for Python, TypeScript/JavaScript, PHP, C#, Go, C, C++, Java,
Kotlin, Rust, Perl.

| Language | Result here |
|---|---|
| Python | functions, methods, classes; CALLS/USAGE/IMPORTS/INHERITS/OVERRIDE/DECORATES |
| TypeScript (`extensions/vscode/src`) | CALLS 722 inside the extension (`lsp_ts_method` 245, `lsp_ts_local` 214, `lsp_ts_import` 118, …), USAGE 1 653, IMPORTS 320. Example: `CheckerFileSystem.readFile` -> `BridgeCore.ensureStarted` (`this.bridge.ensureStarted`, 0.95) |
| JavaScript | toy: `caller` -> `helper`, `lsp_ts_local` |
| Shell | functions are nodes; CALLS between functions of one script (`install.sh`: `need_tools` -> `fail`, `same_module`); toy: `SPAWNS` from a function to the `Module` of `./other.sh`. `scripts/*.sh` here have no functions, so they give Module, Variable, SPAWNS and USAGE only |
| C, C++, Rust, Go | toy: one CALLS edge each (`lsp_direct`, C++ `lsp_scoped`). Go names are per package (`work-toy.langs.caller`), not per file |
| Kotlin | indexed (`extensions/jetbrains`), `lsp_kt_*` strategies |
| HTML | File/Module nodes; `<script src>` gives IMPORTS to the JS module; an `onclick="caller(1)"` gives nothing |
| Markdown | `Section` nodes; `MENTIONS` from a section to a code node named in backticks, `REFERENCES_FILE` for links |
| YAML | keys as `Variable` nodes; compose/Kubernetes/CI get special handling |
| Hydra `_target_` | **not linked.** Toy `conf/config.yaml` with `_target_: pkg.mod.Greeter` and `pkg/mod.py`: no edge between the YAML and the class (only the Markdown mention of `pkg.mod.Greeter` linked) |

## G. Git worktrees

- A worktree **outside** the project (`/work/wt-out`): indexed as its own project
  `work-wt-out` with its own 76 MB database and a full index (18.5 s). `index_status
  --verbose` knows it is one (`is_worktree: true`, `git_common_dir: /work/repo/.git`,
  `canonical_root: /work/repo`, branch). Nothing is shared with the main index.
- A worktree **inside** the project (`/work/repo/wt-in`, untracked, not ignored): the next
  `index_repository /work/repo` indexed it as ordinary files of the main project. Nodes
  doubled (16 495 -> 32 751, database 142 MB, 25 s), every function exists twice
  (`work-repo.src.calu.admin.enable_offer`, `work-repo.wt-in.src.calu.admin.enable_offer`),
  name-matched calls became `suffix_match` 0.28 with 2 candidates, and **9 818 CALLS edges
  cross between the two checkouts**. calu's worktrees inside a project would have to be
  ignored (`.gitignore` / `.cbmignore`) and indexed as separate projects.

## H. Not done, and why

- The watcher: it needs an open MCP session rooted in the project; the CLI cannot register
  one. Not measured: its delay, its handling of untracked files.
- `.bundle` signatures of the release: not verified (needs cosign/sigstore).
- Peak memory of the very first cold full index: only the client was measured there; the
  510 MB is from the later full index with a running daemon.
- Whether the tool tries the network in other commands (`update`, the UI's optional model
  download from Hugging Face named in the README): not run.
- Precision of the 176 entries was not checked one by one; the USAGE mis-targets and the
  missing referrers above are spot checks.
- The host checkout changed while the spike ran (`session.py` grew by about 100 lines after
  my copy); line numbers in `refs.json` refer to the copy of 11:11 UTC.
- The container was stopped with `docker stop calu-spike-cbm`, not removed. Its network is
  still disconnected; `/work/repo` in it holds the test edits and two worktrees.
