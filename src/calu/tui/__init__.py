"""The full-screen terminal client of a running session (`calu attach` on a terminal).

A client of the protocol like the web live view and the ACP adapter. It needs Textual, an
ordinary dependency of calu. `calu.tui.start` imports
nothing from Textual, so `calu.cli` can ask `start.available()` without it; `start.run_tui`
imports the app when it is called.

    calu attach            the full-screen client, when input and output are a terminal
    calu attach --plain    the line client (`calu.attach`), which pipes and scripts also get
    python -m calu.tui     the full-screen client on a scripted session, without calu running

Modules: `model` (the state, a reducer over protocol events, no Textual), `connection` (the
connection and its reconnects), `actions` (the requests), `render` (model to text lines),
`app`, `widgets/`, `screens/` and `tui.tcss` (Textual), `fake` and `scenarios` (a scripted
session for tests, screenshots and the demo).
"""
