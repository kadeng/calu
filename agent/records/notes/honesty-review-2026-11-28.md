# Honesty and writing review of the whole repository (2026-11-28)

The owner asked for "a full honesty review over the entire codebase, according to the
honesty skill", with Haiku to scan and Sonnet or Opus to restore honesty, and in the same
pass a scan "for AI slop in documentation". The main README was to stay as it is.

## How it ran

- The repository was split into 41 parts of about 7,000 lines: Python in `src/`, the VSCode
  extension, the PyCharm plugin, the Python, TypeScript and Kotlin tests, the scripts,
  spikes and UI-test tools, and the user documentation (`DOCUMENTATION.md`,
  `docs/TESTING.md`, the extensions' READMEs, `uitest/README.md`, `LICENSE.md`).
- A Haiku scanner read each part whole and listed findings: 852 in all. The brief asked for
  real mismatches between name, documentation and behaviour, and for the writing faults the
  owner named (style over substance, compressed text, a smart-alecky tone, sentences that
  start with a number word, a question word or the object, long sentences and enumerations
  full of verbs).
- Fixers checked each finding against the code before they changed anything: Opus for
  `session.py`, `cli.py`, the gate and honesty modules, the protocol and the run and serve
  modules; Sonnet for the rest. Each fixer edited only the files of its part. They
  rejected 16 findings as wrong and acted on the others, mostly by correcting docstrings
  and documents.
- Afterwards: ruff, pyright, 3,023 unit tests, 313 database tests, 394 tests of the VSCode
  extension and the PyCharm plugin's `gradle check` all pass.

The briefs and the per-part findings and reports were kept in `/tmp` for the run and are
not part of the repository.

## Behaviour changes

The fixers were told to change code only for a small, obvious slip that the documentation
already describes. These are the changes that go beyond text:

| Where | Change |
|---|---|
| `src/calu/escalations.py` `_wait_for_decision` | A row the poll finds cancelled is reported as cancelled, not as a denial, as `ask` promises |
| `src/calu/checkers/shell.py` `_normalise` | Also catches `ValueError`, so a NUL in a path no longer escapes `parse_command` |
| `src/calu/herd.py` `close_gone` | Skips a session row without a pid |
| `src/calu/human_diffs.py` `record` | A short write raises `HumanDiffError` |
| `src/calu/refindex/cbm.py` `CbmIndex._run` | A non-zero exit of codebase-memory-mcp is a failure; before, its output was read as an answer |
| `src/calu/claude/` `settings_permission_mode` | Reads `$CLAUDE_CONFIG_DIR/settings.json` when that variable is set |
| `src/calu/tui/` `describe_commit` | Refuses a revision that starts with `-` |
| `src/calu/tui/render.py` `reset_text` | Converts the reset time to UTC, as documented |
| `src/calu/uninstall.py` `remove` | Reports "already gone" for an item that was not there |
| `extensions/vscode/pane/webHost.ts` | The socket launcher waits `STOP_TIMEOUT_MS` (5 s) for its end, not 3 s |
| `extensions/vscode/src/bootstrap.ts` | The install dialog shows `--force` in the command |
| `extensions/vscode/webview/items.ts`, `settingsCards.ts` | The note element only for approval cards; "No temporary rule is active." |
| PyCharm plugin: `Approval.kt`, `Json.kt`, `Installer.kt`, `Synchronizer.kt`, `RemoteFileStore.kt` | A non-default sync mode is named in the approval text; `asLong` returns null outside the Long range; the installer's timer thread; a batch that goes offline queues its paths; the size check comes first |

One change was taken back: a fixer had narrowed the plugin's list of held files to the turn's
own holds, and `SynchronizerTest` showed that the bridge's refusals during a turn are held
too. A KDoc that wrote a star followed by a slash ended its comment early; it was reworded.

## Process slips

Four fixers made some of their edits with a short Python script instead of the Edit tool,
against the brief: the parts 14, 24/25, 33/34, 37, 39 and 41 say so in their reports. The
results passed the same checks as the rest.

## Questions for the owner

Behaviour the fixers did not decide and documented as it is:

- `checkers/render.py` `inline` cuts before it cleans, so leading white space can shorten
  the result. `checkers/rules.py` `BUILT_IN_COMMAND_DENY` is unused: remove it or use it?
- `cli.py` `_text_setting`: the conflict check before a save is not a compare-and-set.
  `_traces_finalize` in the worktree mode may ask for a password on a terminal.
- `context/builder.py` `fold`: a failed `on_summary` leaves the new summary in memory with
  the turns unfolded.
- `doctor.py` `sandbox_posture` uses the JSON of a failing `claude sandbox status`.
- `honesty.py`: `diff_paths` drops files whose name git quotes; `content_sha256` takes no
  lock; the whitespace shortcut of `review_plan` compares with the last verified content.
- `localsocket.py` `bind` leaves an untrusted socket file in place after the check fails.
- `recorder.py`: a step whose row cannot be written gets no step time; the parameter `route`
  of `checker_run` is unused.
- `refindex/judge.py`: places in a file over `WHOLE_FILE_BYTES` are judged on their line
  alone, and the cache key leaves out the unit's definition text.
- `session.py` `pre_tool_use`: an exception after the first stage of `_checked` reaches the
  SDK hook instead of becoming ASK.
- `traces/finalize.py`: a trace file that is not UTF-8 stops the whole run.
- `tui/actions.py` `comment` does not check that the path stays inside the workspace.
- `write_sets.py` `review`: an overlapping write set with a measuring error asks nobody; the
  write set is measured also with `changes.review` off.
- VSCode: Enter in the custom model field applies the model to every role at once.
- PyCharm plugin: `linkTarget` refuses a relative `[x](app.py:12)` link; `usageLimitLine`
  reads values above 1 as percents; `holdEnded` does not check the workspace; `guarded`
  lets other exceptions than `FsException` and `IOException` escape; a path noted after an
  I/O failure is not queued.
- Tests: `tests/conftest.py` loads `~/.config/calu/.env` for every run;
  `tests/unit/test_runner_live.py` needs `claude` but lives in `tests/unit`; two test names
  stay as they are because `spikes/RESULTS.md` cites them (lines 928 and 1161).
- Spikes: `spike_other_uid.py` and `spike_fs_identity.py` write to `/etc` and
  `/usr/local/bin` without checking that they run in a container.
- `LICENSE.md`: the quoted public-domain dedication has a last sentence that `LICENSE` does
  not have.
- `docs/TESTING.md` keeps long dated run notes in its list of manual checks.
