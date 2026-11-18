"""`calu ide`: the bridge between an editor extension and calu (JSON-RPC on stdin and stdout).

    editor extension ◄── stdio, one JSON object per line ──► calu ide ──► calu session(s)
                                                                  └────► database

`calu.ide.bridge` answers the requests, `calu.ide.link` holds the connection to the attached
session and reconnects, `calu.ide.access` chooses the database login without ever asking on a
terminal, `calu.ide.database` reads and writes what the editor shows (settings, models,
checker instructions, sessions), `calu.ide.presets` holds the model presets, and
`calu.ide.serve` is the entry point the CLI calls. The contract with the extension is
`docs/notes/vscode-extension-spec.md`.
"""

from calu.ide.bridge import Bridge
from calu.ide.serve import serve_ide

__all__ = ["Bridge", "serve_ide"]
