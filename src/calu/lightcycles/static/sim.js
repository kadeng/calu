// MetaTron's game mechanics, ported from the C++ of MetaTron 1.2 by Kai Londenberg
// (tron.cpp, classes.hpp). This module is pure: no DOM, no timer, no keyboard. Everything
// random comes from the game's own generator, so a seed and a list of inputs give one game.
//
// The field is the original's 800x600 screen as one byte per pixel: 0 is empty, any other
// value is a wall (a cycle's trail, the frame, a spark of an explosion) and stops a cycle.
//
// One frame (`step`), as the original's UpdateFrame:
//
//   inputs (key presses) --> turn the players' cycles
//   for player 0..9, in this order:
//       its explosion runs --> move the sparks (they erase what they pass)
//       it rides, KI -------> react(): look ahead and to both sides, maybe turn
//       it rides -----------> draw `speed` pixels of wall from its position
//             a pixel on the way is not empty, or the position is at the screen's edge:
//             crash --> points for everyone who crashed earlier, escaped?, explosion
//   one cycle left --> countdown of 230 frames --> points --> round over
//   no person left -------------------------------> points --> round over

export const WIDTH = 800;
export const HEIGHT = 600;
export const MAX_PLAYERS = 10;
/** Value of the frame's pixels in the field. Players' walls have the values 1 to 10. */
export const FRAME_VALUE = 11;
/** Number of colours a player can choose from (the original had 5 palette entries). */
export const COLOURS = 10;
/** Frames the last cycle keeps riding before the round is counted. */
export const COUNTDOWN = 230;
export const RAND_MAX = 32767;

const PARTICLES = 30;
const KILLRADIUS = 17;
const DURATION = 25;

/** The default keys of the ten players, as `KeyboardEvent.code` (left key, right key). */
export const DEFAULT_KEYS = [
  ["ControlLeft", "AltLeft"],
  ["ArrowLeft", "ArrowRight"],
  ["KeyV", "KeyB"],
  ["KeyN", "KeyM"],
  ["CapsLock", "ShiftLeft"],
  ["KeyQ", "Digit1"],
  ["Numpad0", "NumpadDecimal"],
  ["Space", "AltRight"],
  ["PageDown", "PageUp"],
  ["NumpadAdd", "NumpadSubtract"],
];

/**
 * The random generator: the linear congruential generator of the C runtime the original was
 * built with, so `next()` has the range of its `rand()` (0 to 32767).
 */
export class Rng {
  /** @param {number} seed The start value. */
  constructor(seed) {
    this.state = seed >>> 0;
  }

  /**
   * Start the sequence again (the original's `srand`).
   * @param {number} seed The start value.
   */
  seed(seed) {
    this.state = seed >>> 0;
  }

  /** @returns {number} The next number, 0 to 32767. */
  next() {
    this.state = (Math.imul(this.state, 214013) + 2531011) >>> 0;
    return (this.state >>> 16) & 0x7fff;
  }
}

/**
 * Create the players and an empty field, with the original's presets: players 1 and 2 are
 * active people, the other eight are inactive.
 * @param {number} seed Seed of the game's random generator.
 * @returns {object} The game state. `step`, `startRound` and the other functions change it.
 */
export function createGame(seed) {
  const game = {
    buf: new Uint8Array(WIDTH * HEIGHT),
    players: [],
    explosions: new Array(MAX_PLAYERS).fill(null),
    rng: new Rng(seed),
    explosionSeed: 1,
    speed: 2,
    waitStates: 0,
    waitLeft: 0,
    playersCount: 0,
    humanPlayers: 0,
    playersAlive: 0,
    humanPlayersAlive: 0,
    countdown: COUNTDOWN,
    roundOver: true,
    frame: 0,
  };
  for (let i = 0; i < MAX_PLAYERS; i++) {
    // The KI's values: the original's constructor leaves `rdiv` and `rthres` unset; 8 and
    // 800 are what its assignment operator gives them.
    game.players.push({ lct: 0, rct: 0, reaction: 4, rcount: 0, rdiv: 8, rthres: 800, impos: { x: 0, y: 0 }, pos: { x: 0, y: 0 }, direction: 1 });
    resetPlayer(game, i);
    game.players[i].lkey = DEFAULT_KEYS[i][0];
    game.players[i].rkey = DEFAULT_KEYS[i][1];
  }
  game.players[0].active = true;
  game.players[1].active = true;
  return game;
}

/**
 * Put a player back to its preset: inactive, a person, the default name, no scores. The keys
 * stay (the original's ResetPlayer). The colour: the original gave every player the same
 * colour; the port gives each player its own.
 * @param {object} game The game.
 * @param {number} i The player's index, 0 to 9.
 */
export function resetPlayer(game, i) {
  const p = game.players[i];
  p.active = false;
  p.human = true;
  p.name = "Player" + String(i + 1).padStart(2, " ");
  p.color = i % COLOURS;
  resetPlayerScores(game, i);
}

/**
 * Set a player's statistics to zero.
 * @param {object} game The game.
 * @param {number} i The player's index.
 */
export function resetPlayerScores(game, i) {
  const p = game.players[i];
  p.alive = false;
  p.escaped = false;
  p.points = 0;
  p.games = 0;
  p.tescaped = 0;
  p.tpoints = 0;
  p.tsurvived = 0;
}

/**
 * Turn a player's cycle a quarter turn to the left. `lct` and `rct` count the net turns; the
 * KI uses them against riding in circles.
 * @param {object} p The player.
 */
export function turnLeft(p) {
  p.lct++;
  p.rct--;
  p.direction = (p.direction + 3) % 4;
}

/**
 * Turn a player's cycle a quarter turn to the right.
 * @param {object} p The player.
 */
export function turnRight(p) {
  p.rct++;
  p.lct--;
  p.direction = (p.direction + 1) % 4;
}

/**
 * Say whether a position is on the screen and not at its edge (the original's InGameScreen).
 * @param {{x: number, y: number}} pos The position.
 * @returns {boolean} False at the edge and outside.
 */
export function inGameScreen(pos) {
  return !(pos.x <= 1 || pos.x >= WIDTH - 1 || pos.y <= 1 || pos.y >= HEIGHT - 1);
}

/**
 * Draw a line of wall from a position and move the position to the line's end (the
 * original's DrawPlayer). The line is cut at the screen's edge. Drawing stops at the first
 * pixel that is not empty; the position is moved to the full length all the same.
 * @param {object} game The game.
 * @param {{x: number, y: number}} pos Start of the line; changed to its end.
 * @param {number} direction 0 up, 1 right, 2 down, 3 left.
 * @param {number} value The value to write, 1 to 11.
 * @param {number} len Length in pixels.
 * @returns {number} 0 if the whole line was drawn; -1 if the position was at the screen's
 *   edge already; else the value of the pixel that stopped the line.
 */
export function drawPlayer(game, pos, direction, value, len) {
  if (pos.x >= WIDTH - 1 || pos.x <= 1 || pos.y <= 1 || pos.y >= HEIGHT - 1) return -1;
  const buf = game.buf;
  let dst = pos.x + WIDTH * pos.y;
  let stride = 0;
  if (direction === 0) {
    if (1 > pos.y - len) len = pos.y - 1;
    pos.y -= len;
    stride = -WIDTH;
  } else if (direction === 1) {
    if (WIDTH - 1 < pos.x + len) len = WIDTH - 1 - pos.x;
    pos.x += len;
    stride = 1;
  } else if (direction === 2) {
    if (HEIGHT - 1 < pos.y + len) len = HEIGHT - 1 - pos.y;
    pos.y += len;
    stride = WIDTH;
  } else {
    if (1 > pos.x - len) len = pos.x - 1;
    pos.x -= len;
    stride = -1;
  }
  for (let ct = 0; ct < len; ct++) {
    if (buf[dst] !== 0) return buf[dst];
    buf[dst] = value;
    dst += stride;
  }
  return 0;
}

/**
 * Count the empty pixels from a position in a direction, the position included (the
 * original's safedirection). Sets `p.impos` to the point `len` pixels away (cut at the
 * screen's edge), whether or not the way there is empty.
 * @param {object} game The game.
 * @param {object} p The player that looks.
 * @param {number} len How far to look.
 * @param {number} dir The direction to look in.
 * @param {{x: number, y: number}|null} from Where to look from; null is the player's position.
 * @returns {number} The number of empty pixels before the first wall, at most `len`; 1 if
 *   the start is at the screen's edge.
 */
export function safeDirection(game, p, len, dir, from = null) {
  const x = from ? from.x : p.pos.x;
  const y = from ? from.y : p.pos.y;
  p.impos = { x, y };
  if (x >= WIDTH - 1 || x <= 1 || y <= 1 || y >= HEIGHT - 1) return 1;
  const buf = game.buf;
  let dst = x + WIDTH * y;
  let stride = 0;
  if (dir === 0) {
    if (1 > y - len) len = y - 1;
    p.impos.y -= len;
    stride = -WIDTH;
  } else if (dir === 1) {
    if (WIDTH - 1 < x + len) len = WIDTH - 1 - x;
    p.impos.x += len;
    stride = 1;
  } else if (dir === 2) {
    if (HEIGHT - 1 < y + len) len = HEIGHT - 1 - y;
    p.impos.y += len;
    stride = WIDTH;
  } else {
    if (1 > x - len) len = x - 1;
    p.impos.x -= len;
    stride = -1;
  }
  for (let ct = 0; ct < len; ct++) {
    if (buf[dst] !== 0) return ct;
    dst += stride;
  }
  return len;
}

/**
 * Measure the room around the far end of the line the player just looked along (the
 * original's freespace): from that point 11 pixels to each side, and 6 pixels on and back.
 * The player's direction is the looked-along direction before and after the call.
 * @param {object} game The game.
 * @param {object} p The player; `p.impos` is the far end.
 * @returns {number} The sum of the empty pixels found, 0 to 46.
 */
function freeSpace(game, p) {
  const far = { x: p.impos.x, y: p.impos.y };
  const side = p.reaction + 7;
  const near = p.reaction + 2;
  turnLeft(p);
  let room = safeDirection(game, p, side, p.direction, far);
  if (room > p.reaction) {
    turnRight(p);
    room += safeDirection(game, p, near, p.direction, far);
    turnLeft(p);
    turnLeft(p);
    room += safeDirection(game, p, near, p.direction, far);
    turnRight(p);
  }
  turnRight(p);
  turnRight(p);
  room += safeDirection(game, p, side, p.direction, far);
  if (room > p.reaction) {
    turnRight(p);
    room += safeDirection(game, p, near, p.direction, far);
    turnLeft(p);
    turnLeft(p);
    room += safeDirection(game, p, near, p.direction, far);
    turnRight(p);
  }
  turnLeft(p);
  return room;
}

/**
 * Rate riding straight on (the original's safestraight).
 * @param {object} game The game.
 * @param {object} p The player.
 * @param {number} range How far to look.
 * @returns {number} -1 if fewer than `reaction` pixels ahead are empty; else the empty pixels
 *   ahead plus a quarter of the room at the far end.
 */
function safeStraight(game, p, range) {
  const ahead = safeDirection(game, p, range, p.direction);
  if (ahead < p.reaction) return -1;
  return ahead + Math.trunc(freeSpace(game, p) / 4);
}

/**
 * Rate a quarter turn (the original's safeleft and saferight).
 * @param {object} game The game.
 * @param {object} p The player; its direction is the same before and after the call.
 * @param {number} range How far to look.
 * @param {boolean} toRight True rates the right turn, false the left turn.
 * @returns {number} -1 if fewer than `reaction` pixels that way are empty; else the empty
 *   pixels plus (if more than `reaction`) a quarter of the room at the far end.
 */
function safeTurn(game, p, range, toRight) {
  if (toRight) turnRight(p);
  else turnLeft(p);
  let free = safeDirection(game, p, range, p.direction);
  if (free > p.reaction) free += Math.trunc(freeSpace(game, p) / 4);
  if (toRight) turnLeft(p);
  else turnRight(p);
  return free < p.reaction ? -1 : free;
}

/**
 * Let a computer player decide for this frame: ride on, or turn (the original's
 * PlayerKI::React). It rates straight, left and right by the empty pixels within 19 pixels
 * plus a random bonus, turns where that is clearly better, turns at random now and then, and
 * looks 34 pixels far when nothing is clearly better. A turn must be better by half the net
 * number of turns already made that way, which works against riding in circles.
 * @param {object} game The game; its random generator is used.
 * @param {object} p The computer player; its direction and counters may change.
 */
export function react(game, p) {
  const rng = game.rng;
  const bonus = () => Math.trunc((rng.next() * p.rdiv) / RAND_MAX);
  const rl = bonus();
  const rr = bonus();
  const rs = bonus();
  p.rcount--;
  const ss = safeStraight(game, p, p.reaction + 15) + 2 + rs;
  let sl = safeTurn(game, p, p.reaction + 15, false) + rl;
  let sr = safeTurn(game, p, p.reaction + 15, true) + rr;
  if (sr > ss && sr > sl + (p.rct >> 1)) return turnRight(p);
  if (sl > ss && sl > sr + (p.lct >> 1)) return turnLeft(p);
  if (ss - 1 > sr && ss - 1 > sl) return undefined;
  if (rng.next() < p.rthres) {
    if (p.rcount > 0) return undefined;
    p.rcount = 20;
    if (sr > sl + (p.rct >> 1)) return turnRight(p);
    if (sl > sr + (p.lct >> 1)) return turnLeft(p);
    return (rng.next() & 1) !== 0 ? turnLeft(p) : turnRight(p);
  }
  if (ss > sr && ss > sl) return undefined;
  sl = safeTurn(game, p, p.reaction + 30, false) + (p.rct >> 1);
  sr = safeTurn(game, p, p.reaction + 30, true) + (p.lct >> 1);
  if (sr > sl) return turnRight(p);
  if (sl > sr) return turnLeft(p);
  return (rng.next() & 1) === 1 ? turnLeft(p) : turnRight(p);
}

/**
 * Change a player's KI values a little after its crash (the original's PlayerKI::die): the
 * size of the random bonus by -1, 0 or +1 and the chance of a random turn by up to 127.
 * @param {object} game The game; its random generator is used.
 * @param {object} p The player.
 */
function die(game, p) {
  const rd = game.rng.next();
  p.rdiv += rd < 10922 ? -1 : rd > 21844 ? 1 : 0;
  p.rthres += Math.trunc((game.rng.next() - 16383) / 128);
}

/**
 * Start an explosion (the original's Explosion constructor): seed the random generator anew
 * (the original does, with 2, 3, 4, ... per explosion), give 30 sparks their directions, and
 * empty a square of 33x33 pixels around the centre -- walls, frame and all -- unless the
 * square would touch the screen's edge.
 * @param {object} game The game.
 * @param {{x: number, y: number}} center The crashed cycle's position.
 * @param {number} value The crashed player's wall value, which the sparks get.
 * @returns {object} The explosion, for `updateExplosion`.
 */
function createExplosion(game, center, value) {
  game.explosionSeed++;
  game.rng.seed(game.explosionSeed);
  const explosion = { center: { x: center.x, y: center.y }, value, age: 0, xdir: [], ydir: [], pos: [] };
  for (let ct = 0; ct < PARTICLES; ct++) {
    explosion.pos.push({ x: center.x, y: center.y });
    explosion.xdir.push(game.rng.next() - 16383);
    explosion.ydir.push(game.rng.next() - 16383);
  }
  const low = { x: center.x - KILLRADIUS, y: center.y - KILLRADIUS };
  const high = { x: center.x + KILLRADIUS, y: center.y + KILLRADIUS };
  if (!inGameScreen(low) || !inGameScreen(high)) return explosion;
  for (let i = 0; i < KILLRADIUS; i++) {
    for (let ct = 0; ct < KILLRADIUS; ct++) {
      game.buf[(center.y + i) * WIDTH + center.x + ct] = 0;
      game.buf[(center.y + i) * WIDTH + center.x - ct] = 0;
      game.buf[(center.y - i) * WIDTH + center.x + ct] = 0;
      game.buf[(center.y - i) * WIDTH + center.x - ct] = 0;
    }
  }
  return explosion;
}

/**
 * Move an explosion's sparks by one frame (the original's Explosion::Update). A spark
 * empties the pixel it leaves and is a wall where it is. After 25 frames every second spark
 * is removed; the other 15 stay on the field as walls.
 * @param {object} game The game.
 * @param {object} explosion The explosion.
 * @returns {boolean} True when the explosion has ended.
 */
function updateExplosion(game, explosion) {
  const buf = game.buf;
  explosion.age++;
  if (explosion.age > DURATION) {
    for (let ct = 0; ct < PARTICLES - 1; ct += 2) buf[explosion.pos[ct].y * WIDTH + explosion.pos[ct].x] = 0;
    return true;
  }
  for (let ct = 0; ct < PARTICLES; ct++) {
    const pos = explosion.pos[ct];
    buf[pos.x + pos.y * WIDTH] = 0;
    const next = {
      x: explosion.center.x + ((explosion.age * explosion.xdir[ct]) >> 15),
      y: explosion.center.y + ((explosion.age * explosion.ydir[ct]) >> 15),
    };
    if (inGameScreen(next)) explosion.pos[ct] = next;
    buf[explosion.pos[ct].x + explosion.pos[ct].y * WIDTH] = explosion.value;
  }
  return false;
}

/**
 * Draw the frame around the field: a rectangle from (30, 30), 740 wide and 540 high.
 * @param {object} game The game.
 * @param {number} value The value of the frame's pixels.
 */
export function drawFrame(game, value = FRAME_VALUE) {
  const pos = { x: 30, y: 30 };
  drawPlayer(game, pos, 1, value, 740);
  drawPlayer(game, pos, 2, value, 540);
  drawPlayer(game, pos, 3, value, 740);
  drawPlayer(game, pos, 0, value, 540);
}

/**
 * Start a round (the original's StartGame and PreparePlayers): count the active players,
 * put every player on its start position, empty the field and draw the frame. Players with
 * an even index start at x 150 heading right, the others at x 650 heading left; players 1
 * and 2 face each other on one row, 3 and 4 on the next, 90 pixels lower, and so on.
 * @param {object} game The game.
 */
export function startRound(game) {
  game.playersCount = 0;
  game.humanPlayers = 0;
  for (let i = 0; i < MAX_PLAYERS; i++) {
    const p = game.players[i];
    if (p.active) {
      game.playersCount++;
      if (p.human) game.humanPlayers++;
      p.games++;
    }
    p.alive = p.active;
    p.escaped = false;
    p.points = 0;
    game.explosions[i] = null;
    if (i % 2 === 0) {
      p.direction = 1;
      p.pos = { x: 150, y: 70 + 45 * i };
    } else {
      p.direction = 3;
      p.pos = { x: 650, y: 25 + 45 * i };
    }
  }
  game.playersAlive = game.playersCount;
  game.humanPlayersAlive = game.humanPlayers;
  game.countdown = COUNTDOWN;
  game.waitLeft = 0;
  game.roundOver = false;
  game.frame = 0;
  game.buf.fill(0);
  drawFrame(game);
}

/**
 * Add up a round (the original's CalculatePoints): `players + 5` points and a victory for a
 * cycle that still rides, `players + 20` points and an escape for a cycle that left the
 * screen; then the round's points are added to the totals.
 * @param {object} game The game.
 */
export function calculatePoints(game) {
  for (const p of game.players) {
    if (!p.active) continue;
    if (p.alive) {
      p.points += game.playersCount + 5;
      p.tsurvived++;
    }
    if (p.escaped) {
      p.points += game.playersCount + 20;
      p.tescaped++;
    }
    p.tpoints += p.points;
  }
}

/**
 * Turn the cycles for the keys pressed since the last frame. As in the original, a key turns
 * its player's cycle whether the player is a person or a KI, rides or not.
 * @param {object} game The game.
 * @param {Array<[number, string]>} inputs Pairs of player index and "L" or "R".
 */
export function applyInputs(game, inputs) {
  for (const [index, turn] of inputs) {
    if (turn === "L") turnLeft(game.players[index]);
    else turnRight(game.players[index]);
  }
}

/**
 * Advance a running round by one frame (see the diagram at the top of this file). With wait
 * states, only every `waitStates + 1`-th call moves the cycles; the others only take inputs.
 *
 * One rule differs from the original: there a round ends at once when no person rides, so a
 * round of computer players only ended in its first frame. Here that rule applies only to a
 * round with at least one person; a round without people ends after its last crash.
 * @param {object} game The game, after `startRound`.
 * @param {Array<[number, string]>} inputs The key presses since the last call.
 * @returns {boolean} True once the round is over and its points are counted.
 */
export function step(game, inputs = []) {
  applyInputs(game, inputs);
  if (game.roundOver) return true;
  if (game.waitLeft > 0) {
    game.waitLeft--;
    return false;
  }
  game.waitLeft = game.waitStates;
  game.frame++;
  for (let i = 0; i < MAX_PLAYERS; i++) {
    if (game.explosions[i] !== null && updateExplosion(game, game.explosions[i])) game.explosions[i] = null;
    const p = game.players[i];
    if (!p.alive) continue;
    if (!p.human) react(game, p);
    if (drawPlayer(game, p.pos, p.direction, p.color + 1, game.speed) === 0) continue;
    p.points += game.playersCount - game.playersAlive;
    if (!inGameScreen(p.pos)) p.escaped = true;
    p.alive = false;
    die(game, p);
    game.playersAlive--;
    if (p.human) game.humanPlayersAlive--;
    if (!p.escaped) game.explosions[i] = createExplosion(game, p.pos, p.color + 1);
  }
  let over = false;
  if (game.playersAlive === 1 && game.countdown-- < 0) over = true;
  if (game.humanPlayers > 0 ? game.humanPlayersAlive === 0 : game.playersAlive === 0) over = true;
  if (over) {
    calculatePoints(game);
    game.roundOver = true;
  }
  return over;
}

/**
 * Leave a running round without counting it (the original's Escape key in a round): the
 * round is taken off every active player's number of games.
 * @param {object} game The game.
 */
export function abortRound(game) {
  for (const p of game.players) if (p.active) p.games--;
  game.roundOver = true;
}

const SAVED = ["name", "active", "human", "color", "lkey", "rkey", "points", "tpoints", "tsurvived", "tescaped", "games", "rdiv", "rthres"];

/**
 * Give the players' setup and statistics as plain data (the content of the original's
 * save games).
 * @param {object} game The game.
 * @returns {Array<object>} One object per player.
 */
export function exportPlayers(game) {
  return game.players.map((p) => Object.fromEntries(SAVED.map((key) => [key, p[key]])));
}

/**
 * Take the players' setup and statistics from saved data. Entries of a wrong type are
 * skipped, so a damaged save game changes nothing it should not.
 * @param {object} game The game.
 * @param {Array<object>} saved What `exportPlayers` returned.
 */
export function importPlayers(game, saved) {
  if (!Array.isArray(saved)) return;
  saved.slice(0, MAX_PLAYERS).forEach((entry, i) => {
    if (entry === null || typeof entry !== "object") return;
    const p = game.players[i];
    for (const key of SAVED) {
      if (typeof entry[key] === typeof p[key]) p[key] = entry[key];
    }
    p.color = Math.abs(Math.trunc(p.color)) % COLOURS;
    p.name = p.name.slice(0, 11);
    p.alive = false;
    p.escaped = false;
  });
}
