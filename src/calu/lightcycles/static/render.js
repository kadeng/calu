// Drawing: the field, the menus and the title picture on a 2D canvas of 800x600. Nothing
// here changes the game. The menus' texts and positions follow tron.cpp; the colours and
// the title picture are calu's own (the original's bitmap and palette are not used).

import { COLOURS, FRAME_VALUE, HEIGHT, WIDTH } from "./sim.js";

/** The players' colours, by colour index. */
export const PLAYER_COLOURS = ["#27c3f5", "#ffb454", "#ff5fa2", "#7ee787", "#b392f0", "#ff6b5e", "#e6edf3", "#f2e85c", "#3ddbb4", "#5b8cff"];
const FRAME_COLOUR = "#3e8fa8";
const BACKGROUND = "#05080d";
const TEXT = "#9ff3ff";
const ACCENT = "#ffb454";
const FONT = 'bold 16px "Courier New", Courier, monospace';
const LINE = 20;
export const CREDIT = "MetaTron for the browser — after MetaTron by Kai Londenberg";

/** The names the original shows for keys, by `KeyboardEvent.code`. */
const KEY_NAMES = {
  ControlLeft: "LCONTROL", ControlRight: "RCONTROL", AltLeft: "LALT", AltRight: "RALT",
  ShiftLeft: "LSHIFT", ShiftRight: "RSHIFT", CapsLock: "CAPITAL", ArrowLeft: "LEFT",
  ArrowRight: "RIGHT", ArrowUp: "UP", ArrowDown: "DOWN", PageDown: "NEXT", PageUp: "PRIOR",
  NumpadDecimal: "NUMPADDOT", NumpadAdd: "NUMPAD+", NumpadSubtract: "NUMPAD-",
  NumpadMultiply: "NUMPAD*", NumpadDivide: "DIVIDE", Enter: "RETURN", Period: "PERIOD",
};

/**
 * Name a key as the setup menu shows it.
 * @param {string} code The key as `KeyboardEvent.code`; "" while a key is being chosen.
 * @returns {string} E.g. "LCONTROL", "V", "NUMPAD0", "CHOOSE KEY".
 */
export function keyName(code) {
  if (code === "") return "CHOOSE KEY";
  if (KEY_NAMES[code]) return KEY_NAMES[code];
  return code.replace(/^Key|^Digit/, "").toUpperCase();
}

/**
 * Turn "#rrggbb" into the 32-bit value of one pixel of an `ImageData` (little endian).
 * @param {string} hex The colour.
 * @returns {number} The pixel value, opaque.
 */
function pixel(hex) {
  const n = parseInt(hex.slice(1), 16);
  return (0xff000000 | ((n & 0xff) << 16) | (n & 0xff00) | (n >> 16)) >>> 0;
}

const PALETTE = new Uint32Array(256).fill(pixel(BACKGROUND));
PLAYER_COLOURS.forEach((colour, i) => {
  PALETTE[i + 1] = pixel(colour);
});
PALETTE[FRAME_VALUE] = pixel(FRAME_COLOUR);

/**
 * Pad or cut a text to a width, as `%-11.11s` (left) and `%11.11s` (right) do.
 * @param {string|number} value The text.
 * @param {number} width The width in characters.
 * @param {boolean} [left] True puts the text at the left.
 * @returns {string} Exactly `width` characters.
 */
function fit(value, width, left = false) {
  const text = String(value).slice(0, width);
  return left ? text.padEnd(width) : text.padStart(width);
}

/**
 * Draw the title picture, which lies under the water: calu's ring and check, the title and
 * the credit, on a faint grid that makes the waves visible.
 * @param {CanvasRenderingContext2D} ctx The context of a canvas of any size.
 * @param {number} width Width of the picture.
 * @param {number} height Height of the picture.
 */
export function drawTitle(ctx, width, height) {
  const glow = ctx.createRadialGradient(width / 2, height * 0.42, 10, width / 2, height * 0.42, width * 0.7);
  glow.addColorStop(0, "#13283a");
  glow.addColorStop(1, BACKGROUND);
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = "rgba(39, 195, 245, 0.16)";
  ctx.lineWidth = 1;
  for (let x = 0.5; x < width; x += 32) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
  }
  for (let y = 0.5; y < height; y += 32) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }
  // calu's mark (extensions/vscode/media/logo.svg): three arcs, a closed ring, a check.
  const cx = width / 2;
  const cy = height * 0.3;
  const scale = (height * 0.36) / 256;
  const degrees = Math.PI / 180;
  ctx.save();
  ctx.translate(cx - 128 * scale, cy - 128 * scale);
  ctx.scale(scale, scale);
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.shadowColor = "#27c3f5";
  ctx.shadowBlur = 14;
  ctx.strokeStyle = "#5fdcfb";
  ctx.lineWidth = 13;
  for (const start of [-78, 42, 162]) {
    ctx.beginPath();
    ctx.arc(128, 128, 92, start * degrees, (start + 96) * degrees);
    ctx.stroke();
  }
  ctx.lineWidth = 4;
  ctx.globalAlpha = 0.75;
  ctx.beginPath();
  ctx.arc(128, 128, 62, 0, 2 * Math.PI);
  ctx.stroke();
  ctx.globalAlpha = 1;
  ctx.lineWidth = 15;
  ctx.beginPath();
  ctx.moveTo(99, 130);
  ctx.lineTo(121, 152);
  ctx.lineTo(160, 106);
  ctx.stroke();
  ctx.shadowBlur = 0;
  ctx.fillStyle = ACCENT;
  for (const [x, y] of [[128, 36], [207.7, 174], [48.3, 174]]) {
    ctx.beginPath();
    ctx.arc(x, y, 5, 0, 2 * Math.PI);
    ctx.fill();
  }
  ctx.restore();
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.shadowColor = "#27c3f5";
  ctx.shadowBlur = 18;
  ctx.fillStyle = TEXT;
  ctx.font = `bold ${Math.round(height * 0.13)}px "Courier New", Courier, monospace`;
  ctx.fillText("MetaTron", cx, height * 0.59);
  ctx.shadowBlur = 0;
  ctx.fillStyle = ACCENT;
  ctx.font = `bold ${Math.round(height * 0.031)}px "Courier New", Courier, monospace`;
  ctx.fillText(CREDIT, cx, height * 0.68);
}

export class Renderer {
  /**
   * @param {HTMLCanvasElement} canvas The visible canvas, 800x600.
   */
  constructor(canvas) {
    this.ctx = canvas.getContext("2d");
    this.image = this.ctx.createImageData(WIDTH, HEIGHT);
    this.pixels = new Uint32Array(this.image.data.buffer);
  }

  /**
   * Write lines of text, the first at `(x, y)`, each 20 pixels below the one before.
   * @param {number} x Left edge.
   * @param {number} y Top of the first line.
   * @param {string[]} lines The lines.
   * @param {string} [colour] The text colour.
   */
  text(x, y, lines, colour = TEXT) {
    const ctx = this.ctx;
    ctx.font = FONT;
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillStyle = colour;
    lines.forEach((line, i) => ctx.fillText(line, x, y + i * LINE));
  }

  /** Fill the canvas with the background colour. */
  clear() {
    this.ctx.fillStyle = BACKGROUND;
    this.ctx.fillRect(0, 0, WIDTH, HEIGHT);
  }

  /**
   * Draw the field from the game's buffer, and a dot on every cycle that rides (the
   * original draws no cycle, only its wall; the dot shows where a wall grows).
   * @param {object} game The game.
   */
  field(game) {
    const { buf } = game;
    const pixels = this.pixels;
    for (let i = 0; i < buf.length; i++) pixels[i] = PALETTE[buf[i]];
    this.ctx.putImageData(this.image, 0, 0);
    for (const p of game.players) {
      if (!p.alive) continue;
      this.ctx.fillStyle = "#ffffff";
      this.ctx.fillRect(p.pos.x - 1, p.pos.y - 1, 3, 3);
    }
  }

  /**
   * Draw the names at the start positions, as the original shows them for a second before
   * a round, and each person's two keys with them.
   * @param {object} game The game.
   */
  names(game) {
    const ctx = this.ctx;
    ctx.font = FONT;
    ctx.textBaseline = "top";
    game.players.forEach((p, i) => {
      if (!p.active) return;
      const keys = p.human ? ` [${keyName(p.lkey)} ${keyName(p.rkey)}]` : " [KI]";
      ctx.fillStyle = PLAYER_COLOURS[p.color];
      ctx.textAlign = i % 2 === 0 ? "right" : "left";
      ctx.fillText(`${p.name}${keys}`, p.pos.x, p.pos.y - 22);
    });
    ctx.textAlign = "left";
  }

  /**
   * Draw a rectangle outline where the field's frame is, as the player setup does in the
   * player's colour.
   * @param {string} colour The colour.
   */
  border(colour) {
    this.ctx.strokeStyle = colour;
    this.ctx.lineWidth = 1;
    this.ctx.strokeRect(30.5, 30.5, 740, 540);
  }

  /**
   * Draw the session: the screen of its phase. The main menu's background (the water or
   * the still title) is drawn by the caller before this.
   * @param {object} session The session.
   */
  draw(session) {
    const game = session.game;
    switch (session.phase) {
      case "menu":
        this.text(300, 450, ["SPACE        -    START GAME", "RETURN       -    SETUP", "S            -    SCORES", "ESC          -    ESCAPE"]);
        this.text(300, 540, ["F fullscreen   W water on/off"], "#5c7f8f");
        break;
      case "bye":
        this.text(210, 450, ["MetaTron has ended. Close this tab;", "the server stops with Ctrl-C in the terminal.", "", "Any key returns to the menu."]);
        break;
      case "intro":
        this.field(game);
        this.names(game);
        break;
      case "play":
        this.field(game);
        if (session.paused) this.text(370, 6, ["PAUSED"], ACCENT);
        break;
      case "stats":
        this.clear();
        this.stats(game);
        break;
      case "setup":
        this.clear();
        this.setup(session);
        break;
      default:
        this.clear();
        this.player(session);
    }
  }

  /**
   * Draw the statistics screen.
   * @param {object} game The game.
   */
  stats(game) {
    this.text(300, 100, ["  Player Statistics"], ACCENT);
    this.text(10, 150, ["Name        Survived  Escaped   Score  Victories  Games  Escapes   Total Score"]);
    let line = 1;
    for (const p of game.players) {
      if (!p.active) continue;
      const survived = p.escaped || p.alive ? "YES" : "NO";
      const row = `${fit(p.name, 11, true)}      ${fit(survived, 3)}      ${fit(p.escaped ? "YES" : "NO", 3)}  ${fit(p.points, 6)}  ${fit(p.tsurvived, 9)}  ${fit(p.games, 5)}  ${fit(p.tescaped, 7)}  ${fit(p.tpoints, 12)}`;
      this.text(10, 150 + line++ * LINE, [row], PLAYER_COLOURS[p.color]);
    }
    this.text(300, 530, ["Press Space to continue"], ACCENT);
  }

  /**
   * Draw the setup menu with the table of the ten players.
   * @param {object} session The session.
   */
  setup(session) {
    const game = session.game;
    this.text(260, 100, ["            SETUP"], ACCENT);
    this.text(260, 150, [
      "ESC/RETURN    -    MAIN MENU",
      "F1-F10        -    SETUP PLAYER 1-10",
      `1-9           -    SPEED: ${game.speed}`,
      `UP/DOWN       -    Wait States ${game.waitStates}`,
      "LSHIFT F1-F9  -    LOAD SAVEGAME 1-9",
      "LCTRL  F1-F9  -    SAVE GAME 1-9",
      "LALT+RALT     -    RESET ALL SCORES",
    ]);
    if (session.notice) this.text(260, 300, [session.notice], ACCENT);
    this.text(10, 340, ["##  Player Name    Active?      Left Key     Right Key    Games   Points"]);
    game.players.forEach((p, i) => {
      const active = p.active ? (p.human ? "YES" : "KI") : "NO";
      const row = `${fit(i + 1, 2)}  ${fit(p.name, 11)}        ${fit(active, 3)} ${fit(keyName(p.lkey), 13)} ${fit(keyName(p.rkey), 13)}    ${fit(p.games, 5)}   ${fit(p.tpoints, 6)}`;
      this.text(10, 380 + i * LINE, [row], p.active ? PLAYER_COLOURS[p.color] : "#5c7f8f");
    });
  }

  /**
   * Draw the setup of one player, also while its name or one of its keys is being entered.
   * @param {object} session The session; `selected` is the player.
   */
  player(session) {
    const p = session.game.players[session.selected];
    const state = p.active ? (p.human ? "     ACTIVE" : "         KI") : "   INACTIVE";
    this.text(200, 100, ["Player Setup", `Setting up Player Nr. ${fit(session.selected + 1, 2)}`], ACCENT);
    if (session.phase === "name") this.text(50, 240, ["Modify Name"], ACCENT);
    this.text(200, 200, [
      `F1 Toggle - ${state}`,
      "F2        -    Reset Player",
      `F3        -    Player Name  : \`${p.name}\``,
      `F4        -    Left Key     : \`${keyName(p.lkey)}\``,
      `F5        -    Right Key    : \`${keyName(p.rkey)}\``,
      `F6        -    Color (${p.color + 1} of ${COLOURS})`,
      "F7        -    Reset Scores",
      "",
      "RETURN OK  -  ESCAPE CANCEL",
    ]);
    this.border(PLAYER_COLOURS[p.color]);
  }
}
