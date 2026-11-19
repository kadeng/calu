"""MetaTron for the browser (`calu lightcycles`) in a real browser.

The mechanics are JavaScript (`src/calu/lightcycles/static/sim.js`), so they are tested where
they run: the page is loaded from the real server application, under its real
Content-Security-Policy, and each test imports the simulation module in the page and steps it
with scripted inputs and a seed. Nothing waits for real time: a round of several thousand
frames is stepped in one call.

They are marked `browser` only: the game needs no database.
"""

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from calu.lightcycles.server import create_app
from tests.browser.conftest import serve

pytestmark = [pytest.mark.browser, pytest.mark.anyio]

SHOTS = Path(
    os.environ.get("CALU_LIGHTCYCLES_SHOTS", Path.home() / ".cache/calu-uitest/share/lightcycles")
)
"""Where the play-through leaves its screenshots, for a person to look at."""

PRELUDE = """
const sim = await import('/static/sim.js');
const flow = await import('/static/flow.js');
// A game whose first `people` players are people and whose next `bots` are computer players.
const game = (seed, people, bots) => {
  const g = sim.createGame(seed);
  g.players.forEach((p, i) => { p.active = i < people + bots; p.human = i < people; });
  sim.startRound(g);
  return g;
};
const at = (g, x, y) => g.buf[x + sim.WIDTH * y];
const run = (g, limit = 100000) => { let n = 0; while (!sim.step(g) && n < limit) n++; return n; };
"""


@pytest.fixture
async def game_page(page: Any) -> AsyncIterator[Any]:
    """The game's page, loaded, with every console error and page error collected.

    A test that leaves an error on the console, or a refused request or script (a violation
    of the Content-Security-Policy is reported as a console error), fails at its end.
    """
    problems: list[str] = []
    page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda exc: problems.append(str(exc)))
    page.on("requestfailed", lambda request: problems.append(f"failed: {request.url}"))
    async with serve(create_app()) as base:
        page.base = base
        await page.goto(f"{base}/?seed=1")
        await page.wait_for_function("window.__lightcyclesTest !== undefined")
        yield page
    assert problems == []


async def js(page: Any, body: str) -> Any:
    """Run JavaScript in the page with the simulation imported, and return its result.

    Args:
        page: The loaded page.
        body: Statements that end in `return ...`; `sim`, `flow`, `game`, `at` and `run`
            are defined (`PRELUDE`).

    Returns:
        The returned value, as JSON gives it.
    """
    return await page.evaluate(f"async () => {{ {PRELUDE} {body} }}")


async def test_a_cycle_moves_its_speed_per_frame_and_turns_by_quarter_turns(game_page) -> None:
    found = await js(
        game_page,
        """
        const g = game(1, 2, 0);
        const p = g.players[0], q = g.players[1];
        const start = [p.pos.x, p.pos.y, p.direction, q.pos.x, q.pos.y, q.direction];
        sim.step(g);
        const one = [p.pos.x, q.pos.x, at(g, 150, 70), at(g, 151, 70), at(g, 152, 70)];
        sim.step(g, [[0, 'R']]);
        const down = [p.pos.x, p.pos.y, p.direction, at(g, 152, 70), at(g, 152, 71)];
        sim.step(g, [[0, 'L'], [0, 'L']]);
        g.speed = 5;
        sim.step(g);
        return {start, one, down, up: [p.pos.x, p.pos.y, p.direction], frame: at(g, 30, 30)};
        """,
    )
    # Players 1 and 2 face each other on one row; the frame is drawn.
    assert found["start"] == [150, 70, 1, 650, 70, 3]
    assert found["frame"] == 11
    # Speed 2: two pixels of wall per frame; the pixel at the position itself is not drawn yet.
    assert found["one"] == [152, 648, 1, 1, 0]
    # A right turn from heading right heads down; the wall goes on from the position.
    assert found["down"] == [152, 72, 2, 1, 1]
    # Two left turns in one frame: from down over right to up, into the wall just drawn.
    assert found["up"][2] == 0


async def test_two_cycles_that_meet_head_on_both_crash_one_after_the_other(game_page) -> None:
    found = await js(
        game_page,
        """
        const g = game(1, 2, 0);
        const died = [0, 0];
        let n = 0, hole = null;
        while (!sim.step(g)) {
          n++;
          g.players.slice(0, 2).forEach((p, i) => { if (!p.alive && !died[i]) died[i] = n; });
          if (died[0] && hole === null) hole = [at(g, 390, 70), at(g, 410, 70), at(g, 380, 70)];
        }
        const points = g.players.slice(0, 2).map(p => [p.points, p.tpoints, p.games]);
        return {n, died, hole, points,
                over: g.roundOver, alive: g.playersAlive};
        """,
    )
    # After 125 frames both are at x 400. Player 1 moves first and finds player 2's wall.
    assert found["died"][0] == 126
    # Its explosion empties 33x33 pixels, walls of both included; outside it the wall stays.
    assert found["hole"] == [0, 0, 1]
    # Player 2 rides through the hole and crashes into the wall behind it a few frames later.
    assert found["n"] < 140 and found["alive"] == 0 and found["over"]
    # Points for a crash: the number of cycles that crashed earlier.
    assert found["points"] == [[0, 0, 1], [1, 1, 1]]


async def test_a_cycle_crashes_into_a_wall_drawn_a_moment_ago(game_page) -> None:
    found = await js(
        game_page,
        """
        const g = game(1, 3, 0);
        const p = g.players[0];
        for (let i = 0; i < 10; i++) sim.step(g);
        sim.step(g, [[0, 'R']]);
        sim.step(g, [[0, 'R']]);
        // Three right turns lead back up to the row the cycle came along.
        sim.step(g, [[0, 'R']]);
        const before = p.alive;
        sim.step(g);
        return {before, alive: p.alive, escaped: p.escaped, exploding: g.explosions[0] !== null,
                others: [g.players[1].alive, g.players[2].alive], over: g.roundOver};
        """,
    )
    assert found == {
        "before": True,
        "alive": False,
        "escaped": False,
        "exploding": True,
        "others": [True, True],
        "over": False,
    }


async def test_a_round_ends_after_the_countdown_and_scores_as_the_original(game_page) -> None:
    found = await js(
        game_page,
        """
        const g = game(1, 3, 0);
        g.speed = 1;
        let lastCrash = 0, n = 0;
        while (!sim.step(g)) { n++; if (g.playersAlive > 1) lastCrash = n + 1; }
        return {frames: n + 1 - lastCrash,
                rows: g.players.slice(0, 3).map(
                  p => [p.alive, p.points, p.tpoints, p.tsurvived, p.games])};
        """,
    )
    # Players 1 and 2 crash head-on; player 3 rides on alone for the countdown of 230 frames.
    assert 230 <= found["frames"] <= 233
    # At speed 1 player 2 is the one that finds the other's wall first: 0 points; player 1
    # crashes second: 1 point; the survivor gets players + 5 = 8 and a victory.
    assert found["rows"] == [[False, 1, 1, 0, 1], [False, 0, 0, 0, 1], [True, 8, 8, 1, 1]]


async def test_a_round_with_people_ends_at_once_when_no_person_rides(game_page) -> None:
    found = await js(
        game_page,
        """
        const g = game(1, 1, 2);
        // The person turns back into its own wall; two computer players still ride.
        sim.step(g); sim.step(g, [[0, 'R']]); sim.step(g, [[0, 'R']]); sim.step(g, [[0, 'R']]);
        const over = sim.step(g);
        const rows = g.players.slice(0, 3).map(p => [p.points, p.tsurvived]);
        return {over, alive: g.playersAlive, rows};
        """,
    )
    assert found["over"] is True and found["alive"] == 2
    # Each of the two that still ride gets players + 5 and a victory.
    assert found["rows"] == [[0, 0], [8, 1], [8, 1]]


async def test_a_cycle_that_leaves_through_a_hole_in_the_frame_has_escaped(game_page) -> None:
    found = await js(
        game_page,
        """
        const g = game(1, 2, 0);
        const p = g.players[0];
        // A hole in the left side of the frame, as an explosion leaves one.
        for (let y = 290; y < 310; y++) g.buf[30 + sim.WIDTH * y] = 0;
        p.pos = {x: 60, y: 300}; p.direction = 3;
        let n = 0;
        while (p.alive) { sim.step(g); n++; }
        const exploding = g.explosions[0] !== null;
        return {n, x: p.pos.x, escaped: p.escaped, points: p.points, exploding,
                over: g.roundOver, alive: g.players[1].alive};
        """,
    )
    assert found["escaped"] is True and found["x"] == 1 and found["exploding"] is False
    # The other cycle still rides; the round goes on into its countdown.
    assert found["over"] is False and found["alive"] is True
    total = await js(
        game_page,
        """
        const g = game(1, 2, 0);
        const p = g.players[0];
        for (let y = 290; y < 310; y++) g.buf[30 + sim.WIDTH * y] = 0;
        p.pos = {x: 60, y: 300}; p.direction = 3;
        g.players[1].pos = {x: 400, y: 500}; g.players[1].direction = 3;
        run(g);
        return g.players.slice(0, 2).map(
          p => [p.escaped, p.alive, p.points, p.tescaped, p.tsurvived]);
        """,
    )
    # Escaped first: 0 for the crash order, players + 20 = 22 for the escape. The other
    # cycle crashes into the frame during the countdown: 1 point, no victory.
    assert total == [[True, False, 22, 1, 0], [False, False, 1, 0, 0]]


async def test_an_explosion_opens_the_frame_and_leaves_fifteen_sparks(game_page) -> None:
    found = await js(
        game_page,
        """
        const g = game(1, 3, 0);
        const p = g.players[2];
        p.pos = {x: 760, y: 300}; p.direction = 1;
        while (p.alive) sim.step(g);
        const centre = {...p.pos};
        const open = [0, -16, 16, -17, 17].map(dy => at(g, 770, 300 + dy));
        for (let i = 0; i < 30; i++) sim.step(g);
        let sparks = 0;
        for (let y = 280; y < 320; y++) {
          for (let x = 740; x < 795; x++) if (at(g, x, y) === 3) sparks++;
        }
        return {centre, open, sparks, done: g.explosions[2] === null};
        """,
    )
    assert found["centre"] == {"x": 772, "y": 300}
    # 33 pixels of the frame are gone; the next pixel on either side is still frame.
    assert found["open"] == [0, 0, 0, 11, 11]
    # Half of the 30 sparks stay as walls in the crashed player's colour (some may share a pixel).
    assert found["done"] is True and 8 <= found["sparks"] <= 15


async def test_wait_states_slow_the_cycles_down(game_page) -> None:
    found = await js(
        game_page,
        """
        const g = game(1, 2, 0);
        g.waitStates = 2;
        const xs = [];
        for (let i = 0; i < 7; i++) { sim.step(g); xs.push(g.players[0].pos.x); }
        return xs;
        """,
    )
    assert found == [152, 152, 152, 154, 154, 154, 156]


async def test_a_computer_player_turns_to_the_open_side_before_a_wall(game_page) -> None:
    """With a wall right ahead and one side closed, the KI takes the open side.

    The original's KI demands more of a turn the more often it has turned that way (against
    riding in circles), so this holds for a computer player without such a history; that is
    how every round's first turns are decided.
    """
    found = await js(
        game_page,
        """
        const wrong = [];
        for (let seed = 1; seed <= 300; seed++) {
          for (const open of ['L', 'R']) {
            const g = game(seed, 0, 1);
            const p = g.players[0];
            p.pos = {x: 400, y: 300}; p.direction = 1;
            // A wall 3 pixels ahead, and a wall along the closed side.
            for (let y = 200; y < 400; y++) g.buf[403 + sim.WIDTH * y] = 9;
            const side = open === 'L' ? 301 : 299;
            for (let x = 300; x < 404; x++) g.buf[x + sim.WIDTH * side] = 9;
            sim.react(g, p);
            const want = open === 'L' ? 0 : 2;
            if (p.direction !== want) wrong.push([seed, open, p.direction]);
          }
        }
        return wrong;
        """,
    )
    assert found == []


async def test_computer_players_ride_for_a_while_and_one_wins(game_page) -> None:
    found = await js(
        game_page,
        """
        const rounds = [];
        for (let seed = 1; seed <= 8; seed++) {
          const g = game(seed, 0, 4);
          let first = 0, n = 0;
          while (!sim.step(g) && n < 50000) { n++; if (!first && g.playersAlive < 4) first = n; }
          rounds.push({n, first, over: g.roundOver, alive: g.playersAlive,
                       top: Math.max(...g.players.map(p => p.points))});
        }
        return rounds;
        """,
    )
    for one in found:
        assert one["over"] is True and one["n"] < 50000
        # Nobody crashes in the first seconds: the KI avoids the walls and the others.
        assert one["first"] > 120
        # The last one gets 3 for the crashes before it, or players + 5 if it still rides.
        assert one["top"] in (3, 9, 12, 24, 27)


async def test_a_seed_and_the_inputs_decide_the_whole_round(game_page) -> None:
    found = await js(
        game_page,
        """
        const play = (seed) => {
          const g = game(seed, 1, 3);
          let n = 0;
          while (!sim.step(g, n % 97 === 40 ? [[0, 'L']] : n % 97 === 80 ? [[0, 'R']] : [])) n++;
          let sum = 0;
          for (let i = 0; i < g.buf.length; i++) sum = (sum * 31 + g.buf[i]) >>> 0;
          const riders = g.players.map(p => [p.points, p.pos.x, p.pos.y, p.rdiv, p.rthres]);
          return JSON.stringify([n, sum, riders]);
        };
        return [play(7), play(7), play(8)];
        """,
    )
    assert found[0] == found[1]
    assert found[0] != found[2]
    assert json.loads(found[0])[0] > 50


async def test_the_presets_and_the_default_keys_are_the_originals(game_page) -> None:
    found = await js(
        game_page,
        """
        const g = sim.createGame(1);
        return {speed: g.speed, wait: g.waitStates,
                players: g.players.map(p => [p.name, p.active, p.human, p.lkey, p.rkey])};
        """,
    )
    assert found["speed"] == 2 and found["wait"] == 0
    assert found["players"][0] == ["Player 1", True, True, "ControlLeft", "AltLeft"]
    assert found["players"][1] == ["Player 2", True, True, "ArrowLeft", "ArrowRight"]
    assert found["players"][9] == ["Player10", False, True, "NumpadAdd", "NumpadSubtract"]
    assert [p[3:] for p in found["players"][2:9]] == [
        ["KeyV", "KeyB"],
        ["KeyN", "KeyM"],
        ["CapsLock", "ShiftLeft"],
        ["KeyQ", "Digit1"],
        ["Numpad0", "NumpadDecimal"],
        ["Space", "AltRight"],
        ["PageDown", "PageUp"],
    ]


async def test_the_setup_menus_change_the_players_and_keep_save_games(game_page) -> None:
    found = await js(
        game_page,
        """
        const store = new Map();
        const storage = {
          setItem: (k, v) => store.set(k, v),
          getItem: (k) => store.has(k) ? store.get(k) : null,
        };
        const s = flow.createSession({seed: 1, storage});
        const press = (...codes) => codes.forEach(code => flow.keyDown(s, code));
        const p = s.game.players[2];
        press('Enter', 'Digit7', 'ArrowUp', 'ArrowUp', 'ArrowDown', 'F3');
        const phase = s.phase;
        press('F1', 'F1');                       // inactive -> person -> KI
        press('F3', 'Backspace', 'Backspace', 'KeyK', 'KeyI', 'Enter');
        press('F4', 'KeyX', 'F5', 'Pause', 'KeyC', 'F6');
        const edited = [p.active, p.human, p.name, p.lkey, p.rkey, p.color, s.phase];
        press('Enter');
        flow.keyDown(s, 'F2', new Set(['ControlLeft', 'F2']));   // save game 2
        press('F3', 'F2', 'Escape');             // reset the player, then cancel: restored
        const kept = [p.active, p.name];
        press('F3', 'F2', 'Enter');              // reset the player and keep that
        const reset = [p.active, p.human, p.name, p.lkey];
        flow.keyDown(s, 'F2', new Set(['ShiftLeft', 'F2']));     // load game 2
        const loaded = [p.active, p.human, p.name, p.lkey, p.rkey, p.color];
        press('Escape');
        const [speed, wait] = [s.game.speed, s.game.waitStates];
        return {phase, speed, wait, edited, kept, reset, loaded, end: s.phase};
        """,
    )
    assert found["phase"] == "player" and found["end"] == "menu"
    assert found["speed"] == 7 and found["wait"] == 1
    assert found["edited"] == [True, False, "PlayerKI", "KeyX", "KeyC", 3, "player"]
    assert found["kept"] == [True, "PlayerKI"]
    assert found["reset"] == [False, True, "Player 3", "KeyX"]
    assert found["loaded"] == [True, False, "PlayerKI", "KeyX", "KeyC", 3]


async def canvas_sum(page: Any) -> int:
    """Add up the visible canvas's pixels, to tell two pictures apart.

    Args:
        page: The loaded page.

    Returns:
        A checksum of the canvas's content.
    """
    return await page.evaluate(
        """() => {
          const c = document.getElementById('screen');
          const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
          let sum = 0;
          for (let i = 0; i < d.length; i += 4) {
            sum = (sum * 31 + d[i] + d[i + 1] * 3 + d[i + 2] * 7) >>> 0;
          }
          return sum;
        }"""
    )


async def test_a_round_played_with_the_keyboard_from_the_menu_to_the_scores(game_page) -> None:
    """A round from the main menu to the score table, played with the keyboard.

    Side effects: writes screenshots of the menus and the round to `SHOTS`
    (`~/.cache/calu-uitest/share/lightcycles`, or `$CALU_LIGHTCYCLES_SHOTS`).
    """
    page = game_page
    SHOTS.mkdir(parents=True, exist_ok=True)
    hook = "window.__lightcyclesTest"
    # The main menu: the water moves the title picture.
    first = await canvas_sum(page)
    await page.wait_for_timeout(700)
    assert await canvas_sum(page) != first
    await page.screenshot(path=str(SHOTS / "1-title-water.png"))

    # Setup with real keys: player 3 becomes a computer player.
    await page.keyboard.press("Enter")
    await page.keyboard.press("F3")
    await page.keyboard.press("F1")
    await page.keyboard.press("F1")
    await page.keyboard.press("Enter")
    assert await page.evaluate(f"() => {hook}.session.phase") == "setup"
    await page.screenshot(path=str(SHOTS / "2-setup.png"))
    await page.keyboard.press("Enter")

    # Space starts the round; from here the test advances the frames itself.
    await page.evaluate(f"() => {hook}.setRealTime(false)")
    await page.keyboard.press("Space")
    assert await page.evaluate(f"() => {hook}.session.phase") == "intro"
    await page.evaluate(f"() => {hook}.advance(60)")
    assert await page.evaluate(f"() => {hook}.session.phase") == "play"

    # Player 1 turns left with its original key, Left Ctrl; player 2 right with the arrow.
    await page.evaluate(f"() => {hook}.advance(40)")
    await page.keyboard.press("ControlLeft")
    await page.keyboard.press("ArrowRight")
    await page.evaluate(f"() => {hook}.advance(1)")
    directions = await page.evaluate(
        f"() => {hook}.session.game.players.slice(0, 2).map(p => p.direction)"
    )
    assert directions == [0, 0]
    # A held key that repeats turns once only.
    await page.keyboard.down("ArrowLeft")
    await page.keyboard.down("ArrowLeft")
    await page.keyboard.up("ArrowLeft")
    await page.evaluate(f"() => {hook}.advance(1)")
    assert await page.evaluate(f"() => {hook}.session.game.players[1].direction") == 3
    await page.evaluate(f"() => {hook}.advance(60)")
    await page.screenshot(path=str(SHOTS / "3-round.png"))

    # Both people ride on until they crash; then the statistics show the scores.
    await page.evaluate(f"() => {hook}.advance(3000)")
    assert await page.evaluate(f"() => {hook}.session.phase") == "stats"
    status = await page.inner_text("#status")
    assert status.startswith("stats. Player 1: score ") and "Player 3: score " in status
    await page.screenshot(path=str(SHOTS / "4-statistics.png"))
    await page.keyboard.press("Space")
    assert await page.evaluate(f"() => {hook}.session.phase") == "menu"


async def test_the_title_stands_still_for_a_user_who_asked_for_reduced_motion(page) -> None:
    await page.emulate_media(reduced_motion="reduce")
    async with serve(create_app()) as base:
        await page.goto(base)
        await page.wait_for_function("window.__lightcyclesTest !== undefined")
        await page.wait_for_timeout(200)
        first = await canvas_sum(page)
        await page.wait_for_timeout(500)
        assert await canvas_sum(page) == first


async def test_the_command_lines_options_reach_the_page(page) -> None:
    async with serve(create_app()) as base:
        await page.goto(f"{base}/?players=1&bots=3&speed=4&seed=9")
        await page.wait_for_function("window.__lightcyclesTest !== undefined")
        found = await page.evaluate(
            """() => {
              const g = window.__lightcyclesTest.session.game;
              return [g.speed, g.players.map(p => p.active ? (p.human ? 'P' : 'K') : '-').join('')];
            }"""
        )
        assert found == [4, "PKKK------"]
