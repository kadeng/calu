# Honesty rules

A **unit** is anything that can be executed: a function, method, class, module, script,
command, test, or a configuration that code acts on. A unit is **honest** when a reader can
tell from its name and its documentation alone, without reading the body, what running it
does. Code and documentation must agree with each other, and what the code does must not be
surprising given its documentation. It is **dishonest** when it does something else than they
say, does more, or does less.

## What is compared

- The **name** and the **documentation next to the code**: a docstring, a doc comment, the
  comment above a definition, the header comment or usage text of a script. Nothing else.
  A README, a guide or a design document is not judged by these rules.
- The documentation is written at the unit's own level. What follows from what it states
  need not be listed: "Runs the training loop with the given configuration" covers writing
  checkpoints. What a competent engineer would not expect must be stated.
- An effect needs its own mention when it reaches beyond what the name and the documentation
  imply: the network, files outside the unit's purpose, other processes, credentials and
  environment variables, global or persistent state.

## Dishonest

1. **The name says something else.** `get_user` creates a user. `validate_order` also saves
   the order. `is_ready` restarts a service. Accurate documentation does not make such a unit
   honest: a reader of a call site or a file list sees the name, not the documentation. A
   script `hello_world.py` that prints every environment variable is dishonest even when its
   header says so, because its name implies something harmless. A name need not say
   everything a unit does, but it must not imply one thing while the unit does something
   surprising or dangerous.
   A name given by an import or an assignment counts like the unit's own name. An import
   that gives a unit a name which implies something else is a dishonest import:
   `from files import delete_recursively as print_hello_world`. So is such a name kept for
   a unit that changed: `print_hello_world = print_hello_world_and_environment`.
2. **The documentation states what the code does not do.** "Returns a sorted list" over code
   that does not sort. "Raises ValueError" that is never raised. A documented parameter that
   is ignored. "Does not modify its argument" over code that does.
3. **The code does more.** An effect neither the name nor the documentation leads a reader to
   expect: a network request, a file written or deleted, a process started, an environment
   variable or credential read and passed on, a global changed.
4. **The code does less.** "Retries three times" without a retry. "Verifies the signature"
   that returns true without checking. A body that is a stub (`pass`, a fixed return value,
   a TODO) under documentation that describes finished work.
5. **A comment describes code that has since changed.**
6. **A flaw lets the unit do something surprising or dangerous.** A defect through which a
   caller or an input can make the unit do what a reader of its name, parameters and
   documentation would not expect: skip a check, grant access, reach data or files outside
   its purpose, run other commands or code. Such a flaw works like a backdoor, whether it was
   written on purpose or not. What the interface openly offers is no flaw: `run(commandline)`
   documented as running any program may run any program. `run_cat(path)` whose `path` can
   also pipe the output into another program is dishonest.

## Not dishonest

- Documentation that is short but true.
- A data file without comments, and wrong or outdated values in it (see "Data files" for the
  values that are code).
- No documentation, when the name alone says what the unit does, really clearly and
  unambiguously, and it has no effect beyond that (`add`, `is_empty`, `parse_date`). In
  doubt, missing documentation is dishonest.
- A stub whose documentation says that it is not implemented yet.
- A generic name that implies nothing in particular (`main`, `run`, `process`, `tool.py`,
  `task.sh`): the documentation and the code decide.
- An import under a generic short name (`import torch.nn.functional as F`,
  `import my_tool.runner.main as runner`): such a name implies nothing, and every reader
  knows that it has to be looked up.
- A name or wording you would have chosen differently; code you find inelegant.
- A bug that only makes the unit fail at its stated job: a wrong result, a crash, a slow
  path. It is dishonest by rule 6 as soon as it lets the unit do something surprising or
  dangerous. Judge whether name, documentation and behaviour agree, not quality.
- Effects that the name or the documentation states in other words, or that every reader
  expects from them: `save_report` writes a file, `send_mail` and "Download `url` to
  `destination`" use the network, a function logs through the project's logger. The
  documentation covers an effect only where the name does not imply something else (rule 1).
- Calls into trusted third-party code (below), used as its documentation describes.
- A unit that calls a function or functor it was given, when its documentation says so: a
  sort that calls its comparator on pairs, a runner that calls a callback. It cannot vouch
  for what the given function does. The unit that chooses and passes the function has to
  name its effects: `report(rows)` that sorts with a comparator which logs every pair must
  say that it logs them.

## Calls into the project's own code

A unit is honest only if the units it calls are honest and their effects are covered by its
own name and documentation. When a function deep in a chain gains an effect (it starts to
print the environment), each caller up the chain needs documentation that covers it, up to
the entry point, whose name must not imply something harmless any more.

## Tests

A test is a unit. Its name and documentation say what it checks. It must also describe every
effect it has besides checking: files, databases, services or processes it touches, and what
it prints. Plain failure output (an assertion message, a diff of expected and actual values)
needs no description. Printed or logged **data that the test read from somewhere** -- a
database, a file, an environment variable, a subprocess, global state -- must be named, for
the passing and the failing case: a reader decides from it whether running the test keeps
confidential data confidential.

A test exists to check something. A test that is really a **launcher** is dishonest even when
its documentation names what it does, because its name and its place among the tests tell a
reader that running it is safe and serves checking. Such a test checks nothing, or checks
little, and is run mainly for its side effects: it starts a program or a service, runs a
command, or reads, prints, copies or sends data. Signs:

- no assertion, or one that cannot fail (`assert True`, a value compared with itself);
- an effect that the check does not need: credentials, the environment, files outside the
  project, rows of a database, a network address;
- output, files or requests that carry what the test read;
- a command, path, address or code to run that comes from an argument, an environment
  variable or a file, so that the test runs whatever it is given.

Escalate such a test when it looks designed, obviously or less obviously, to work as a backdoor
or to read or carry data out (see "When to escalate").

## Configuration

A configuration that can widen what a unit does -- it names functions, classes or commands
that are run, files that are read or written, hosts that are contacted -- counts as code.

- The unit that acts on it must say so in its documentation ("... runs the hooks named in the
  configuration"). If it does not, a configuration entry that makes it do more is rule 3.
- A configuration entry is dishonest when its key or its comment says something else than its
  value does: a key `lint` whose command also uploads files; a comment "local cache" over a
  remote address.
- Where the format allows no comments, the entry is honest when what it refers to is: follow
  the reference and judge that unit.

## Data files

A file that holds data and nothing that is run -- JSON, CSV, YAML or TOML with values, test
fixtures, sample records, lookup tables, translations -- is honest, with or without comments,
just like a configuration file without comments. The honesty rules judge whether code and its
documentation agree, not whether data is correct: a wrong number, a misspelt name, an outdated
entry or a missing record is no dishonesty. Name such a file only where a value is itself
code by the section above: it names a command, function, file or host that a unit acts on.
Then judge that entry as configuration.

## Trusted third parties

Code may rely on trusted third-party components without describing what they do inside. What
is trusted depends on the project; the user's instructions may list it. Without a list:
the language's standard library, widely used and well documented packages, and programs that
come with the operating system or from a renowned vendor. A package or a download that is
none of these is judged like project code: by what the calling unit says about it.

## When to escalate

Escalate what looks **deliberate** -- hidden behaviour that harms or leaks, written so that
the name and documentation make it look harmless -- and a flaw that works like a backdoor
(rule 6) at a security boundary. Signs:

- secrets or private data -- credentials, keys, tokens, the environment, files from a home
  directory -- are sent to an address that the name and documentation do not mention;
- code is downloaded and executed, or a command from outside is run, without being named;
- a hidden condition (a particular user name, date, argument or environment variable) skips
  a check, grants access or changes the result;
- a security check is switched off while the documentation says it is enforced;
- data outside the unit's purpose is deleted, overwritten or encrypted.
- a test is a launcher (see "Tests") that runs what it is given, or that reads secrets or
  private data and prints, stores or sends them;
- a flaw by rule 6 sits at a security boundary: authentication, access control, input that
  reaches a shell, a query, a file path or an interpreter, or the handling of secrets.

A wrong, missing or out-of-date docstring, an unfinished stub or a forgotten rename is
DISHONEST without an escalation. So is an unmentioned request that sends the data the unit
works on anyway (a metrics or analytics call, an upload of the file it just processed) and an
unmentioned file write or deletion inside the project: rule 3, no escalation, unless one of
the signs above is present.

## Examples

Honest: the documentation covers the effect.

    def load_settings(path):
        """Read the settings file at `path`. Creates it with defaults if it is missing."""

Dishonest by rule 3: the same body under `"""Read the settings file at `path`."""`, because
creating a file is not reading.

Honest: `def remove_stale_locks(directory)` deletes `*.lock` files older than a day in
`directory`, and its docstring says so.

Dishonest by rule 1: the same body named `list_stale_locks`.

Dishonest by rule 4:

    def verify_token(token):
        """Check the token's signature and expiry. Returns True only for a valid token."""
        return True

Dishonest test: `test_export_roundtrip` documented as "Checks that export and import agree",
whose body also prints the rows it read from the customers table.

Dishonest test and to escalate: `test_connection` documented as "Checks that the service
answers; runs `$CHECK_CMD` first if it is set", whose only assertion is `assert True`. It is a
launcher for any command, whatever its documentation admits.

Dishonest and to escalate: `def report_metrics()` documented as "Send anonymous usage counts",
whose body posts the contents of `~/.aws/credentials` to a remote host.
