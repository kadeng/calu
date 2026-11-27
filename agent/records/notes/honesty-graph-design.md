# The honesty graph: a view of the honesty state in the chat pane

Design note of 2026-11-27. Nothing is built. The owner asked for the view and agreed to the
plan and the five points of section 6 ("Agreed with your plan regarding the honesty
visualization. Implement it like that."). The owner also set the order: getting the honesty
checker and the review to work, tested with a real model, comes first.

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

## 2. The display

A **layered graph**: callers above, the units they use below, an edge from caller to used
unit.

- Not a tree: a caller can use two dishonest units, and a unit has many callers. A tree
  would repeat nodes.
- Not a force-directed layout: it moves every node when one appears or vanishes, and it hides
  direction. Dishonesty travels from a unit up to its callers, so direction is what the view
  has to show.
- Nodes keep their place between updates. A change is animated: a node changes colour, a
  settled node turns green, fades, and the rest closes up.

The chat pane in VSCode is a side bar about 350 pixels wide. So:

- A node is a compact pill with the short name (`fields`, `Parser.parse`).
- The callers of one unit collapse into one node "12 callers". A click expands it.
- The view pans and zooms.

The pane has no runtime dependency today. The layout would use `@dagrejs/dagre` (MIT),
bundled by esbuild. The drawing is SVG written by hand.

The view replaces the chat area while it is open. A switch in the pane's header (next to the
"Honesty" chip) goes back to the chat.

## 3. Colours and what they mean

| Colour | Node | Where it comes from |
|---|---|---|
| red | a unit with an open mark | `HonestyState.marks` |
| yellow | a unit that changed and nobody verified | the changed definitions of an unreviewed file, against the baseline (Python); the whole file for other languages |
| orange, solid border | a unit to judge again, because a unit it uses changed its contract | `HonestyState.recheck`; this blocks under `enforce` |
| orange, thin border | an unchanged caller of a unit with an open mark | found for the view only; it blocks nothing |
| green | a unit that was just settled | shown briefly, then removed |
| a ring or pulse | a unit in a file that a running review reads | the plan of the review in flight |

## 4. What the client gets

The client subscribes when the view becomes visible and unsubscribes when it is hidden.
While no client is subscribed, the server computes nothing for the view.

On each change the server sends the whole graph with a revision number. The graph is small,
and the client compares it with the one before to animate. Updates are sent at most twice a
second.

| Part | Content |
|---|---|
| node | id (`path:dotted.name`), path, line, short name, kind (function, method, class, script, file), state, whether a review reads it now |
| details for the hover | the mark's reason and who opened it; what changed (signature, doc comment, body); for a caller, the unit it uses, and that unit's contract before and now |
| edge | caller id, used unit id, and whether the link is confirmed or only possible |
| settled | the ids that became clean since the last update, with the reason (review, checked edit, person) |
| summary | baseline, counts, and "every file is unreviewed" when there is no baseline |

Above about 150 nodes the server sends one node per file.

## 5. What the server has to compute

- **Red**: the marks have a path and a symbol. The line comes from the file.
- **Yellow**: per unreviewed Python file, the definitions that differ from the baseline
  (`calu.checkers.symbols.changed_definitions`, extended by definitions that are new).
  Cached by the file's content hash.
- **Orange, solid**: `HonestyState.recheck`, with its edge to the unit that changed.
- **Orange, thin**: one reference search per unit with an open mark, with the word search and
  the language server if one is configured, and without the judge. A link that nothing
  confirmed is drawn dashed. Cached until a file changes. This is display only and must cost
  no model call.
- **Review**: the files of the review in flight.

All of it runs in a worker thread and only while a client is subscribed.

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

## 7. Steps

1. Server: build the graph from the state on request; the subscription; the two new
   requests (dismiss an entry, mark by hand). Tests with the scripted runner.
2. Client: the switch in the header, layout, drawing, hover, click, menu. In the webview
   that VSCode and the web pane share (`extensions/vscode/webview/`).
3. A scripted scenario and a run in the Cua desktop.
