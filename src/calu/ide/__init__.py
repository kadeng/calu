"""`calu ide`: the bridge between an editor extension and calu (JSON-RPC on stdin and stdout).

    editor extension ◄── stdio, one JSON object per line ──► calu ide ──► calu session(s)
                                                                  └────► database

`calu.ide.bridge` answers the requests, `calu.ide.link` holds the connection to the attached
session and reconnects, `calu.ide.access` chooses the database login without ever asking on a
terminal, `calu.ide.database` reads and writes what the editor shows (settings, models,
checker instructions, sessions), `calu.ide.presets` holds the model presets, and
`calu.ide.serve` is the entry point the CLI calls. The contract with the extension is
`docs/notes/vscode-extension-spec.md`.

For an editor that works on a copy of the project and reaches the server over SSH only, the
bridge also synchronises files (`docs/notes/jetbrains-sync-spec.md`): `calu.ide.files` holds
the rules that need no file access (paths, limits, the change log, the held files),
`calu.ide.filestore` every access to the project's files, and `calu.ide.filesync` the
`ide.fs.*` requests, the watcher and the `ide.fs.changed` notifications.
"""

from calu.ide.bridge import Bridge
from calu.ide.serve import serve_ide

__all__ = ["Bridge", "serve_ide"]
