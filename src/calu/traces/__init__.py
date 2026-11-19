"""Session traces: the conversation of a session as a redacted Markdown file in the project.

With `traces.enabled` on, a session appends every step to
`<traces.directory>/<YYYY-MM-DD>/<user>/session_NNN.md` (`traces.layout` and `traces.file_names`
choose other paths, D245), so that the conversation can be committed and reviewed with the code it
produced (agent/records/PLAN.md D232, REQUIREMENTS 36). The file only grows; nothing is folded or
summarised; it holds no images. Before a commit, the part the commit adds is redacted once more
as a whole (`finalize`, D246); calu makes no commit of its own.

    calu.session.Session
        │ every event it emits, in order          the narrations it computes for its checkers
        ▼                                          ▼
    writer.TraceWriter.feed ───────────────── writer.TraceWriter.narration
        │ render.TraceRenderer: event -> Markdown block(s)
        ▼
    a bounded queue ──► redact: stage 1 (known secret values, shapes of keys and tokens)
                                stage 2 (the redaction agent, if `traces.redact` is on, with
                                `traces.redactor_instructions`)
                    ──► appended to the file, in the order of the events

| Module | Content |
|---|---|
| `render` | The blocks, the header, the file's path and its exclusive creation |
| `redact` | The two stages of the redaction; `WITHHELD` |
| `writer` | The queue, the tasks and the file; what is written when something fails |
| `finalize` | The final redaction pass before a commit, over what the commit adds (D246) |
| `hook` | The opt-in git pre-commit hook that runs the final pass |
"""

from calu.traces.redact import WITHHELD, RedactionFailed, Redactor, Secrets
from calu.traces.render import Header, TraceRenderer, session_directory
from calu.traces.writer import TraceWriter

__all__ = [
    "WITHHELD",
    "Header",
    "RedactionFailed",
    "Redactor",
    "Secrets",
    "TraceRenderer",
    "TraceWriter",
    "session_directory",
]
