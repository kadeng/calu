# Honesty across units: the review marker, stale verdicts and the reference index

Design note of 2026-11-27 for the owner's requirements 49 and 50 (PLAN D293, D294, phase 11).

**Status.** Nothing is built. The owner decided the direction and the points of section 7 on
2026-11-27; that section quotes the answers. Where the text says "proposed" or "Claude's
assumption", the owner has not decided it.

## 1. The review marker (D293)

The owner: Claude places a magic string such as `#do-honesty-review` in its output, calu
detects it and runs an honesty review. No tool is added to Claude Code and no MCP setting is
changed.

**The gap it closes.** Under `enforce`, a step that runs code while marks are open is denied
without a review. A mark closes only through an edit of its file, a review or a person. When
a change of a callee settles a mark on a caller, Claude has no way to say so. It edits the
caller's docstring only to have the file judged again.

Proposed details:

1. **Where the marker counts.** Anywhere in a text message of an agent (the owner). A
   sentence that mentions the marker therefore starts a review too; the limits of point 4
   bound the cost. Not in thinking, tool input, tool output or the user's text. Claude's
   assumption, not asked: a subagent's marker counts like the main agent's.
2. **Which review runs.** The one calu starts by itself at the gate: unreviewed and marked
   files, the file cap, and it never certifies a commit. It runs in `track` and `enforce`. It
   does not run in `off` or with `honesty.sufficient_trust = user`.
3. **When the result arrives.** The review starts when the text arrives. The next checked
   tool call of that agent waits for it, within the hook's time, and carries the result as a
   note: clean, the marks with their reasons, or the reason it did not run. If the turn ends
   first, the first step of the next turn carries the result. Clients see `honesty.update`
   as today.
4. **Limits.** One review at a time. None while nothing changed since the last one; the
   answer then says so and lists the open marks. None within 60 seconds of a failed one.
   Text that a web page plants in Claude's output can cost one review per change of the
   state, and nothing else.
5. **No authority.** The reviewer judges. The marker closes no mark, so no checker has to
   approve it.
6. **How Claude learns it.** From the texts calu already sends about marks (`HONESTY_MARKS`
   in `gate.py`, `HONESTY_NOTICE` in `session.py`) and from the skill `honest-code`.

**As built on 2026-11-27** (`Session._marker_seen`, `_marker_review`; unit tests only), with
what differs from the proposal:

- The outcome goes to the **main agent**, with its next checked tool call after the review
  ended, also when a subagent wrote the marker. No tool call waits for the review; under
  `enforce` a step that needs a clean state joins the running review at the gate anyway.
- The review **runs although the last one read the same files** (`ReviewRun.again`): the
  marker's use is a mark that a change in another file settled, and the files of the plan
  are then unchanged. The proposal's "none while nothing changed" would have stopped exactly
  that review.
- The limit against repeated or planted markers is therefore a different one: a marker
  starts no review until a tool call has finished since the last marker's review, and none
  while one is running. Within 60 seconds of a failed review none is started, as proposed.

Once stale marks are judged again by calu itself (section 3), Claude needs the marker less
often. It remains the way to ask under `track`, and when the index missed a dependency.

## 2. The problem of requirement 50

Today a verdict belongs to a file's content. An edit that the honesty checker answers HONEST
verifies the file. A verdict about a unit also rests on other units, because the checker reads
what a callee does from the callee's name and documentation. Two cases are wrong today:

- An honest edit of `f` changes what `f` is documented to do. A caller `g`, in a file nobody
  edits, keeps its verification. Its documentation no longer covers what it does. Only the
  honesty checker's own search for callers catches this (D112).
- `g` carries a mark because of what `f` did. `f` is changed back. The mark on `g` stays
  until somebody edits `g`.

## 3. The model

    contract(u) = name, signature and doc comment of u
    refs(u)     = the units that u's text references: calls, base classes, overridden
                  methods, decorators, module-level imports, entries of a configuration
    verdict(u)  = judge(text(u), { contract(c) : c in refs(u) })

A verdict is valid while its inputs are unchanged, like a build output:

| Change | Judged again |
|---|---|
| the body of `u`, contract unchanged | `u` |
| the contract of `u` (also: `u` renamed or deleted) | `u`; every unit with `u` in its `refs` becomes **stale** |
| a stale unit is judged honest | nothing further |
| a stale unit is judged dishonest | it gets a mark; the fix changes its body (the change stops here) or its contract (one level up) |
| a contract returns to the form a verdict relied on | the stale entry is dropped without a model call |

A change travels upward only through units whose own contract had to change. It stops at a
unit whose documentation already covers the effect at its level ("what follows from what it
states need not be listed", the honesty rules), or at an entry point: a script, a command, a
test.

    upload_key()   contract now says "sends the SSH key to host X"      contract changed
       ▲ sync()    "Synchronise the cache" does not cover that          stale -> dishonest
       ▲ main()    "Run the tool"                                       stale once sync()'s
                                                                        contract changes

Either the upload is removed, or every level up to the entry point states it. At the entry
point the question changes its kind. The command checker reads a contract that names the
upload and decides whether running it is permitted. Honest documentation makes a dangerous
effect visible; it does not make it allowed.

**References, not only calls.** A subclass that overrides a method, a decorator, an import
that runs module code and a pytest fixture taken by name all make one unit depend on
another's contract without a call expression. The query calu needs is "find the references
of this symbol". A call hierarchy is a subset of it.

### The state

- **Stale units** are a fourth part of the honesty state, next to marks, unreviewed files and
  review required. An entry holds the path, the symbol, the unit whose contract changed, and
  the contract hashes the last verdict relied on. The state is not clean while one exists.
- **As granular as the index allows (the owner).** A stale entry and a mark belong to a node
  of the graph, a function, method or class by its qualified name, wherever a verdict can be
  attributed to one. A module has many more importers than one dishonest function has
  callers. File-level marks stay for what has no node: a script, a configuration file, a
  language the index does not cover.
- **The project as a whole is clean or not (the owner).** Enforcement does not change: one
  stale unit makes the state not clean, as one mark does, and editing and commands that run
  no project code stay allowed. Honesty per entry point, computed from the graph, is a later
  refinement.
- **Stored trust stays per file content.** Stale entries are kept in the session's memory
  and computed again at its start, from the contracts that differ between the baseline and
  the working tree. After a restart a referrer may be judged a second time. A certified
  commit covers its whole tree, so nothing is stale relative to the baseline itself.
- A file that did not change since the baseline can hold a stale unit. That is intended.
- **A mark is stale in the same way.** It records the contract hashes it relied on. When one
  changes, the review judges the mark again, without an edit of its file.
- **Under `enforce`**, stale units are treated like unreviewed files: the review judges them
  before a step that runs code or commits. The reviewer gets the stale unit and the old and
  the new contract of what changed, not the whole file.
- **How a contract change is noticed.** calu writes no parsers of its own (the owner). The
  indexer reports each unit with its signature and doc comment where it can, and calu
  compares them before and after a step. Where it cannot, the honesty checker's answer says
  per unit whether the documented behaviour changed. For Python the existing comparison in
  `calu.checkers.symbols` stays.
- **The honesty checker and the reviewer get the contracts of `refs(u)` in their prompt**,
  with a note on those that are unverified. Today they search for them with tools.
- The review in waves from the leaves of the call graph (D196 (4), the later answer to the
  file cap) uses the same index.

## 4. The index

calu asks two questions: who references this unit, and what does this unit reference. One
interface in calu, several backends. A unit's identity is its qualified name as the index
resolves it, never its bare name (the owner). Where an index covers the language, its
resolved references decide. The name search finds candidate referrers only where no index
does.

**Name search** (calu's own `checkers/search.py`). The fallback for a language no index
covers.

- Gives every file that holds the name as a word, also in shell scripts, configuration and
  strings.
- Knows no scopes or types, so a common name (`run`, `get`) matches too much. There is no cap
  (the owner): every match is a candidate.

**codebase-memory-mcp** (github.com/DeusData/codebase-memory-mcp; MIT, one C binary, SQLite,
v0.12.1 of 2026-10-16).

- Tree-sitter for 164 languages. Calls are resolved with imports and inferred types for
  Python, TypeScript, Go, Java, Kotlin, Rust, C and C++. That resolution is the tool's own
  code, not a compiler or a language server.
- Three edge kinds: a call, a reference with one proven target, and a use without a proven
  target. calu can treat the third as "maybe" and still judge the referrer.
- A command line with JSON output, and 17 MCP tools (`trace_path` for callers and callees).
- Its README says it makes no network request of its own, collects no telemetry and needs no
  model. The index lies in `~/.cache/codebase-memory-mcp` (`CBM_CACHE_DIR` moves it).
- Its own figures put Python and TypeScript at 75 to 89 %.
- The watcher polls git. What it does with untracked files, and how soon after an edit, is
  not stated. `trace_path` leaves tests out by default; calu needs them.
- Its installer writes MCP settings, skills and hooks into the agent's configuration. calu
  never runs it.
- Versions 0.9 to 0.10 fixed five CVEs, one of them an out-of-bounds write.

**Graphify** (github.com/Graphify-Labs/graphify; PyPI `graphifyy`, Python 3.11 to 3.14,
"Apache-2.0, MIT" per the repository page, v0.9.121 of 2026-11-26).

- Code is parsed with tree-sitter for about 40 languages, without a model
  (`GRAPHIFY_BACKEND=none`). Documents, PDFs and images go to a model provider; calu would
  switch that off.
- Edges for calls, imports and inheritance, labelled EXTRACTED (explicit in the source),
  INFERRED or AMBIGUOUS. Recent releases constrain a cross-file call to the imported name.
- The graph is a file inside the project (`graphify-out/graph.json`), where the supervised
  agent can write it. Whether another place can be chosen is not stated. The owner does not
  count this against it (section 5).
- Queries: an MCP server (`graphify-mcp` with `affected`, `get_neighbors`, `query_graph`) and
  the commands `affected` and `explain`, which show file and line.
- Updates: `graphify update .`, a watcher, git hooks on commit and checkout. The time of an
  update after one edit is not stated.
- Its installer writes a skill and PreToolUse and SessionStart hooks for Claude Code. It also
  offers a hosted MCP endpoint. calu uses neither.
- It had four releases in four days, so calu would pin a version.
- Its centre is a knowledge graph for the agent (communities, central nodes, documents). The
  reverse dependencies calu needs are one feature of it.

**Language servers over stdio** (pyright, basedpyright, ty, Pyrefly; typescript-language-server;
gopls; rust-analyzer; clangd; jdtls; kotlin-lsp).

- References as the language's own tools resolve them, and the server is told of each edit as
  it happens.
- Client library: SolidLSP (MIT, part of Serena) or `lsp-client`. Serena's application is
  GPL-3.0-or-later and cannot be imported into calu, which is GPL-2.0.
- One server per language and checkout, to install and pin. Gigabytes of memory on large
  repositories. jdtls, kotlin-lsp and rust-analyzer run build code of the project.
- Claude Code's own LSP servers cannot be queried from outside it.

Left out: SCIP indexers, Glean and Kythe (batch runs over the whole project), stack-graphs
(archived in 2025), GitNexus (non-commercial licence).

Known gaps of every static index: dynamic dispatch, `getattr`, dependency injection, programs
started by name from another language. The threat model is an agent that drifts, not a
determined attacker (honesty spec, section 1). The name search covers part of these gaps.

### Search first, then explain away (the owner's idea, 2026-11-27)

The owner: "How about combining free-form search and an LSP or other kind of index in a novel
way. If we have a method called SomeClass.add_user. We can now do a fulltext search for all
definitions and uses of it, but immediately remove everything we find where we can explain it
away because the LSP or code index tells us this is an add_user with a different qualified
name. The rest separates in clear cases ( LSP agrees ) and unclear cases. Only the latter
need to be looked at in detail."

The owner on this the same day: it is a third implementation behind the index's interface,
one that combines the slow, thorough one and the fast one. "I'm for it, but let's build the
base ones first and get it to work." So the order is: the word search (built,
`calu.refindex.SearchIndex`), a fast index on a language server or a code index, then the
combination below. The interface is `calu.refindex.ReferenceIndex` (requirement 52).

Claude's reading of the combination, for when it is built. The index is asked the forward
question about each hit of the word search ("what does the name at this place resolve to"),
and never the reverse question ("list the referrers"):

    hits = every place the bare name occurs as a word            (complete, far too many)
    for each hit:
        resolves to the changed unit                             -> referrer
        resolves to a unit the changed one overrides or
          implements (a base class, a Protocol)                  -> referrer
        resolves to another unit                                 -> explained away
        in a comment or a docstring                              -> explained away
        in a string that is the name or a dotted path to it      -> unclear
        no resolution (unknown receiver, missing dependency,
          no index for the language, index not ready)            -> unclear
    unclear hits go to the checker with the unit around them

What this changes against sections 3 and 4:

- **A weak or missing index fails safe.** An index is used to dismiss a hit or to confirm
  it, so an index that knows nothing leaves the hit unclear. Today's design reads a missing
  edge, or the empty list of a language server that is not ready, as "no referrer".
- **One procedure for every language.** With a language server the unclear rest is small.
  With a graph tool it is larger. With no index every hit is unclear, which is the name
  search of the owner's decision 3.
- **A dismissal needs proof.** Only a resolution by scope or type counts. A graph tool's
  match by bare name (41 of 100 call edges of codebase-memory-mcp in the spike) proves
  nothing and leaves the hit unclear. A wrong resolution is the one way this procedure
  misses a referrer.
- **Strings are where configuration lives.** A Hydra `_target_: pkg.mod.Class` is a dotted
  path in a YAML string, so it comes out unclear and the checker reads it. No tool of the
  spike links it.

The spike's numbers for the 50 sample functions (basedpyright, word search over `src/` and
`tests/`): 6,015 hits. 284 are references the server confirms, and 27 more go through a base
class or a Protocol. About 2,400 resolve to another unit of the same name and about 2,490 are
in strings or docstrings: both explained away. 445 have an unresolved receiver: unclear,
about 9 per function, most of them on the ten common names. The spike's report gave the two
large groups as round figures, so about 390 of the hits are not assigned to a group here.
Not measured: how many of the
hits in strings are the name itself or a dotted path, and the time of one resolution per hit.

### Language boundaries (the owner, 2026-11-27)

The owner: "what happens at language boundaries. A python class becoming dishonest through a
change in a rust library that it calls. I guess an LLM which inspects grep results figures
this out. A LSP? Hard, it would have to trace through things like symbol declarations and
obscure C preprocessor definitions. Maybe using grep and an LLM actually *is* the best way
for things that cannot easily be explained away. I don't think we can get to the 100% without
it."

Claude's reading, not decided: this is the division of work of the combination above.

    word search      finds the candidates, in every language
    language server  confirms or explains away the hits of its own language (in the spike
                     about 5,300 of 5,700 hits that were no reference)
    checker (LLM)    reads what is left: unresolved receivers, strings, and every hit in a
                     file of another language

A hit in a file the server does not cover is unclear by construction, so a Python caller of
a Rust function reaches the checker without any rule about language pairs.

**The word search finds a boundary only where the name survives it.** It does when Python
calls a PyO3 function or a C function by the name it is defined under. It does not when the
binding renames it (`#[pyo3(name = "add")]`), a macro builds the name, a program is started
with a sub-command (`tool add-user` for `add_user`), or the link is a route, a topic or a
method string (calu's own clients call `chat.send`, which `send_chat` serves). So the checker
has a second task at a contract change: to say under which other names the changed unit is
reached from outside its file (it reads the unit with its decorators and registration), and
calu searches for those names as well. Proposed as one more field of the honesty checker's
answer about a changed unit.

No procedure reaches 100 %: a name built at run time appears nowhere. The aim stays the one
of the honesty spec, an agent that drifts. What this division buys is that an error becomes
"one place too many was read" where a tool cannot decide, and that the checker's share stays
small enough to pay for at every contract change.

## 5. How calu and Claude Code use the index

**What the owner has fixed (2026-11-27):** "I would like the honesty checker to still work
even if we add no MCP servers and no tools to the underlying claude config. There might be
cases where the underlying claude config may not be touched. So calu's checkers needs to be
using the graph on their own, independent of how claude uses them, even if the index is
shared."

So:

- **calu queries the index itself**, in its own process or a child of it, through the
  indexer's command line or library. Nothing of this needs an MCP server, a tool or a hook in
  Claude Code. The honesty checker, the reviewer and the stale state work with Claude Code's
  configuration untouched.
- **Claude Code getting the index is a separate option**, off by default. The session can
  pass an MCP server in the options it starts Claude Code with, which edits no settings file.
  Where even that is unwanted, the option stays off and nothing changes for the checkers.
- **calu brings the index up to date itself** after each step and before it asks. It does not
  rely on a watcher or on Claude Code having queried the index.

**An index inside the project is acceptable (the owner, 2026-11-27):** "Graphiphy writing to
the project dir is not a hard exclusion criterion. Since calu checks every edit and tool
call, we can make it pretty hard to cheat by writing there. We do not try to be bullet proof
against malicious agents, just reduce the likelihood and impact of agents going off track."

Claude had proposed an index out of the reach of Claude Code's user. With the owner's
statement that is a preference where it costs nothing, and no condition. For an index that
the supervised agent could write, Claude proposes these protections, each from a mechanism
calu already has:

- **calu rebuilds before it asks.** A changed graph file is then replaced by calu's own run.
  The spike checks whether a rebuild trusts a cache that lies beside the graph.
- **A hand edit of the index waits for a person.** Its path is treated like generated code
  (D288): Edit, Write and MultiEdit of it escalate.
- **A command that writes there is the command checker's to judge.** The edit and command
  checkers are instructed to allow changes to the index only through the regular indexing
  run (the owner). With `changes.snapshot` on, the measured write set shows such a write.
- The index directory is left out of the honesty state, as generated files are.

The indexer parses text the supervised agent wrote, so it runs sandboxed: no network, the
project read-only apart from its own output directory, never as root outside a sandbox.

## 6. The spike before the choice, and the steps

**Spike**, on calu's own repository, for a sample of 50 functions and methods: the referrers
that the name search, codebase-memory-mcp, Graphify and a Python language server each report.
Measured:

- missed referrers, against the union of all four checked by hand;
- how soon an uncommitted edit and an untracked file are reflected;
- the time of a full indexing and of one after a single edit;
- whether an answer carries path, line and signature;
- whether the indexer runs inside bubblewrap, without a network;
- where the index and its caches lie, whether that can be chosen, and whether a rebuild
  replaces a graph file that was changed by hand;
- what it does with git worktrees.

- how each handles the languages the owner named (section 7, point 4), and whether any
  follows a Hydra `_target_` to the class it names.

The spike runs in a container, not on the owner's host. The result goes to
`spikes/RESULTS.md`.

**Steps** (PLAN phase 11):

1. The review marker. It needs nothing of the rest.
2. The spike.
3. Contract changes and the stale part of the state with the name search, shown under `track`
   and not enforced.
4. Stale units and stale marks judged by the review and at the gate.
5. The indexer the spike chose; the contracts of referenced units in the honesty prompts.
6. The index as an MCP server for Claude Code, as an option.
7. Evaluation cases (a contract change makes a caller dishonest; a change back restores it;
   an effect carried up three levels) and the documentation.

## 7. The owner's decisions of 2026-11-27

1. **The direction.** "Adopt it, but lets try to make this as granular as the code index
   allows. A module has many more callers / importers than a single dishonest functon may
   have. This needs to be handled on the call graph level not on a file level, at least if we
   *can* attribute honesty/dishonesty to nodes in the call graph. File-level marks are still
   useful, so let's not remove them outright."
2. **Enforcement.** "This is not changed, we consider the entire project honest or dishonest
   for the time being.. Later we can make this more granular ( so that certain entrypoints
   are honest and others are not, with the help of the call graph / code index ). Dishonesty
   can be resolved by editing, which can be done with the edit tool and other bash calls that
   don't invoke project code (most sed, grep, git, ruff, find commands for example are still
   allowed )"
3. **Common names.** "No cap, but of course the code index should be able to resolve
   duplicate names in different namespaces. A function's identity in the call graph should
   not be it's unqualified name, even if we use that to find candidate callers of it in the
   absence of a call graph/index."
4. **Languages and parsers.** "We should support many languages, but start with at least
   Python, Javascript, Typescript, Html, Markdown, Yaml (ideally with hydra, omegaconf
   support if possible ), C++, C, Rust, Go, Bash / sh shell scripts - let's try to go for
   good support and mostly leave this to the preexisting code indexer and to the checker LLM
   which likely knows most languages. Calu uses checker agents to make sense of code, we
   don't need to write parsers of our own. Where we need them, rely on the code indexer/call
   graph, LSP servers and the like, let's not write custom language parsers into calu itself
   unless strictly neccessary."
5. **An index inside the project.** All four protections of section 5. "The edit and command
   checkers are instructed to not allow changes to it except through official means ( e.g.
   when the code is being indexed in the regular way ). That's it."
6. **The marker.** It counts "Anywhere in the text".
7. **Order of work.** "Finish the old work first": step 6 of the temporary rules, then the
   stop-and-resume issue report, then this design, starting with the marker and the spike.

Still open: whether a subagent's marker counts, and the indexer, which the spike chooses.
