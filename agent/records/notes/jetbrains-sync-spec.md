# File synchronisation for the JetBrains plugin's SSH mode: specification

Written 2026-11-18 for requirement 35 (mode C of `pycharm-plugin-research.md`). This file is
the contract between the bridge `calu ide` (Python: `calu.ide.files`, `calu.ide.filestore`,
`calu.ide.filesync`; built 2026-11-18, section 13 lists where it deviates) and the plugin
(`extensions/jetbrains`, package `io.github.kadeng.calu.sync`). It extends section 2 of
`vscode-extension-spec.md`; change it when the build deviates.

## 1. Situation

    user's machine (often Windows)                         server (Linux), reached over SSH only
    ┌────────────────────────────┐   one SSH connection    ┌──────────────────────────────────┐
    │ PyCharm + plugin           │   NDJSON, JSON-RPC 2.0  │ calu ide --cwd <remote root>     │
    │ local project copy         │◄───────────────────────►│ remote project directory         │
    │ sync state, base contents, │  ssh -T -o BatchMode=yes│ calu serve, sessions, database,  │
    │ pending queue              │  <host> calu ide --cwd… │ Claude Code                      │
    └────────────────────────────┘                         └──────────────────────────────────┘

- The *local side* is the project copy the IDE edits. The *remote side* is the directory the
  agent works in. The plugin keeps them equal through the bridge: no second connection, no
  extra port, only the system's `ssh` client.
- A *relative path* is relative to the project root, with "/" separators. A *hash* is the
  SHA-256 of a file's bytes as 64 lowercase hex digits. The *base* of a path is the hash of
  the content both sides last agreed on.
- Setting of the plugin: **Sync: through calu / none (I sync myself)**. With "none" the plugin
  sends no `ide.fs.*` request (section 10).

## 2. Additions to the bridge

`calu ide --cwd <dir>`: the bridge changes to `<dir>` before anything else (an SSH command
starts in the home directory). A missing directory ends the bridge with exit code 2 and one
line on stderr. Without `--cwd` nothing changes.

| method | params | result |
|---|---|---|
| `ide.initialize` | as before | as before, plus `capabilities: [str]`. This spec defines `"cwd"`, `"ping"`, `"paths"`, `"fs"` (all requests of this table) and `"fs.watch"` (changes that no session reported are noticed too). A result without `capabilities` is an older bridge: the plugin says which calu version the server needs and does not sync. |
| `ide.ping` | `nonce?` | `{nonce, time}` (`time`: seconds since the epoch). The plugin sends it every 20 s; no answer within 10 s counts as a lost connection. |
| `ide.paths` | - | `{root, home, separator: "/", git: bool, head: str \| null, branch: str \| null, case_sensitive: bool}`. `root` is the resolved absolute project directory. |
| `ide.fs.manifest` | `scope: "all" \| "dirty" = "all"`, `paths?: [str]`, `cursor?: str` | `{head, files: [{path, size, mode, sha256}], skipped: [{path, reason}], missing: [str], next_cursor: str \| null}`, section 3 |
| `ide.fs.read` | `path`, `offset = 0`, `length = 1048576` | `{content_b64, sha256, size, mode, eof}`: `sha256` and `size` describe the whole file at the time of this request. A reader of several pieces starts again when `sha256` changes between two of them. |
| `ide.fs.write` | `path`, `content_b64`, `base_sha256: str \| null`, `mode?: int`, `during_turn = false` | `{sha256, size}`. The whole content in one request, at most 1 MiB raw. Rules in section 4. |
| `ide.fs.write.begin` | `path`, `base_sha256`, `size`, `sha256`, `mode?`, `during_turn = false` | `{upload_id, chunk_size}`. Checks path, size limit, base and hold; changes no project file. |
| `ide.fs.write.chunk` | `upload_id`, `offset`, `content_b64` | `{received}` (bytes so far). `offset` must equal the bytes received so far. |
| `ide.fs.write.commit` | `upload_id` | `{sha256, size}`. Checks the announced `size` and `sha256` and the base once more, then replaces the target in one step. |
| `ide.fs.write.abort` | `upload_id` | `{}` |
| `ide.fs.delete` | `path`, `base_sha256`, `during_turn = false` | `{}`. Empty parent directories are removed up to the root. |
| `ide.fs.rename` | `from`, `to`, `base_sha256`, `during_turn = false` | `{}`. `base_sha256` is the hash of `from`; `to` must not exist (-32038). |
| `ide.fs.subscribe` | `after_seq?: int`, `epoch?: str` | `{epoch, seq, complete: bool}`; from then on `ide.fs.changed` notifications, section 5 |
| `ide.fs.unsubscribe` | - | `{}` |

Notification:

| method | params |
|---|---|
| `ide.fs.changed` | `{epoch, seq, changes: [{path, before_sha256, after_sha256, deleted, origin}], skipped: [{path, reason}]}`; `origin`: `"agent"` (a tool call of a session), `"ide"` (an `ide.fs.*` request of this connection), `"other"`; `skipped`: paths that became ones the bridge does not synchronise, with the reason of a manifest's `skipped` (added 2026-11-19; a bridge before that sends no `skipped`) |

New error codes (-32010 to -32018 are taken; -32019 to -32029 stay free for other additions):

| code | name | when | `data` |
|---|---|---|---|
| -32030 | path refused | the path is absolute, has a `..` segment, resolves outside the root, or is in an excluded directory | `reason: "outside" / "excluded" / "invalid"` |
| -32031 | base mismatch | the file's hash is not `base_sha256`, or the file exists and `base_sha256` is null, or the reverse | `sha256` (null if no file), `size`, `mode` |
| -32032 | too large | the content is above the per-file limit, or one request carries more than 1 MiB | `size`, `limit` |
| -32033 | not a regular file | the path is a directory, a symbolic link, a device, a socket | `kind` |
| -32034 | not found | `ide.fs.read` or `ide.fs.rename` of a missing file | - |
| -32035 | held | the agent edits the file in a running turn and `during_turn` is false | `session_id`, `sha256` |
| -32036 | upload failed | unknown or expired `upload_id`, wrong `offset`, size or hash differ at commit | `reason`, `received` |
| -32037 | I/O error | the operating system refused (permissions, disk full, read-only) | `errno`, `strerror` |
| -32038 | target exists | `ide.fs.rename` to an existing path | `sha256` |
| -32039 | no manifest | the file listing failed (git error, unreadable directory) | `reason` |

## 3. The manifest

- In a git repository the files are those of `git ls-files -co --exclude-standard -z`: tracked
  and untracked files, `.gitignore`, `.git/info/exclude` and the global excludes honoured.
  Outside a git repository: a walk of the directory that skips the excluded directories
  (section 9) and the built-in list `node_modules`, `.venv`, `venv`, `__pycache__`, `.tox`,
  `.mypy_cache`, `.ruff_cache`, `.idea`; `head` is null. Git submodules are skipped (`reason:
  "submodule"`).
- `scope: "dirty"` (git only): only files that differ from `HEAD` or are untracked, as `git
  status --porcelain=v2 -z --untracked-files=all` reports them. Tracked files that are gone
  are in `missing`.
- `paths`: only these paths (files or directories). A requested file that does not exist is
  in `missing`.
- `skipped`: symbolic links (`"symlink"`, never followed), files above the per-file limit
  (`"too-large"`), other non-regular files (`"not-regular"`), submodules. The plugin shows
  them in the sync status and never writes to them.
- Paging: at most 2000 entries per response; `next_cursor` continues. `head` is the same in
  every page of one listing; a listing whose `HEAD` moved between pages is started again.
- Cost: the bridge caches hashes by (path, size, mtime in ns, inode); a second full manifest
  of an unchanged tree reads no file content.

## 4. Writes, sizes and chunking

| limit | value | where |
|---|---|---|
| raw content per request or response | 1 MiB (1 048 576 bytes); about 1.4 MiB as base64 | fixed |
| one NDJSON line, either direction | at most 2 MiB; a longer line ends the connection | fixed |
| per-file limit | 32 MiB, setting `ide.sync_max_file_mib` (bridge) and `sync.maxFileMib` (plugin); the smaller wins | `skipped`, -32032 |
| merge limit (plugin) | 4 MiB: above it two changed sides are a conflict without a merge attempt | plugin |
| manifest page | 2000 entries | fixed |
| open uploads per connection | 4; an upload without a request for 120 s is discarded | bridge |

- **Base check.** `base_sha256` must equal the hash of the file as it is; `null` means "the
  file must not exist". Otherwise -32031 with the hash that is there. The check and the
  replacement happen without another write of the bridge between them.
- **Atomic.** The content goes to a temporary file in the target's directory (name
  `.calu-upload-<id>`, never listed, never reported) and replaces the target with one rename.
  A reader on the server sees the old or the new content, never a part.
- **Chunked upload: begin / chunk / commit**, not `offset` + final flag on `ide.fs.write`:
  the base check and the replacement are one request (commit), an abandoned upload leaves the
  target untouched, two uploads cannot mix their pieces, and the bridge can refuse an upload
  (size, hold, base) before a megabyte is sent. The plugin uses plain `ide.fs.write` up to
  1 MiB and the three requests above it.
- **Mode.** `mode` holds the permission bits (0o777 mask). Omitted: an existing file keeps its
  mode, a new file gets 0o644. A Windows plugin never sends `mode`.
- **Binary files** are transferred like any other (base64 is byte-exact) and are never merged.
- **Content is synced byte for byte.** No newline conversion, no encoding conversion, no BOM
  handling on either side. The plugin switches the IDE's line separator handling off for
  synced files or keeps the separators it found.

## 5. Changes from the remote side

Sources of `ide.fs.changed`, in order of exactness:

1. the attached session's edit tools: `edit.diff` and the tool's result (`origin: "agent"`,
   `before_sha256` exact);
2. the session's write sets (`calu.write_sets`, D31): what a command wrote, measured after it;
3. a watcher for everything else (a command without a measured write set, another process,
   the user in an SSH shell). The Python agent decides: the `watchfiles` package (inotify), or
   polling `git status --porcelain=v2` plus size and mtime every 2 s. Without a watcher the
   bridge does not announce `"fs.watch"`, and the plugin compares manifests every 60 s and
   after every `turn.result`.

Rules:

- Changes are batched (200 ms quiet, at most 500 per notification). `seq` grows by 1 per
  notification, starting at 1. `epoch` is a random id of this bridge process.
- Paths that the manifest would not list (ignored, excluded) are not reported. A path the
  manifest would list as skipped (above the size limit, not a regular file, a link, a
  submodule, an unsupported name) is reported in `skipped` once, when it becomes skipped or
  its reason changes, so that the editor can list it at once; a regular file that becomes
  skipped is not reported as deleted. A skipped path that goes away is not reported (the
  next full comparison drops it from the editor's list).
- The bridge keeps the last 1000 notifications. `ide.fs.subscribe {after_seq, epoch}`
  answers `complete: true` and replays the later ones if the epoch is its own and nothing
  after `after_seq` was dropped; else `complete: false`.
- **Resync rule.** The plugin runs a full comparison (manifest, section 7) when
  `complete` is false, when a notification's `seq` is not the last one plus 1, or when the
  `epoch` changes. In mode C a lost SSH connection ends the bridge, so a reconnect normally
  meets a new epoch and resyncs; the hash cache and `scope: "dirty"` keep that cheap.
- A change with `origin: "ide"` is the echo of the plugin's own request; the decision table
  gives "nothing" for it because its hash already is the base.

## 6. The decision table

`b` = base, `l` = local hash, `r` = remote hash, `-` = no file (for `b`: never synced). L and R
differ from B and from each other. Implemented in `DefaultSyncEngine` (same table in its KDoc).

| b | l | r | decision | if the path is held |
|---|---|---|---|---|
| - | - | - | nothing | |
| - | L | - | Upload (base null) | Hold |
| - | - | R | Download | |
| - | L | L | Settle: base := L | |
| - | L | R | Conflict `created-both` | Hold |
| B | B | B | nothing | |
| B | B | R | Download | |
| B | L | B | Upload (base B) | Hold |
| B | L | L | Settle: base := L | |
| B | L | R | Merge; above the merge limit Conflict `too-large` | Hold |
| B | B | - | DeleteLocal | |
| B | - | B | DeleteRemote (base B) | Hold |
| B | L | - | Conflict `deleted-remotely-modified-locally` | Hold |
| B | - | R | Conflict `modified-remotely-deleted-locally` | Hold |
| B | - | - | Settle: the state forgets the path | |

- **Merge**: line-based three-way merge (diff3) of base content, local and remote. A clean
  result is written to both sides (locally through the open document, so undo works). One
  binary content gives Conflict `binary`; overlapping changes or a missing base content give
  Conflict `both-modified`.
- **Conflict**: the IDE's merge dialog (three panes for `both-modified`; for the other kinds
  a choice "keep mine / take theirs / show diff"). Until the user decides, neither side
  is changed, and the file has a "conflict" mark in the project view and the sync status.
- **Unsaved text in the editor counts as a local change**: a remote change of such a file is
  merged into the document (or is a conflict), never written over it.
- **Base contents.** The sync state stores hashes. The plugin keeps the content of every file
  it downloaded, uploaded or merged in a bounded cache in the IDE's system directory, keyed by
  hash; for a file untouched since the first copy the base content is the file at the common
  commit, read from the local repository.

### Holds during a running turn

A path is *held* while (a) the attached session's `session.state` is `running`, `checking`,
`tool_call` or `escalated`, and (b) the path occurred since the turn's `user.input` in an
`edit.diff`, in a `tool.use` of an edit tool (its file path), or in `tool.files`. The hold
ends with `turn.result` or a state of `idle`, `stopped` or `failed`; then the held paths are
decided again with a fresh manifest of those paths.

- Held uploads wait in the pending queue; downloads go on, so the editor shows the agent's
  edits as they happen (merged into unsaved text when that is clean).
- What the user sees: a status bar entry "calu sync: 2 files wait for the agent's turn", a
  mark on the editor tab, and the action "Upload now", which sends the request with
  `during_turn: true` (the base check still applies).
- The bridge enforces the same rule for the attached session (-32035), so an older or faulty
  plugin cannot write into a file the agent is editing by accident.

## 7. First copy, catch-up, offline queue

**First copy and catch-up** (both sides are git repositories):

| local copy | what the plugin does |
|---|---|
| no directory, or empty | offers "Clone over SSH" (`git clone <host>:<root>` in a terminal, or an rsync/scp done by hand), then continues below |
| same commit as the remote `HEAD` | syncs the dirty overlay: `ide.fs.manifest {scope: "dirty"}`, the local `git status`, and for the union of both path lists the table of section 6 with the content at the common commit as base |
| another commit | shows both commits and syncs nothing. Offers: "fetch and check out the remote commit" when the working tree is clean (in the IDE's git, visible to the user); otherwise the user brings the commits together with git and presses "Retry" |
| not a git repository (either side) | full manifest; equal files settle, different ones are `created-both` conflicts, files of one side only are listed in a confirmation dialog before anything is copied |

Nothing is overwritten silently: the first run after "Enable sync" shows the plan (counts and
the list of downloads, uploads, deletions, conflicts) and waits for "Apply". A plan that
would delete more than 20 files or 10 % of the tracked files asks again later as well. When
the remote `HEAD` moves (the agent committed), the plugin shows it and offers the fetch; the
changed files arrive through the normal sync either way.

**Offline queue** (connection lost, or a path is held):

- Every local save, delete or rename is recorded in the pending queue, which is persisted.
  It stores paths, not contents. Repeated saves of one path are one entry at the place of the
  first; a rename is a delete of the old path and a save of the new one.
- Replay after the reconnect: subscribe, take a manifest of the queued paths, decide each
  queued path in queue order with the table of section 6, then run the full comparison.
- If the base moved meanwhile (the agent changed the file), the table gives Merge or
  Conflict instead of Upload: the queued change is never sent over the agent's work. An entry
  leaves the queue when its action was carried out or the user resolved its conflict.
- While offline the status bar says "calu sync: offline, n changes queued".

## 8. Path mapping

| topic | rule |
|---|---|
| roots | local root = the IDE project's base directory; remote root = `ide.paths.root`. Only relative paths cross the connection. |
| separators | "/" in the protocol and the sync state; the plugin converts at the local file system |
| case | on a case-insensitive local file system, paths (or their directories) that differ only in letter case are not synced; both are listed as "case collision" |
| names Windows cannot hold | `< > : " \| ? *`, a trailing dot or space, device names (`CON`, `NUL`, `COM1`, ...): not synced, listed |
| newlines, encodings | byte-exact (section 4) |
| file modes | Linux and macOS: sent with uploads, applied to downloads. Windows: not sent; the remote mode is kept. A change of the mode alone is not synced (v1). |
| symbolic links | not followed, not created, reported in `skipped` |
| empty directories | not synced (git does not track them either) |

## 9. Security requirements

- Every path of a request is resolved on the server; the result must lie inside the project
  root, and no component of it may be a symbolic link that leads out of the root. Otherwise
  -32030. The plugin applies the same rule to paths it receives before it touches a local file.
- Excluded from every `ide.fs.*` request and from the manifest: `.git` at any depth, the
  trace directory `agent/traces` (requirement 36), and calu's own state and credentials
  (the XDG directories of `calu.paths`, wherever they lie).
- The bridge writes as the user who started it: no sudo, no other account, no credential
  beyond those of the rest of the bridge (section 2 of `vscode-extension-spec.md`).
- Writes of the IDE are the user's own edits, like typing in an SSH shell: no checker judges
  them. The bridge reports each of them to the sessions of the project directory, and the
  session records them as user edits, so that a command's write set and the honesty state do
  not attribute them to the agent. (The request for that report is the Python agent's choice.)
- The bridge logs each write, delete and rename (path, hashes, size) to `ide.log`'s file.
- File content from the server is data: the plugin never executes it and never lets it decide
  a path outside the local root.

## 10. Alternatives the owner named, and when each applies

| option | needs | verdict |
|---|---|---|
| Through the bridge (this spec) | the `ssh` client only | the default: per-edit, exact conflicts, holds during a turn |
| PyCharm Deployment / SFTP | PyCharm Professional; no public API for a plugin | the user may configure it himself (automatic upload) and set **Sync: none**; whole files, no holds, conflicts by hand |
| rsync / scp | not on stock Windows (rsync); scp copies whole trees without deletions | fine for a one-off first copy by hand; not for the running sync |
| Mutagen | the user installs it on his machine | two-way with its own conflict handling; use with **Sync: none**; not bundled (binary size, licence of official builds) |
| git push / pull | git on both sides | too coarse per edit and needs commits; right for the catch-up to a common commit (section 7) |

With **Sync: none** the plugin still shows remote diffs from `edit.diff` and opens files by
mapped path; it says in the status bar that the local copy is not kept in sync by calu.

## 11. Sequences

First sync (same commit on both sides):

    plugin                                   bridge
      │ ide.initialize ─────────────────────►│ capabilities: [.., "fs", "fs.watch"]
      │ ide.paths ──────────────────────────►│ {root, git: true, head: H}
      │ (local HEAD == H)                    │
      │ ide.fs.subscribe ───────────────────►│ {epoch: E, seq: 0, complete: true}
      │ ide.fs.manifest {scope: "dirty"} ───►│ {head: H, files: [a.py, new.md], ...}
      │ plan; the user confirms              │
      │ ide.fs.read {a.py} ─────────────────►│ {content_b64, sha256, eof: true}    Download
      │ ide.fs.write {b.py, base: hB} ──────►│ {sha256}                            Upload
      │◄── ide.fs.changed {seq 1, [b.py, origin "ide"]}       echo: nothing to do

The agent edits a file that is open in the IDE:

    session ──edit.diff a.py──► bridge ──session.event──► plugin   (held += a.py)
                                bridge ──ide.fs.changed {seq 7, [a.py, hB → hR, "agent"]}──► plugin
      plugin: b = hB, l = hB, r = hR → Download
      │ ide.fs.read {a.py} ─────────────────►│ {sha256: hR}
      │ document replaced as one undoable command; base := hR
      (unsaved text in the editor: merged into the document, or the merge dialog)

The user saves a file while the agent edits it:

    plugin: save a.py (hL), a.py is held → Hold; queue [a.py]; status "waits for the turn"
    bridge ──ide.fs.changed {seq 8, [a.py, hB → hR]}──► plugin: held → nothing is written
    session ──turn.result──► plugin: hold ends
      │ ide.fs.manifest {paths: [a.py]} ────►│ {files: [a.py hR]}
      plugin: b = hB, l = hL, r = hR → Merge
        clean:    document := merged (hM)
      │ ide.fs.write {a.py, base: hR} ──────►│ {sha256: hM}          base := hM
        conflict: merge dialog; the result is uploaded the same way
      (had the plugin uploaded with base hB:  ◄── error -32031 {sha256: hR} → same decision)

Reconnect:

    connection lost → status "offline"; saves go to the queue
      │ ssh ... calu ide --cwd <root>        (a new bridge process)
      │ ide.initialize, ide.paths ──────────►│
      │ ide.fs.subscribe {after_seq: 8, epoch: E} ─►│
      │      same bridge, nothing dropped:   │ {epoch: E, seq: 11, complete: true}
      │◄── ide.fs.changed seq 9, 10, 11      │      then: replay of the queue
      │      new bridge or a gap:            │ {epoch: E2, seq: 0, complete: false}
      │ ide.fs.manifest ────────────────────►│      then: replay of the queue, full comparison

## 12. What exists in the plugin

Built and unit-tested (`extensions/jetbrains/shared`, package `io.github.kadeng.calu.sync`,
Kotlin and JDK only, no IntelliJ import):

| part | file | state |
|---|---|---|
| types (`FileEntry`, `Manifest`, `RemoteChange`, `SyncAction`, `ConflictKind`), `SyncState` with `encode`/`decode` | `Types.kt` | built, tested |
| decision table: `plan`, `decide`, `onRemoteChanged`, `onLocalSaved`, `replay`; the two size limits | `SyncEngine.kt` | built, tested (every row, holds, limits) |
| pending queue with coalescing and its text form | `PendingQueue.kt` | built, tested |
| path rules and mapping, case collisions, Windows names | `PathMapper.kt` | built, tested |
| diff3 text merge, binary check, `mergeContents` | `Merge.kt` | built, tested |
| `FileStore`, `BaseContents`, `SyncRunner` (applies a plan to two stores) | `FileStore.kt` | built, tested with in-memory stores |
| the held set from the chat's view state (files an edit tool touched in the running turn) | `Held.kt` | built, tested |

Not built:

- (built since: sections 2 to 5 on the Python side, see section 13);
- the plugin's `FileStore` over the bridge (paged manifest, chunked read and write) and over
  the local project (VFS, documents, undo);
- the status bar entry, the conflict marks, the merge dialog, the confirmation of the first
  plan; the VFS listener and applying remote changes through the open document;
- the dirty-overlay comparison and the commit check of section 7, the persisted base content
  cache (only an in-memory one exists), persisting `SyncState` and the queue;
- `Manifest.skipped` and `missing`, file modes in the state, and the `origin` of a change are
  not modelled in the Kotlin types yet.

## 13. Deviations of the Python side (2026-11-18)

Built in `src/calu/ide/files.py` (rules without file access), `filestore.py` (file access) and
`filesync.py` (requests, watcher, notifications); tests in `tests/unit/test_ide_files.py`,
`test_ide_filestore.py`, `test_ide_fs_bridge.py` and `test_ide_user_edit.py`. Everything not
listed here is as sections 2 to 9 say.

| topic | the spec | what the bridge does, and why |
|---|---|---|
| links in directories (§9) | refused if the link leads out of the root | Every directory on the way is opened with `O_NOFOLLOW`, one `openat` per segment, so a link is never followed, also one that stays inside: -32033 `kind: "symlink"` for it, -32030 `reason: "outside"` for one that leads out. A check of the resolved path could be outrun by a directory that is replaced meanwhile; no manifest lists a path through a link anyway. |
| path form (§8, -32030) | "resolved on the server" | A path is accepted or refused, never repaired. `reason: "invalid"`: not a string, empty, a control character (NUL included), a backslash, an empty or `.` segment (`a//b`, `./a`, `a/`), more than 4096 bytes, a segment above 255 bytes. `reason: "outside"`: absolute, a drive letter (`C:`), a `..` segment. |
| names the protocol cannot carry | - | A file whose name has a control character, a backslash or bytes that are not UTF-8 is in `skipped` with the new reason `"unsupported-name"`. |
| excluded (§9) | `.git`, `agent/traces`, the XDG directories | `.git` at any depth and in any letter case; `agent/traces` always; the trace directory the attached session reports (another `traces.directory`); calu's XDG directories where they lie inside the root; every name that starts with `.calu-upload-`. |
| per-file limit (§4) | setting `ide.sync_max_file_mib` | No setting: the bridge often has no database login. The limit is 32 MiB; `ide.paths` reports it in the new field `limits: {file_bytes, request_bytes, page_entries, uploads}`. The plugin's smaller limit wins as before. |
| line limit (§4) | 2 MiB, a longer line ends the connection | The bridge still reads lines up to 64 MiB, because `session.call` carries chat messages with images. It writes no file-related line above 2 MiB: a manifest page and a notification end early when their paths are long (fewer than 2000 entries or 500 changes), and `content_b64` is refused by its length before it is decoded. |
| `ide.fs.manifest` | paging by cursor | The cursor is opaque. The bridge keeps the listings of the last 4 manifests with further pages. -32039 `reason`: `"git"`, `"not-git"` (dirty scope outside git), `"too-many-files"` (more than 200 000), `"unreadable"`, `"cursor-expired"`, `"head-moved"` (HEAD changed since the first page: ask again from the start). `missing` is filled on the first page only. `paths` holds at most 2000 entries; `paths: []` selects nothing. A named path that exists but is ignored by git is in neither list. Outside git a FIFO, socket or device is in `skipped` (`"not-regular"`); git does not list such files at all. A bridge started in a subdirectory of a repository lists that subtree. |
| `ide.fs.read` | `length` default 1 MiB | A larger `length` is read as 1 MiB; the caller continues at `offset + size of the content` until `eof`. |
| `mode` | "0o777 mask" | A value outside 0 to 0o777 is -32602, not masked. |
| `base_sha256` | `str \| null` | The key must be present in `ide.fs.write` and `ide.fs.write.begin`; absent is -32602, so that a forgotten base never creates a file. For delete and rename it must be a hash. |
| -32031 | hash mismatch | Also for `ide.fs.delete` of a file that is gone (`sha256: null`). |
| -32032 | content above the limit | Also for a write, delete or rename whose target on the server is above the per-file limit (it cannot be compared). |
| -32033 `kind` | directory, symbolic link, device, socket | `"directory"`, `"symlink"`, `"fifo"`, `"socket"`, `"device"`, and `"file"` for a path below a regular file (`a.txt/b`). |
| chunked upload | begin / chunk / commit / abort | The temporary file and the target's missing directories are created with the first chunk, so `begin` changes nothing; abort, expiry and every failed commit remove both. Any failed commit ends the upload. -32036 `reason`: `"unknown-upload"`, `"offset"`, `"size"`, `"sha256"`, `"too-many-uploads"`. `ide.fs.write.abort` of an unknown id answers `{}`. The hold is checked at begin and at commit. |
| `ide.fs.rename` | `to` must not exist | Done with `link` + `unlink`, so an existing target is never replaced; the hold applies to both paths; the source's empty directories are removed. |
| the watcher (§5.3) | `watchfiles`, or polling `git status` | Polling; `watchfiles` is not a dependency of calu and is not added. Every 2 s (20 times the last scan's duration if that is more, at most 30 s) the bridge lists the files as a manifest does and compares size, mtime (ns) and inode; only files whose signature changed are read. A change of the mode alone is not noticed. A scan also starts at once after a `tool.result`, a `turn.result`, a state of idle, stopped or failed, and a change of the session connection. `"fs.watch"` is always announced. |
| `origin: "agent"` (§5.1, §5.2) | the edit tools' events; the write sets | `edit.diff` arrives before the edit runs, so the bridge reads the file after the call's `tool.result` instead of trusting the event's text. A change is `"agent"` if an edit tool call of the attached session named the file, or if it was found while a `Bash` call of that session ran or in the scan after it. The second rule is a guess: the session protocol has no event that carries a measured write set. Everything else is `"other"`, also the writes of another bridge on the same directory. |
| `before_sha256` | exact | The last hash this bridge computed for the path (manifest, read, scan, own write); null if it never read the file. |
| hash cache (§3) | (path, size, mtime, inode) | In memory, for the life of the bridge process. A file whose mtime is less than 2 s older than the moment it was hashed is not cached: a second write within the same clock tick would keep the signature. Proposal, not built: keep the cache in `$XDG_STATE_HOME/calu` across bridge processes, so that the first manifest after a reconnect reads nothing either. |
| subscription (§5) | replay by `after_seq` and `epoch` | A subscription without `after_seq` and `epoch` answers `complete: true`. `ide.fs.unsubscribe` stops the watcher, so a later subscription that names a sequence number answers `complete: false`. The subscription belongs to the editor's connection: it goes on while the bridge reconnects to the session, and after the session is lost (changes then have the origin `"other"`). |
| holds (§6) | enforced for the attached session | From the same events as the plugin's set. Nothing is held after `turn.result`, in the states idle, stopped and failed, after `ide.link lost`, and after a reconnect that missed events (the state is read again, the turn's files are forgotten). `data.session_id` is "" for a session that was attached by address and has no id. |
| report of IDE writes (§9) | to the sessions of the project directory | New request of the session protocol: `files.user_edit {paths}` → `{excluded}`. The bridge sends it to the **attached** session only, after the base matched and before the file is replaced (it waits at most 2 s for the answer), and once with no paths at the attach to learn `excluded`. The session leaves the paths out of the write set of every command whose window is open then or opens within 10 s, watches the files for the honesty state (unreviewed), and logs one line. A session of the directory that is not attached is not told. |
| end of the connection | - | When stdin ends, requests that are still being answered are cancelled and their answers are not written. A client that wants the answers sends `ide.shutdown` last and reads until the process ends. Open uploads are removed in both cases. |

## 14. The plugin's I/O side as built (2026-11-19)

Section 12's "Not built" list is done except for what follows. Code: `shared/sync`
(`RemoteFiles.kt`, `LocalFiles.kt`, `GitIgnore.kt`, `Synchronizer.kt`, `SyncDriver.kt`) and
`backend` (`VfsLocalFiles.kt`, `CaluSync.kt`); tests `SyncIoTest`, `SynchronizerTest`,
`RealBridgeSyncTest`; the walk in the IDE is section H of `test-plan.md`.

| topic | the spec | what the plugin does, and why |
|---|---|---|
| first copy (§7) | `git clone <host>:<root>` in a terminal, then the dirty overlay against the common commit | "Clone from Server": the full manifest and every listed file through the bridge, after a confirmation. The copy has no `.git` (it is excluded from every request), so nothing compares commits; the base of every file is the content that was copied, kept by hash in the plugin's state directory. git operations happen on the server |
| which local files take part | - | regular files below the root without `.git`, `.idea`, `.hg`, `.svn`, the excluded directories, upload leftovers, links, and what the copy's own `.gitignore` files name (read by the plugin, because the copy has no git). A file that the server's git ignores by another rule is uploaded once and then listed as "not listed by the server" |
| deletions from a listing (§7) | a plan that deletes more than 20 files or 10 % asks | also: a file that is missing from a full manifest is deleted locally only if a manifest of exactly that path reports it in `missing` (an ignored file is in neither list) |
| every decision | from the notification or the plan | from what both sides hold at that moment: the local file is hashed again, and a write that the bridge refuses with -32031 is decided again with the hash it reports (at most three times per path in one go) |
| unsaved text (§6) | counts as a local change | merged with a remote change (the merged text is saved); a remote deletion against unsaved text is a conflict; unsaved text alone is never uploaded |
| conflicts (§6) | merge dialog; a mark in the project view and on the tab | a card in the chat, a notification with "Merge…", the status bar; the IDE's three-way merge dialog for texts, "Keep Mine / Take Theirs" for a deletion against a change, binary and large files. No marks in the project view or on tabs |
| holds (§6) | status, a mark on the tab, "Upload now" | the status bar and a line in the chat with "Upload now" (all held files); no mark on the tab |
| `ide.ping` (§2) | every 20 s | not sent: `ServerAliveInterval` of `ssh` ends a dead connection after about 45 s |
| local changes | a listener for saves | the VFS listener with 300 ms of quiet; a rename of a file whose content is its base is one `ide.fs.rename`, every other move is a deletion and an upload |
| line ends (§4) | byte for byte | a file that is open in an editor is changed through its document (undo works) and saved; if the saved bytes differ from the wanted ones (other line ends, a BOM) the bytes are written to the file and the document is reloaded |
| limits (§4) | setting `sync.maxFileMib` | no setting: the bridge's limit of `ide.paths` (32 MiB) |
| progress | - | a background progress indicator with a cancel button and no percentage (the platform's functions for one are internal API in 2026.3); the status bar counts the files left |

