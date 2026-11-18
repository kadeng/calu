// MetaTron's menus and the flow between them, ported from tron.cpp. Pure like `sim.js`: no
// DOM. Keys arrive as `KeyboardEvent.code` strings, time as calls of `frame` (60 per second).
//
//   menu --Space--> intro (names, 1 s) --> play --round over--> stats --Space--> menu
//    |  ^                                   | Escape (round not counted)          ^
//    |  +-----------------------------------+                                     |
//    +--S (scores)----------------------------------------------------------------+
//    +--Enter--> setup --F1..F10--> player --F3--> name
//    |             ^ Enter/Escape     |  ^  --F4/F5--> lkey / rkey (the next key is taken)
//    +--Escape--> bye                 +--+  Enter keeps, Escape restores

import * as sim from "./sim.js";

/** Frames the names are shown before a round starts (the original waits 1000 ms). */
export const INTRO_FRAMES = 60;
/** Longest player name, as in the original. */
export const NAME_LENGTH = 11;

/**
 * Create a session: a game with its players, in the main menu.
 * @param {object} options What to start with.
 * @param {number} options.seed Seed of the game's random generator.
 * @param {Storage|null} [options.storage] Where save games go (`localStorage`), or null.
 * @returns {object} The session. `keyDown` and `frame` change it.
 */
export function createSession({ seed, storage = null }) {
  return {
    game: sim.createGame(seed),
    phase: "menu",
    paused: false,
    selected: 0,
    backup: null,
    pending: [],
    introLeft: 0,
    storage,
    notice: "",
  };
}

/**
 * Choose who plays, for the command line's options: the first `people` players are people,
 * the next `bots` are computer players, the rest is inactive.
 * @param {object} session The session.
 * @param {number} people Number of people, 0 to 10.
 * @param {number} bots Number of computer players; cut so that the sum is at most 10.
 */
export function choosePlayers(session, people, bots) {
  session.game.players.forEach((p, i) => {
    p.active = i < people + bots;
    p.human = i < people;
  });
}

/**
 * Start a round and show the names.
 * @param {object} session The session.
 */
function startGame(session) {
  sim.startRound(session.game);
  session.pending = [];
  session.paused = false;
  session.introLeft = INTRO_FRAMES;
  session.phase = "intro";
}

/**
 * Write or read a save game: the ten players with their setup and statistics.
 * @param {object} session The session.
 * @param {number} slot 1 to 9.
 * @param {boolean} save True writes, false reads.
 */
function saveGame(session, slot, save) {
  const key = `metatron.save${slot}`;
  try {
    if (save) {
      session.storage.setItem(key, JSON.stringify(sim.exportPlayers(session.game)));
      session.notice = `SAVED GAME ${slot}`;
    } else {
      const text = session.storage.getItem(key);
      if (text === null) return;
      sim.importPlayers(session.game, JSON.parse(text));
      session.notice = `LOADED SAVEGAME ${slot}`;
    }
  } catch {
    session.notice = "NO STORAGE IN THIS BROWSER";
  }
}

/**
 * Handle a key in the setup menu.
 * @param {object} session The session.
 * @param {string} code The key.
 * @param {Set<string>} held The keys that are down, this one included.
 */
function setupKey(session, code, held) {
  const game = session.game;
  session.notice = "";
  if (code === "Escape" || code === "Enter") session.phase = "menu";
  const digit = /^Digit([1-9])$/.exec(code);
  if (digit) game.speed = Number(digit[1]);
  if (code === "ArrowUp") game.waitStates++;
  if (code === "ArrowDown" && game.waitStates > 0) game.waitStates--;
  const fkey = /^F([1-9]|10)$/.exec(code);
  if (fkey) {
    const number = Number(fkey[1]);
    if (number < 10 && held.has("ControlLeft")) saveGame(session, number, true);
    else if (number < 10 && held.has("ShiftLeft")) saveGame(session, number, false);
    else {
      session.selected = number - 1;
      session.backup = { ...game.players[session.selected] };
      session.phase = "player";
    }
  }
  if (held.has("AltLeft") && held.has("AltRight")) {
    for (let i = 0; i < sim.MAX_PLAYERS; i++) sim.resetPlayerScores(game, i);
  }
}

/**
 * Handle a key in the setup of one player.
 * @param {object} session The session.
 * @param {string} code The key.
 */
function playerKey(session, code) {
  const game = session.game;
  const p = game.players[session.selected];
  if (code === "Escape") {
    Object.assign(p, session.backup);
    session.phase = "setup";
  }
  if (code === "Enter") session.phase = "setup";
  if (code === "F1") {
    if (!p.active) p.active = true;
    else if (p.human) p.human = false;
    else {
      p.active = false;
      p.human = true;
    }
  }
  if (code === "F2") sim.resetPlayer(game, session.selected);
  if (code === "F3") {
    session.keep = p.name;
    session.phase = "name";
  }
  if (code === "F4" || code === "F5") {
    const which = code === "F4" ? "lkey" : "rkey";
    session.keep = p[which];
    p[which] = "";
    session.phase = which;
  }
  if (code === "F6") p.color = (p.color + 1) % sim.COLOURS;
  if (code === "F7") sim.resetPlayerScores(game, session.selected);
}

/**
 * Handle a key while a player's name is typed: letters and digits (as capitals), Space,
 * Backspace; Enter keeps the name, Escape restores the old one.
 * @param {object} session The session.
 * @param {string} code The key.
 */
function nameKey(session, code) {
  const p = session.game.players[session.selected];
  if (code === "Escape") {
    p.name = session.keep;
    session.phase = "player";
  } else if (code === "Enter") session.phase = "player";
  else if (code === "Backspace") p.name = p.name.slice(0, -1);
  else if (p.name.length < NAME_LENGTH) {
    const found = /^(?:Key([A-Z])|Digit([0-9]))$/.exec(code);
    if (found) p.name += found[1] || found[2];
    else if (code === "Space") p.name += " ";
  }
}

/**
 * Handle a key pressed down. A held key that repeats must not be passed: the original
 * reacts to the moment a key goes down.
 * @param {object} session The session.
 * @param {string} code The key, as `KeyboardEvent.code`.
 * @param {Set<string>} [held] The keys that are down, this one included; the setup menu
 *   reads its combinations from it.
 */
export function keyDown(session, code, held = new Set([code])) {
  const game = session.game;
  switch (session.phase) {
    case "menu":
      if (code === "Escape") session.phase = "bye";
      if (code === "Space") startGame(session);
      if (code === "KeyS") {
        for (const p of game.players) {
          p.alive = false;
          p.escaped = false;
        }
        session.phase = "stats";
      }
      if (code === "Enter") {
        session.notice = "";
        session.phase = "setup";
      }
      break;
    case "bye":
      session.phase = "menu";
      break;
    case "stats":
      if (code === "Space") session.phase = "menu";
      break;
    case "setup":
      setupKey(session, code, held);
      break;
    case "player":
      playerKey(session, code);
      break;
    case "name":
      nameKey(session, code);
      break;
    case "lkey":
    case "rkey": {
      const p = game.players[session.selected];
      if (code === "Escape") p[session.phase] = session.keep;
      else if (code === "Pause") break;
      else p[session.phase] = code;
      session.phase = "player";
      break;
    }
    case "play":
      game.players.forEach((p, i) => {
        if (code === p.lkey) session.pending.push([i, "L"]);
        if (code === p.rkey) session.pending.push([i, "R"]);
      });
      if (code === "Escape") {
        sim.abortRound(game);
        session.phase = "menu";
      }
      // The original pauses with the Pause key; P is added for keyboards without one.
      if (code === "Pause" || code === "KeyP") session.paused = !session.paused;
      break;
    default:
      break;
  }
}

/**
 * Advance the session by one frame of 1/60 s. In a round this is one `sim.step` with the
 * keys pressed since the last frame; while the round is paused the keys still turn the
 * cycles, as in the original.
 * @param {object} session The session.
 */
export function frame(session) {
  if (session.phase === "intro") {
    if (--session.introLeft <= 0) session.phase = "play";
    return;
  }
  if (session.phase !== "play") return;
  const inputs = session.pending;
  session.pending = [];
  if (session.paused) sim.applyInputs(session.game, inputs);
  else if (sim.step(session.game, inputs)) session.phase = "stats";
}

/**
 * Describe the session in one line of plain text, for screen readers and tests.
 * @param {object} session The session.
 * @returns {string} The phase, and on the statistics screen each active player's scores.
 */
export function describe(session) {
  if (session.phase !== "stats") return session.phase;
  const rows = session.game.players
    .filter((p) => p.active)
    .map((p) => `${p.name.trim()}: score ${p.points}, total ${p.tpoints}`);
  return `stats. ${rows.join("; ")}`;
}
