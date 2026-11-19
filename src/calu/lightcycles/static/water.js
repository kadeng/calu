// The water of MetaTron's main menu, re-implemented from the WaterEffect class of
// classes.hpp: a height field in two pages over a still picture.
//
// The original ran the field at the picture's size, one step per frame. Here the field has
// half the picture's width and height (so a wave is twice as long on screen) and drops twice
// as high, and it takes one step every STEP_FRAMES frames (so the waves travel more slowly);
// the frames in between show the two newest pages blended, so the motion stays smooth at 60
// frames per second.
//
//   each step:   rain    every 0 to 14 steps a disc of radius 25 field cells is set to
//                        height 2000
//                calc    new[i] = (sum of the 8 neighbours in old) / 4 - new[i], then damped
//                        by 1/32: waves spread and fade
//   each frame:  blend   h = old + (new - old) * phase / STEP_FRAMES, scaled up to the
//                        picture's size (bilinear)
//                draw    out[i] = picture[i + (|slope x| / 8) + width * (|slope y| / 8)]:
//                        the picture is looked up further right and down where the water
//                        is steep, which bends it like a refraction

const DROPSIZE = 25;
const DROPSTRENGTH = 2000;
const DENSITY = 5;
const SCALE = 2;

/** Frames per step of the height field: the waves travel 1 field cell (2 pixels) per step. */
export const STEP_FRAMES = 4;

export class Water {
  /**
   * @param {Uint32Array} picture The still picture, `width * height` pixels.
   * @param {number} width Width of the picture.
   * @param {number} height Height of the picture.
   * @param {() => number} [random] Source of numbers in [0, 1) for the rain.
   */
  constructor(picture, width, height, random = Math.random) {
    this.picture = picture;
    this.width = width;
    this.height = height;
    this.random = random;
    this.fieldWidth = Math.ceil(width / SCALE) + 1;
    this.fieldHeight = Math.ceil(height / SCALE) + 1;
    const cells = this.fieldWidth * this.fieldHeight;
    this.pages = [new Int32Array(cells), new Int32Array(cells)];
    this.page = 0;
    this.rainIn = 8;
    this.phase = 0;
    this.heights = new Int32Array(width * height);
  }

  /**
   * Set a disc of the current page to a height (the original's HeightBlob).
   * @param {number} x Centre, in field cells.
   * @param {number} y Centre, in field cells.
   * @param {number} radius Radius in field cells.
   * @param {number} level The height to set.
   */
  blob(x, y, radius, level) {
    const field = this.pages[this.page];
    const w = this.fieldWidth;
    const top = Math.max(1, y - radius);
    const bottom = Math.min(this.fieldHeight - 1, y + radius);
    const left = Math.max(1, x - radius);
    const right = Math.min(w - 1, x + radius);
    for (let cy = top; cy < bottom; cy++) {
      for (let cx = left; cx < right; cx++) {
        if ((cx - x) * (cx - x) + (cy - y) * (cy - y) < radius * radius) field[cy * w + cx] = level;
      }
    }
  }

  /** Advance the height field by one step: rain, then the wave equation of the original. */
  step() {
    const w = this.fieldWidth;
    const h = this.fieldHeight;
    if (this.rainIn-- === 0) {
      this.blob(Math.floor(this.random() * w), Math.floor(this.random() * h), DROPSIZE, DROPSTRENGTH);
      this.rainIn = Math.floor(this.random() * 15);
    }
    this.page ^= 1;
    const now = this.pages[this.page];
    const old = this.pages[this.page ^ 1];
    for (let y = 1; y < h - 1; y++) {
      for (let i = y * w + 1, end = i + w - 2; i < end; i++) {
        const level =
          ((old[i + w] + old[i - w] + old[i + 1] + old[i - 1] + old[i - w - 1] + old[i - w + 1] + old[i + w - 1] + old[i + w + 1]) >> 2) - now[i];
        now[i] = level - (level >> DENSITY);
      }
    }
  }

  /**
   * Fill `heights` with the water at picture resolution: the two newest pages blended by the
   * phase between steps, scaled up bilinearly.
   */
  blend() {
    const { width, height, fieldWidth: w, heights } = this;
    const now = this.pages[this.page];
    const old = this.pages[this.page ^ 1];
    // 1/STEP_FRAMES just after a step, 1 just before the next: the blend ends on the newest
    // page, which the next step's blend starts from.
    const t = this.phase / STEP_FRAMES;
    for (let y = 0; y < height; y++) {
      const fy = y / SCALE;
      const y0 = Math.floor(fy);
      const ty = fy - y0;
      for (let x = 0; x < width; x++) {
        const fx = x / SCALE;
        const x0 = Math.floor(fx);
        const tx = fx - x0;
        const a = y0 * w + x0;
        const b = a + w;
        const top = old[a] + (old[a + 1] - old[a]) * tx;
        const bottom = old[b] + (old[b + 1] - old[b]) * tx;
        const topNew = now[a] + (now[a + 1] - now[a]) * tx;
        const bottomNew = now[b] + (now[b + 1] - now[b]) * tx;
        const before = top + (bottom - top) * ty;
        const after = topNew + (bottomNew - topNew) * ty;
        heights[y * width + x] = before + (after - before) * t;
      }
    }
  }

  /**
   * Advance the water by one frame (a step of the field every `STEP_FRAMES` frames) and write
   * the bent picture.
   * @param {Uint32Array} out Where the frame goes, `width * height` pixels.
   */
  frame(out) {
    const { width, height, picture, heights } = this;
    if (this.phase === 0) this.step();
    this.phase += 1;
    this.blend();
    if (this.phase === STEP_FRAMES) this.phase = 0;
    const last = width * height - 1;
    for (let y = 1; y < height - 1; y++) {
      for (let i = y * width + 1, end = i + width - 2; i < end; i++) {
        const dx = Math.abs(heights[i] - heights[i + 1]);
        const dy = Math.abs(heights[i] - heights[i + width]);
        const from = i + width * (dy >> 3) + (dx >> 3);
        out[i] = from < last ? picture[from] : 0xff000000;
      }
    }
  }
}
