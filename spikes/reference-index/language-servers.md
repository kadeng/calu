# Spike `lsp`: Python language servers over LSP stdio, and the name-search baseline

Container `calu-spike-lsp` (`python:3.13-slim`, no privileges, repository mounted read-only,
copy at `/work/repo`, HEAD 9532ef3 plus the uncommitted change of `src/calu/session.py` that
the checkout held when it was copied at 11:11). The container is stopped, not removed.
All numbers are measured (wall clock, 24 cores) unless marked "estimated" or "inferred".

## Versions and commands

| What | Version | Source |
|---|---|---|
| basedpyright (the reference; based on pyright 1.1.414, runs on the bundled Node 24.19.0 of `nodejs-wheel-binaries`) | 1.40.2 | PyPI |
| ty | 0.0.112 | PyPI |
| pyrefly | 1.5.0 | PyPI |
| typescript-language-server / typescript | 6.1.0 / 5.9.3 | npm (section F only) |
| bash-language-server | 5.8.1 | npm (section F only) |
| uv, CPython 3.14.10 (only for `ast`, see H) | 0.13.10 | PyPI, Astral's python-build-standalone release |
| Debian nodejs / npm (only to run `npm install`) | 20.19.2 / 9.2.0 | apt |

The project's dependencies are NOT installed.

    py314 lspclient.py --ready-log "Found [0-9]+ source files" --raw raw-basedpyright.json refs.json -- basedpyright-langserver --stdio
    py314 lspclient.py --settle 20 --raw raw-ty.json refs-ty.json -- ty server
    py314 lspclient.py --settle 20 --raw raw-pyrefly.json refs-pyrefly.json -- pyrefly lsp
    py314 grepref.py /work/repo sample.json refs-grep.json
    py314 classify.py /work/repo sample.json refs-grep.json refs.json grep-only-classified.json -- basedpyright-langserver --stdio
    py314 freshness.py /work/repo [--client-watches] --ready-log "Found [0-9]+ source files" -- basedpyright-langserver --stdio

Files: `lspclient.py` (the client, 230 lines, standard library only), `enclosing.py` (the `in`
column with `ast`), `grepref.py`, `classify.py`, `freshness.py`, `compare.py`,
`show_missing.py`; results `refs.json`, `refs-with-bases.json`, `refs-ty.json`,
`refs-pyrefly.json`, `refs-grep.json`, `grep-only-classified.json`, `compare-*.txt`,
`raw-*.json`; intermediate runs in `work/`.

## Headline

- basedpyright answers all 50 samples: 284 referring lines (function 131, method 117,
  common-name 36). pyrefly agrees on 281 of them, ty on 267 of the 47 it answers.
- The name search gives 6015 lines; 5731 of them are not in the server's answer, and the
  server has no line the name search lacks (0).
- **A method's references are those of that one declaration.** Calls through a base class or
  a `Protocol` are attributed to the base method, not to the implementation that was asked
  about. calu uses Protocols for its stores and runners (`SessionStore`, `ClaudeRunner`), so
  `DatabaseSessionStore.distrust_file` shows 4 referrers and none of the two calls
  `self.store.distrust_file(...)` in `session.py`. `textDocument/implementation` on the base
  method returns the implementations (also the structural ones of a Protocol), so a client can
  join the two. `refs-with-bases.json` holds this join (27 more entries, `kind` says which).
- **The server answers before it knows the files.** A `references` request sent right after
  `initialized` came back with an empty list for S01 (9 referrers once ready) and no error.
  The client must wait for the log message `Found N source files`. pyrefly has the same race
  (258 instead of 282 lines when asked at once) and no message to wait for.
- **Unresolved third-party imports lose real references** where the receiver's type comes
  from the missing package: `with pytest.raises(RequestFailed) as e: ... e.value.code` is
  unresolved in 27 places, against 3 referrers found for `RequestFailed.code` (inferred from
  the `raises(...)` argument; not confirmed with pytest installed).
- 47 of the 353 files use `except A, B:` without parentheses (Python 3.14, PEP 758).
  Python 3.13's `ast` rejects them, so `in` needs a 3.14 interpreter. Any tool whose parser
  is older than 3.14 will have the same trouble with these files, among them `session.py`,
  `cli.py` and `gate.py`.

## A. How the referrers are obtained

`initialize` (rootUri `/work/repo`, one workspace folder) → `initialized` → wait for the
`window/logMessage` "Found 353 source files" → `textDocument/didOpen` of the sample's file →
`textDocument/references` at (line-1, col) with `includeDeclaration=false`. The client answers
the server's `workspace/configuration` with nulls and `window/workDoneProgress/create` with
null. The settings come from the repository's `[tool.pyright]` (`include = ["src", "tests"]`,
Python 3.14, standard mode).

- Granularity: file, line and column range of every referring name. No enclosing function:
  `in` is computed afterwards with `ast` (innermost `def`/`class`, decorators included, else
  `<module>`; 14 of the 284 lines are at module level, all of them imports or `__all__`
  entries).
- Identity: the position of the declaration. Two functions of the same bare name are distinct;
  `Transcript.move_cursor` does not get `Composer.move_cursor`, `web.password.load` gets 9 of
  236 `load` lines. LSP has no qualified name in a `references` answer; calu would compute it
  (as here) or ask `textDocument/documentSymbol`.
- What counts as a reference: calls, the name passed as a value, `import` lines, names in
  `__all__` strings. NOT: names in docstrings, comments and other strings
  (`monkeypatch.setattr(local, "container_data", ...)` is missed), and calls through a base
  class or Protocol (headline). `includeDeclaration=true` adds only the declaration asked
  about, not overriding ones. No sample has an overriding method in the repository.
- The definition line never appears in the answers; the client drops it anyway.

Raw answer for S02 (`_private_file_handler`), complete:

    [{"uri": "file:///work/repo/src/calu/logfile.py",
      "range": {"start": {"line": 210, "character": 18}, "end": {"line": 210, "character": 39}}},
     {"uri": "file:///work/repo/src/calu/logfile.py",
      "range": {"start": {"line": 260, "character": 18}, "end": {"line": 260, "character": 39}}}]

    refs.json: "S02": [{"file": "src/calu/logfile.py", "line": 211, "in": "setup"},
                       {"file": "src/calu/logfile.py", "line": 261, "in": "_setup_refusals"}]

Base classes and Protocols, asked directly (`work/tmp-proto-*.json`):

| Asked | `references` | `implementation` |
|---|---|---|
| `SessionStore.distrust_file` (Protocol) | `session.py:1707`, `:2369` | `MemorySessionStore.`, `DatabaseSessionStore.distrust_file` |
| `ClaudeRunner.set_model` (Protocol) | `session.py:3415` | `FakeRunner.`, `SdkRunner.set_model` |
| `LockSpace.snapshot` (ABC) | 6 lines | `FileLockSpace.`, `ThreadLockSpace.snapshot` |
| `ThreadLockSpace.snapshot` (S41) | 5 other lines | none |

### The three servers compared (B relative to basedpyright A; `compare-lsp-vs-*.txt`)

| Server | Answered | Lines | A only | B only | Notes |
|---|---|---|---|---|---|
| basedpyright 1.40.2 | 50 | 284 | | | reads `include` of pyproject: `spikes/` is not searched |
| pyrefly 1.5.0 | 50 | 282 | 3 | 1 | lacks two `__all__` strings and `self.pause.info()` (the only referrer of S33); has `spikes/worktrees_leakcheck.py:11` |
| ty 0.0.112 | 47 | 268 | 16 | 1 | answers `null` for S28, S45, S47 (0 or 1 referrers elsewhere); lacks all 14 import and `__all__` lines and 2 calls |

`refs-ty.json` keeps ty's `null` for those three; it means "no answer", ty has the symbols.

## The name search against the language server

Every line of the 353 Python files under `src/` and `tests/` that holds the bare name as a
word (`(?<![A-Za-z0-9_])name(?![A-Za-z0-9_])`), without the definition line: 6015 lines, 9.9 s
in pure Python.

| Group | Samples | Name-search lines | Server lines | Name search only | Server only |
|---|---|---|---|---|---|
| function | 20 | 230 | 131 | 99 (43 %) | 0 |
| method | 20 | 2048 | 117 | 1931 (94 %) | 0 |
| common-name | 10 | 3737 | 36 | 3701 (99 %) | 0 |
| all | 50 | 6015 | 284 | 5731 (95 %) | 0 |

What the 5731 lines are, for all of them (`classify.py`: token kind with `tokenize`, and for a
name in code the server's `textDocument/definition` at that place):

| Kind | function | method | common-name | all |
|---|---|---|---|---|
| inside a string or docstring | 78 | 934 | 1480 | 2492 |
| another symbol of the same name, resolved by the server (attribute access) | 6 | 140 | 872 | 1018 |
| another symbol of the same name, resolved (bare name: variable, parameter, function) | 3 | 327 | 545 | 875 |
| the line declares that name itself (variable, parameter, attribute) | 4 | 315 | 199 | 518 |
| attribute access the server cannot resolve (receiver of unknown type) | 0 | 87 | 335 | 422 |
| comment | 2 | 65 | 86 | 153 |
| a symbol outside the repository (standard library) | 0 | 52 | 74 | 126 |
| another `def`/`class` of that name | 0 | 6 | 71 | 77 |
| bare name the server cannot resolve (mostly keyword arguments of pydantic models) | 6 | 5 | 12 | 23 |
| **call of the base or Protocol method the sample implements** | 0 | 0 | 22 | 22 |
| the `def` of that base or Protocol method | 0 | 0 | 5 | 5 |

Real references among them, as far as found:

- 22 calls through a base class or Protocol (S41 6, S42 2, S46 1, S47 12, S50 1 -- in
  `refs-with-bases.json`).
- Of the 445 unresolved places: 27 `e.value.code` after `pytest.raises(RequestFailed)` (S38),
  1 call inside an untyped lambda (`lambda s, t: s.run_command_in_worktree(...)`, S22). Most of
  the rest are other objects whose type comes from a package that is not installed
  (326 `conn.close()` and the like for S49: psycopg, sockets, websockets). I read about 40 of
  them, not all 445.
- 2 names in strings that are real: `monkeypatch.setattr(local, "container_data", ...)` (S06).
- Docstring mentions such as `` `calu.admin.enable_offer` `` are the bulk of the "function"
  group's 99 lines. They are references in prose; whether calu wants them is its choice.

Ten lines drawn at random (seed 20261127) and read by hand:

| Sample | Line | What it is |
|---|---|---|
| S29 `FakeSession.record` | `src/calu/honesty.py:2483` `record = self.verified.get(key)` | a local variable |
| S47 `ThreadRWLock.request` | `tests/unit/test_protocol_side_usage.py:66` `await protocol.request("side.ask", ...)` | other function of that name (`Client.request`) |
| S41 `ThreadLockSpace.snapshot` | `src/calu/write_sets.py:211` | the word in a docstring |
| S41 | `tests/unit/test_protocol_server.py:437` `assert snapshot["workspace"] == ...` | a local variable |
| S47 | `tests/unit/test_cli_commands.py:453` `request_id: The request (ignored).` | the word in a docstring |
| S38 `RequestFailed.code` | `src/calu/gate.py:65` "executes project code" | the word in the module docstring |
| S38 | `src/calu/checkers/rules.py:296` "code tools" | the word in a docstring |
| S41 | `tests/unit/test_call_locks.py:248` `space.snapshot()` | **real**: `space` is a `LockSpace`, the call goes to the base method the sample overrides |
| S47 | `src/calu/serve.py:548` `"... to file a request with"` | a string literal |
| S39 `CheckersScreen._store` | `tests/unit/test_honesty_review.py:947` `session, rec, _store = ...` | a local variable |

One of ten is a reference the server left out, and it is of the base-class kind.

Per sample (A = name search, B = basedpyright):

    id   group        name                      A    B  A-B  B-A
    S01  function     enable_offer               12    9    3    0
    S02  function     _private_file_handler       3    2    1    0
    S03  function     record_start               17   15    2    0
    S04  function     _section_fields             7    7    0    0
    S05  function     user_message_source         7    4    3    0
    S06  function     container_data              6    4    2    0
    S07  function     cache_settings             10    6    4    0
    S08  function     stage                      43    2   41    0
    S09  function     _connect_failure            2    1    1    0
    S10  function     no_login                   18   16    2    0
    S11  function     commit_options             29   24    5    0
    S12  function     prepare_data_dir           15   12    3    0
    S13  function     step_details                1    1    0    0
    S14  function     _nest                       1    1    0    0
    S15  function     new_owner_password          3    2    1    0
    S16  function     cases_from_escalations      6    6    0    0
    S17  function     remove_from_setting         3    3    0    0
    S18  function     honesty_clean               1    1    0    0
    S19  function     label_for_unchecked        10    9    1    0
    S20  function     chat_summary               36    6   30    0
    S21  method       _on_check                   1    1    0    0
    S22  method       run_command_in_worktree    47   44    3    0
    S23  method       all_text                   10   10    0    0
    S24  method       limits                    146    3  143    0
    S25  method       move_cursor                 5    4    1    0
    S26  method       _left_out_line              2    2    0    0
    S27  method       load_snapshot               4    4    0    0
    S28  method       drop_over                   2    1    1    0
    S29  method       record                    423   10  413    0
    S30  method       from_environment            4    4    0    0
    S31  method       _log                       12    4    8    0
    S32  method       for_role                   12   10    2    0
    S33  method       info                      376    1  375    0
    S34  method       duration                  105    6   99    0
    S35  method       _set_mode                   1    1    0    0
    S36  method       _rule_rows                  1    1    0    0
    S37  method       _apply_auto                 2    2    0    0
    S38  method       code                      748    3  745    0
    S39  method       _store                    140    2  138    0
    S40  method       turn_number                 7    4    3    0
    S41  common-name  snapshot                  498    5  493    0
    S42  common-name  distrust_file              18    4   14    0
    S43  common-name  release                   202    2  200    0
    S44  common-name  load                      236    9  227    0
    S45  common-name  compose                   101    0  101    0
    S46  common-name  load_history               37    2   35    0
    S47  common-name  request                  1915    0 1915    0
    S48  common-name  withdraw                   65    3   62    0
    S49  common-name  close                     651   10  641    0
    S50  common-name  set_model                  14    1   13    0
    group function     samples 20  A   230  B   131  A-B    99  B-A     0
    group method       samples 20  A  2048  B   117  A-B  1931  B-A     0
    group common-name  samples 10  A  3737  B    36  A-B  3701  B-A     0
    all                samples 50  A  6015  B   284  A-B  5731  B-A     0

## B. Freshness (basedpyright; `freshness.py`, sample S12)

A language server reads the working tree, never git: uncommitted and untracked content counts.

| Step | New referrer reported | Query time |
|---|---|---|
| first query after start | | 1.80 s |
| call appended to tracked `src/calu/homes.py` on disk, no notification, asked 3 s later | yes | 0.11 s |
| new untracked `src/calu/spike_new.py` on disk, no notification, asked 3 s later | **no** | 0.10 s |
| `workspace/didChangeWatchedFiles` (created) sent, asked at once | **no** | 1.15 s |
| asked again 2 s later | yes | 0.15 s |
| changed text sent with `didOpen` only (unsaved buffer, disk unchanged) | yes | 0.15 s |
| fresh server on the changed directory | yes (both) | 2.7 s from start to answer |

- There is no update command and no re-index: the server reads a changed file again at the
  next query. A new file needs the notification, and then again a wait without a completion
  signal (the file list is rebuilt in the background).
- With the client capability `workspace.didChangeWatchedFiles.dynamicRegistration` the server
  asks the client to watch; the results were the same in both modes.
- ty and pyrefly: not tested for freshness. ty logs "Your LSP client doesn't support file
  watching: You may see stale results when files change outside the editor".

## C. Time and size

| | basedpyright | pyrefly | ty |
|---|---|---|---|
| start to first answer | 3.1 to 3.2 s (4 runs) | 0.4 s, but incomplete; complete after a wait (20 s used, shortest sufficient wait not measured) | 0.5 s |
| time per query, median / mean / max | 0.27 / 0.34 / 2.2 s | 0.02 / 0.03 / 0.13 s | 0.05 / 0.07 / 0.45 s |
| all 50 queries, one process | 22 s | 3.7 s plus the wait | 5.9 s |
| server memory after 50 queries (VmHWM of the server and its children) | 0.91 GB | 0.35 GB | 0.39 GB |
| index on disk | none | none | none |

- "Full index", "repeated index" and "index after an edit" do not exist: every start costs the
  3 s, nothing is kept between processes, and a query after a one-file edit took 0.11 s.
- `/usr/bin/time -v` on the client shows only the client (44 MB), because the server is a
  child that is killed; the server figures come from `/proc/<pid>/status`.
- The first query pays for parsing (1.8 to 2.2 s); the slowest later ones are the common
  names (`close`, `request`), since pyright reads every file that holds the name as text.
- A CLI type check of the whole project (`basedpyright --outputjson`) takes 40 s and reports
  706 unresolved imports (pytest 161, anyio 152, pydantic_ai 101, textual 56, psycopg 54,
  pydantic 38, ...). The reference queries do not need that run.

## D. Index and caches

- None of the three servers wrote a file: `find / -xdev -newer <marker>` after a full run
  shows nothing outside the client's own output, and `git status` in `/work/repo` is
  unchanged. Nothing is written into the project directory.
- The state is the server process's memory. There is no stored graph that a supervised agent
  could change by hand, and the "delete a node, update, check" test has no object. What the
  agent can change is the input: the source files and `pyproject.toml` (`[tool.pyright]
  include/exclude` decides which files are searched; a `pyrightconfig.json` in the project
  would override it).
- basedpyright looks for a Python interpreter to find `site-packages`; no setting was needed.

## E. Network and code execution

- With `docker network disconnect bridge calu-spike-lsp` all three servers answered as before
  (basedpyright: the same 284 lines, 3.1 s to the first answer). None logged a network error.
- No code of the repository is run and nothing is built. basedpyright starts `node` and may
  start `python` to ask for its search paths (the interpreter, not project code).
- Installation: wheels from PyPI; basedpyright needs no system Node.

## F. Languages

A language server covers one language; each further language is another server process.

| Language | Server | Checked |
|---|---|---|
| Python | basedpyright, pyrefly, ty | this report |
| TypeScript, JavaScript | typescript-language-server 6.1.0 + typescript 5.9.3 | `oneLine` exists twice in `extensions/vscode/src/shared`; `sessionPick.ts:57` gets 1 referrer, `chatModel.ts:80` gets 4, no mixing; without `node_modules`; 3.8 s to the answer, 0.41 GB. `typescript@7.0.2` (npm `latest`) has no `tsserver` and does not work with it. |
| shell | bash-language-server 5.8.1 | the scripts in `scripts/` define no functions; the variable `LIBS` of `get-browser.sh` gets its 7 uses; a toy `lib.sh` with `greet()` sourced by `main.sh` gets both calls. Resolution is by name within sourced files. |
| C, C++, Rust, Go | clangd, rust-analyzer, gopls | not tested |
| HTML, Markdown | servers exist, no reference edges into code | not tested |
| YAML with Hydra `_target_` | none | toy (`conf/config.yaml` with `_target_: pkg.mod.Greeter`, `pkg/mod.py`, `use.py`): all three Python servers return only `use.py`; the YAML is not linked |

## G. Git worktrees

`git worktree add /work/wt` (outside) and `/work/repo/wt-inside` (inside the project).

- **Second root** (`workspaceFolders` = repo and `/work/wt`): basedpyright starts one
  "service instance" per root, each with its own `pyproject.toml`. The 50 answers for the
  repository are identical (284 lines, none from a worktree). The same sample asked at its
  path in the worktree gets only worktree files, including a call added only there. The roots
  do not mix. Cost: 6.3 s to the first answer instead of 3.1 s; memory 0.93 GB while only one
  root was queried, 1.41 GB after the 50 samples were asked in both.
- **Worktree inside the project**: basedpyright ignores it only because `include` is
  `src`, `tests`. ty and pyrefly search the whole root: ty returned 501 lines instead of 268
  (233 from `wt-inside`, attributed to the main checkout's functions), pyrefly 295 instead of
  282 (13 from `wt-inside`). A worktree inside the project must be excluded by configuration
  or be a root of its own.

## H. Not done, and limits

- The effect of the missing dependencies is inferred from the unresolved places, not measured
  by a second run with the dependencies installed (the task forbade installing them).
- The 445 unresolved places were not all read by hand.
- `ast` of Python 3.13 cannot parse 47 files, so CPython 3.14.10 was added with
  `uv python install 3.14` (download from Astral's python-build-standalone releases on GitHub,
  not from PyPI).
- ty and pyrefly: only the 50 samples, the Hydra toy, the network cut and the worktree
  inside; no freshness test, no readiness signal found for pyrefly.
- The stored-graph tampering test of D does not apply.
- C, C++, Rust, Go, HTML and Markdown servers were not installed.
- By mistake `pip download nothing` ran once in the container: it fetched the 1.5 kB PyPI
  wheel `nothing-0.0.3` into `/work/repo` without installing it; the file is deleted.
- Debian's `npm` was installed with apt for the two npm servers, since the Node of
  `nodejs-wheel-binaries` comes without a working `npm`.
