// The page: keyboard and gamepads in, 60 frames per second of `flow.frame`, the canvas out.
//
//   keydown (not a repeat) --> flow.keyDown --> session
//   requestAnimationFrame --> as many flow.frame calls as 1/60 s steps have passed
//                         --> Renderer.draw (main menu: the water first)

import * as flow from "./flow.js";
import { drawTitle, Renderer } from "./render.js";
import * as sim from "./sim.js";
import { Water } from "./water.js";

const STEP = 1000 / 60;
const WATER_WIDTH = 640;
const WATER_HEIGHT = 480;

const canvas = document.getElementById("screen");
const status = document.getElementById("status");
const renderer = new Renderer(canvas);
const query = new URLSearchParams(location.search);

/**
 * Read a whole number from the page's address.
 * @param {string} name The parameter.
 * @param {number} low Smallest value accepted.
 * @param {number} high Largest value accepted.
 * @returns {number|null} The number, or null if the parameter is missing or out of range.
 */
function option(name, low, high) {
  const text = query.get(name);
  if (text === null || !/^\d+$/.test(text)) return null;
  const value = Number(text);
  return value >= low && value <= high ? value : null;
}

/**
 * Get the browser's storage for the save games.
 * @returns {Storage|null} `localStorage`, or null where the browser refuses it.
 */
function storage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

const seed = option("seed", 0, 0xffffffff) ?? (Date.now() & 0x7fffffff);
const session = flow.createSession({ seed, storage: storage() });
const people = option("players", 0, sim.MAX_PLAYERS);
const bots = option("bots", 0, sim.MAX_PLAYERS);
if (people !== null || bots !== null) {
  const count = people ?? 1;
  flow.choosePlayers(session, count, Math.min(bots ?? 0, sim.MAX_PLAYERS - count));
}
session.game.speed = option("speed", 1, 9) ?? session.game.speed;

// The title picture and the water over it. Both canvases exist only in memory.
const titleCanvas = document.createElement("canvas");
titleCanvas.width = WATER_WIDTH;
titleCanvas.height = WATER_HEIGHT;
const titleCtx = titleCanvas.getContext("2d", { willReadFrequently: true });
drawTitle(titleCtx, WATER_WIDTH, WATER_HEIGHT);
const picture = new Uint32Array(titleCtx.getImageData(0, 0, WATER_WIDTH, WATER_HEIGHT).data.buffer.slice(0));
const waterCanvas = document.createElement("canvas");
waterCanvas.width = WATER_WIDTH;
waterCanvas.height = WATER_HEIGHT;
const waterCtx = waterCanvas.getContext("2d");
const waterImage = waterCtx.createImageData(WATER_WIDTH, WATER_HEIGHT);
const waterPixels = new Uint32Array(waterImage.data.buffer);
const water = new Water(picture, WATER_WIDTH, WATER_HEIGHT);
const calm = window.matchMedia("(prefers-reduced-motion: reduce)");
let waterOn = true;

const held = new Set();
const padDown = new Map();
let realTime = true;
let last = performance.now();
let owed = 0;

/**
 * Draw the main menu's background: the water, or the still title where the user asked for
 * reduced motion or switched the water off.
 * @param {number} frames Frames of water to compute, 0 or more.
 */
function background(frames) {
  const ctx = renderer.ctx;
  if (!waterOn || calm.matches) {
    ctx.drawImage(titleCanvas, 0, 0, sim.WIDTH, sim.HEIGHT);
    return;
  }
  for (let i = 0; i < Math.max(1, Math.min(frames, 2)); i++) water.frame(waterPixels);
  waterCtx.putImageData(waterImage, 0, 0);
  ctx.drawImage(waterCanvas, 1, 1, WATER_WIDTH - 2, WATER_HEIGHT - 2, 0, 0, sim.WIDTH, sim.HEIGHT);
}

/**
 * Turn gamepad buttons into turns: the n-th connected gamepad steers the n-th active
 * person; left and right on the directional pad or the two shoulder buttons turn.
 */
function pollGamepads() {
  if (session.phase !== "play" || !navigator.getGamepads) return;
  const riders = session.game.players.map((p, i) => (p.active && p.human ? i : -1)).filter((i) => i >= 0);
  const pads = [...navigator.getGamepads()].filter((pad) => pad !== null && pad.connected);
  pads.slice(0, riders.length).forEach((pad, n) => {
    const now = [(pad.buttons[14]?.pressed || pad.buttons[4]?.pressed) ?? false, (pad.buttons[15]?.pressed || pad.buttons[5]?.pressed) ?? false];
    const before = padDown.get(pad.index) ?? [false, false];
    if (now[0] && !before[0]) session.pending.push([riders[n], "L"]);
    if (now[1] && !before[1]) session.pending.push([riders[n], "R"]);
    padDown.set(pad.index, now);
  });
}

/**
 * One animation frame: advance the session by the 1/60 s steps that have passed (at most
 * five, so a tab that was in the background does not race), then draw.
 * @param {number} now The time of the frame in milliseconds.
 */
function animate(now) {
  owed += Math.min(now - last, 5 * STEP);
  last = now;
  let frames = 0;
  while (owed >= STEP) {
    owed -= STEP;
    frames++;
    if (realTime) {
      pollGamepads();
      flow.frame(session);
    }
  }
  redraw(frames);
  requestAnimationFrame(animate);
}

/**
 * Draw the session and update the status line.
 * @param {number} [frames] Frames of water to compute behind the main menu.
 */
function redraw(frames = 0) {
  if (session.phase === "menu" || session.phase === "bye") background(frames);
  renderer.draw(session);
  const text = flow.describe(session);
  if (status.textContent !== text) status.textContent = text;
}

window.addEventListener("keydown", (event) => {
  // The game uses function keys, Alt, Space and the arrows: none of them may also scroll
  // the page, open a menu or reload. Combinations the browser reserves stay the browser's.
  if (!event.metaKey && event.code !== "F11" && event.code !== "F12") event.preventDefault();
  held.add(event.code);
  if (event.repeat) return;
  if (session.phase === "menu" && event.code === "KeyF") {
    document.documentElement.requestFullscreen?.().then(() => navigator.keyboard?.lock?.()).catch(() => {});
    return;
  }
  if (session.phase === "menu" && event.code === "KeyW") {
    waterOn = !waterOn;
    return;
  }
  flow.keyDown(session, event.code, held);
});
window.addEventListener("keyup", (event) => {
  event.preventDefault();
  held.delete(event.code);
});
window.addEventListener("blur", () => held.clear());
window.addEventListener("beforeunload", (event) => {
  // A browser shortcut hit by accident in a round (Ctrl+W with player 1's key held) asks first.
  if (session.phase === "play") event.preventDefault();
});

// For tests: the session, and frames advanced by the test instead of by the clock.
window.__lightcyclesTest = {
  session,
  /** @param {boolean} on False stops the clock from advancing the session. */
  setRealTime(on) {
    realTime = on;
  },
  /** @param {number} frames Frames to advance at once. */
  advance(frames) {
    for (let i = 0; i < frames; i++) flow.frame(session);
    redraw();
  },
};

requestAnimationFrame(animate);
