"""MetaTron for the browser -- after MetaTron by Kai Londenberg (`calu lightcycles`).

An easter egg: a port of the light-cycle game MetaTron (C++ and DirectDraw; source archive
`MetaTron1-2-src.zip` of the SourceForge project `metatron`) to a web page. The port takes the
original's mechanics, its computer players, its scoring and its menus from the author's
source. It does not take the music, the sounds or the title bitmap, and none of the DirectDraw
helper files.

What MetaTron is:

- Up to 10 players, each a person or a computer player ("KI"), ride on one field of 800x600
  pixels. A cycle moves `speed` pixels (1 to 9, preset 2) per frame and leaves a wall behind
  it. Each player has two keys: one turns the cycle a quarter turn to the left, one to the
  right.
- The players start in two columns that face each other. A frame encloses the field, 30
  pixels inside the screen's edge.
- A cycle that runs into anything crashes. The crash explodes: it empties a square of 33x33
  pixels around it -- walls and frame alike -- and 15 of its 30 sparks stay as obstacles.
- Through a hole in the frame a cycle can leave the field. A cycle that reaches the screen's
  edge has escaped, which scores more than surviving.
- A round ends 230 frames after only one cycle is left, or at once when no person rides any
  more. Score of a round: the number of cycles that crashed before one's own crash, plus
  `players + 5` for the cycle that still rides, plus `players + 20` for an escape. There is no
  end of the match: the statistics add up until the players reset them.
- Menus: main menu over a water effect (start, setup, scores, leave), setup (speed, wait
  states, save games, the ten players), player setup (person, KI or inactive; name; the two
  keys; colour; resets).

The game runs in the page; this package only serves the files in `static/`:

    calu lightcycles --> server.serve: 127.0.0.1, prints the address, opens the browser
    browser --> GET /            static/index.html
            --> GET /static/...  main.js (keys, 60 frames per second), flow.js (menus),
                                 sim.js (one frame of a round), water.js, render.js, style.css

    one frame of a round (static/sim.js, `step`):
        key presses --> turn the cycles
        each player in order: sparks of its explosion | KI decides | draw `speed` pixels,
            crash on the first pixel that is not empty --> points, explosion
        one cycle left --> countdown --> points --> statistics

It needs nothing of calu: no database, no credential, no configuration. The page loads nothing
from another server and sends nothing anywhere. Only `calu.cli` imports this package, and only
inside the handler of the command.
"""
