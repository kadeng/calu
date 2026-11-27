# The honesty graph: a view of the honesty state in the chat pane

Design note of 2026-11-27, updated the same evening to what was built (PLAN D297). The owner
asked for the view and agreed to the plan and the five points of section 6 ("Agreed with your
plan regarding the honesty visualization. Implement it like that."). The owner also set the
order: getting the honesty checker and the review to work, tested with a real model, comes
first.

State: built for VSCode and the web chat pane, which share one webview. Unit tests on both
sides; seen in VSCode in the Cua desktop (test plan, section V). Section 8 lists what was not
seen and what is open.

## 1. What the owner asked for

"add a new honesty visualization pane to the chat pane, which we can open. While the chat
runs and it's visible (don't render or update it while hidden), it shows a dynamic graph or
call tree that shows the current honesty state. Nodes in the graph are units in the sense of
the code-honesty, e.g. functions, methods, scripts that can be marked honest / dishonest at
the most granular level. We only show the subgraph or subtree of interest, parts that become
clean vanish from view. A dishonest node is shown in red, a modified (unreviewed) node in
yellow, an unmodified (unreviewed) caller of a dishonest node is shown in orange. A honest
node in green, ongoing reviews mark nodes. Nodes have (short, not fully qualified) names of
the functions or Class.method and can show details on hover. A click should bring us to the
source line. A right click should open a dropdown for a node that allows to set the honesty
state manually to honest or dishonest." "Layout and visuals should be done client-side." "It
should work in VSCode and the web UI's chat pane ( the one that is also used in VSCode ). It
should take the same visual space that the chat area takes when we look at it. When it's not
visible, we should not pay much of an overhead for it."

Later the same day: "The graph display might benefit from the ability to zoom with
ctrl-scrollwheel, by the way."

## 2. The display

A **layered graph**: callers above, the units they use below, an edge from caller to used
unit.

- Not a tree: a caller can use two dishonest units, and a unit has many callers. A tree
  would repeat nodes.
- Not a force-directed layout: it moves every node when one appears or vanishes, and it hides
  direction. Dishonesty travels from a unit up to its callers, so direction is what the view
  has to show.

As built:

- The button "Graph" in the pane's header, next to the "Honesty" chip, switches between the
  chat and the graph. The graph takes the chat area's place. The button is disabled while no
  session is attached or `honesty.mode` is off; while the graph is open it always leads back.
- The layout is calu's own code (`extensions/vscode/src/shared/honestyGraphLayout.ts`), not
  `@dagrejs/dagre`: the pane keeps no runtime dependency. Its steps:
  1. A node's layer is the longest path down to a node that uses nothing; an edge that closes
     a cycle is ignored for this.
  2. The lowest layer is sorted by id, every other layer by the average position of the
     nodes it uses. The same graph gives the same picture.
  3. A layer wider than the view wraps into rows.
  4. Each edge is one curve from the source's lower edge to the target's upper edge.
- Nodes do not keep a remembered place between updates. The layout is deterministic, so an
  unchanged graph looks the same, but a node that appears or vanishes can move others. There
  is no animated movement.
- A unit is a pill with its short name (`fields`, `Parser.parse`); a whole file is a square
  with the file's name. A long name is cut in the middle. Each state also has a glyph, so
  the state can be told without colour.
- More than 5 callers of one unit that use nothing else and that nothing uses collapse into
  one node "N callers". Its menu expands the group and collapses it again.
- Dragging pans the view. Ctrl and the scroll wheel zoom (0.25 to 3 times), and so do the
  buttons "−" and "+" in the bar. "Fit" fits the drawing to the view.
- A bar above the drawing shows notes: a review is running, the graph is one node per file,
  there is no baseline. A legend below explains the states. A tooltip shows the node's id,
  its state in words and its detail, on hover and on keyboard focus.

## 3. Colours and what they mean

| Colour | State | Glyph | Node | Where it comes from |
|---|---|---|---|---|
| red | `dishonest` | ✕ | a unit with an open mark | `HonestyState.marks` |
| orange, thick border | `recheck` | ? | a unit to judge again, because a unit it uses changed its contract | `HonestyState.recheck`; this blocks under `enforce` |
| yellow | `changed` | Δ | a unit that changed and nobody verified | the definitions of an unreviewed Python file that differ from the file's earlier text; the whole file for other languages |
| orange, thin border, pale fill | `caller` | ↑ | an unchanged caller of a unit with an open mark | found for the view only; it blocks nothing |
| green | `honest` | ✓ | the unit whose changed contract made another unit `recheck` | shown so that the edge has an end |
| grey, pale fill | `related` | · | an unchanged unit that uses a changed unit, or that a changed or dishonest unit uses | found for the view only; it blocks nothing (section 9) |
| green, faded | settled | ✓ | a unit that just left the graph | the client, for 2.5 seconds |
| a pulse | | | a unit in a file that a running review reads | the files of the review in flight |

If a unit has several states, the first of this order wins: dishonest, recheck, changed,
caller, honest, related.

An edge that the reference index only suspected (found by the name alone, nothing confirmed
it) is dashed.

## 4. What the client gets

There is no subscription. The client asks for the graph with the request `honesty.graph`
(read-only, also allowed for a read-only client). The server computes it only when asked.
So a hidden view costs nothing, and a client that disconnects leaves nothing behind. Between
requests the session keeps the word search's results per file (section 9) and the language
server, if one is configured.

The client asks:

- when the view opens;
- when a new honesty state arrives (`honesty.update`) while the view is open;
- after every finished tool call while the view is open (commit `7b38f5e`);
- when the attached session changes.

`GraphRefresh` in `extensions/vscode/src/shared/honestyGraphModel.ts` keeps one request at a
time: at least 500 ms between two requests, 150 ms of debounce, and a request without an
answer after 30 seconds counts as lost.

The answer (`HonestyGraphResult`) is the whole graph. The client compares it with the one
before: a node that is gone is shown once more as settled, in green, then removed.

| Part | Content |
|---|---|
| node | `id` (`path:dotted.name`, or the path for a whole file), `path`, `line`, short `name`, `kind` (`unit` or `file`), `state`, `detail` (the hover text, at most 400 characters), `reviewing`, `mark_ids`, `recheck_ids`, `count` (units a node stands for in a graph by file) |
| edge | `source` (the caller), `target` (the unit it uses), `certain` |
| graph | `by_file` (reduced to one node per file), `everything` (no baseline), `mode`, `review_running` |

The detail holds the mark's reason and who found it; for a changed unit, what changed; for a
unit to judge again, which unit it uses. The answer carries no list of what was settled and
why, and no counts: the client finds the settled nodes itself, and the counts are in the
"Honesty" chip already.

Above 150 nodes the server sends one node per file.

## 5. What the server computes

`src/calu/honesty_graph.py` builds the graph; `Session.honesty_graph` answers the request.

- **Red**: the marks have a path and a symbol. The line comes from the file.
- **Orange, thick**: `HonestyState.recheck`, with an edge to the unit whose contract changed.
- **Yellow**: per unreviewed Python file, the definitions that differ from the file's earlier
  text (`changed_units`). The earlier text is the content last verified in this session, or
  else the baseline's. A new file counts as changed as a whole. Not cached; the files are
  read at every request. A file over 2 MB is one node and is not read.
- **Without a baseline** every file counts as unreviewed. The graph then holds only the marks
  and the recheck entries (`everything`), and the bar says so.
- **The clean units around** (`add_neighbours`; section 9 has the owner's words). For every
  dishonest and every changed unit, never for a whole file:
  - what uses it: one reference search with the word search, and the language server if
    `honesty.dependents_navigator` names one, never the judge, so the view costs no model
    call. Each place is mapped to the unit around it. A caller of a dishonest unit is
    orange with a thin border (`caller`); a caller of a changed unit is grey (`related`). A
    place nothing confirmed gives an uncertain edge.
  - what it uses: read from its own syntax (`calu.callees`), grey (`related`), with a
    certain edge.
  - A unit that is in the graph already keeps its state and only gets the edge.
  - Limits: 12 clean callers per unit (`hidden_callers` counts the rest), 20 seconds for all
    searches together (`NEIGHBOURS_BUDGET_S`), 150 nodes. The last two set `partial`. On an
    error the graph comes without these units and the session logs the error.
- **Review**: the files of the review in flight set `reviewing`.

The graph is built in a worker thread, only when a client asks.

## 6. Actions, as agreed

1. **Click**: open the source line. The pane already opens a file at a line.
2. **Right click, "honest"**:
   - on red: clear the mark (`honesty.clear`, exists);
   - on orange: dismiss the entry (new);
   - on yellow: trust the content of the **whole file** (`honesty.trust` with a path,
     exists). Trust is kept per file content, and the confirmation says so.
3. **Right click, "dishonest"**: open a mark in the person's name, with a one-line reason
   (new).
4. Other languages than Python: one node per file for now.
5. Above about 150 nodes: one node per file.

As built (`nodeMenu` in `honestyGraphModel.ts`):

| Node | "Mark as honest" sends | Also in the menu |
|---|---|---|
| dishonest | `honesty.clear`, once per open mark | Open the source |
| recheck | `honesty.dismiss` with the node's recheck ids | Mark as dishonest…, Open the source |
| changed | "Mark as honest: trust the whole file…", `honesty.trust` with the path; the host asks first | Mark as dishonest…, Open the source |
| caller, honest | nothing | Mark as dishonest…, Open the source |
| settled | nothing | Open the source |
| a group "N callers" | nothing | expand or collapse |

- "Mark as dishonest…" asks for the reason in the host (an input box in VSCode, a dialog in
  the web pane) and sends `honesty.mark`. The session opens the mark as
  "<client> (by hand)", withdraws the stored trust of the file's content as for a checker's
  mark, and logs it (`Session.mark_by_hand`).
- `honesty.dismiss` drops the entries from every checkout's state and logs who dropped how
  many (`Session.dismiss_recheck`).
- The menu opens with a right click, the context-menu key or Shift+F10.
- All text from calu is set as text, never as HTML.

## 7. Steps

1. Server: the graph on request, `honesty.mark`, `honesty.dismiss`; tests with the scripted
   runner. **Done** (`508b508`; `tests/unit/test_honesty_graph.py`, additions in
   `tests/unit/test_honesty_session.py`).
2. Client: the switch in the header, layout, drawing, hover, click, menu, zoom, in the
   webview that VSCode and the web pane share (`extensions/vscode/webview/honestyGraph.ts`).
   **Done** (`508b508`, the refresh after each tool call in `7b38f5e`; unit tests of the
   layout and the model).
3. A scripted scenario and a run in the Cua desktop. **Partly done** (test plan, section V):
   the view, yellow and red nodes, the hover, the menu, "Mark as honest" with the settled
   node. See section 8.

## 8. Not done, and open

Not seen in a UI yet:

- edges, callers of a dishonest unit, units to judge again. The scripted honesty checker of
  `uitest/fake_server.py` marks whole files, so there is no unit to find callers of. It needs
  a mark on a unit;
- click to the source, "Mark as dishonest…" with its prompt, zoom with Ctrl and the wheel,
  panning, the "N callers" group;
- the web chat pane.

Not built:

- The PyCharm plugin has no graph view.
- The changed units are not cached. For the callers see section 9.
- No animation when a node appears or moves; only the settled node fades and a reviewed node
  pulses.

For the owner to decide:

- Whether compiled files (`.pyc` in a project without `.gitignore`) are left out of the
  graph. In the Cua demo project they appear as unreviewed files.

## 9. Callers and used units of unreviewed nodes (2026-11-27, late evening)

The owner tested the view and saw only unconnected yellow nodes: "Show callers and callees
of unreviewed nodes ( as far as known ) as well." And: "If a node has too many callers,
let's limit how many are shown, I would say, by default we don't show more than 12 callers
of a node unless it's dishonest or unreviewed itself".

Before, only two kinds of edges existed: from a unit to judge again to the unit whose
contract changed, and from a caller to a dishonest unit. A changed unit had none.

Built (`calu.honesty_graph.add_neighbours`, `calu.callees`):

- A changed unit gets its callers and the units it uses, as grey `related` nodes. A
  dishonest unit gets the units it uses as well.
- Two changed units of which one uses the other are connected.
- "As far as known": the used units come from the unit's own Python syntax. Followed are
  names of the same file, methods on `self` and `cls`, and names imported from files of
  the project. A call on another object (`reader.read()`) is not followed. With a language
  server configured, the callers are confirmed; the used units are not asked from it yet.
- At most 12 clean callers per unit. A caller that is itself dishonest, changed or to
  judge again is always connected. The tooltip of the unit says how many are left out.
  "By default" in the owner's words: there is no way to see the others yet.

Speed, measured on calu itself (17 changed files, 42 changed units, 145 nodes, 173 edges):

| Request | Time |
|---|---|
| building the graph without neighbours | 1.2 s |
| the neighbours, first request | 12.4 s |
| the neighbours, a repeated request with no file changed | 3.7 s |

- Before the work on speed the first request took 14 s for 25 units, and hit the 20 second
  limit for 76.
- The word search keeps its result per file and name while the file's modification time
  and size stay the same (`SearchMemo` on the session). A changed file is searched again.
- Within one request each file is read, tokenized and parsed once.
- What a file parses to (the unit around each line, its definitions and imports) is kept per
  file state as well (`NeighbourMemo` on the session). Measured after that, 14 changed
  units: 11.7 s the first request, 1.7 s a repeated one.
- What is left is the first request: the word search reads every file once per unit. One
  pass over the files for all names of a request would remove that; not built.

Not done: nothing of this was seen in a UI.
