// The water of MetaTron's main menu, re-implemented from the WaterEffect class of
// classes.hpp: a height field in two pages over a still picture.
//
//   each frame:  rain    every 0 to 14 frames a disc of radius 25 is set to height 1000
//                calc    new[i] = (sum of the 8 neighbours in old) / 4 - new[i], then damped
//                        by 1/32: waves spread and fade
//                draw    out[i] = picture[i + (|slope x| / 8) + width * (|slope y| / 8)]:
//                        the picture is looked up further right and down where the water
//                        is steep, which bends it like a refraction

const DROPSIZE = 25;
const DROPSTRENGTH = 1000;
const DENSITY = 5;

export class Water {
  /**
   * @param {Uint32Array} picture The still picture, `width * height` pixels.
   * @param {number} width Width of the picture and of the height field.
   * @param {number} height Height of both.
   * @param {() => number} [random] Source of numbers in [0, 1) for the rain.
   */
  constructor(picture, width, height, random = Math.random) {
    this.picture = picture;
    this.width = width;
    this.height = height;
    this.random = random;
    this.pages = [new Int32Array(width * height), new Int32Array(width * height)];
    this.page = 0;
    this.rainIn = 8;
  }

  /**
   * Set a disc of the current page to a height (the original's HeightBlob).
   * @param {number} x Centre.
   * @param {number} y Centre.
   * @param {number} radius Radius in pixels.
   * @param {number} level The height to set.
   */
  blob(x, y, radius, level) {
    const field = this.pages[this.page];
    const top = Math.max(1, y - radius);
    const bottom = Math.min(this.height - 1, y + radius);
    const left = Math.max(1, x - radius);
    const right = Math.min(this.width - 1, x + radius);
    for (let cy = top; cy < bottom; cy++) {
      for (let cx = left; cx < right; cx++) {
        if ((cx - x) * (cx - x) + (cy - y) * (cy - y) < radius * radius) field[cy * this.width + cx] = level;
      }
    }
  }

  /**
   * Advance the water by one frame and write the bent picture.
   * @param {Uint32Array} out Where the frame goes, `width * height` pixels.
   */
  frame(out) {
    const { width, height, picture } = this;
    if (this.rainIn-- === 0) {
      this.blob(Math.floor(this.random() * width), Math.floor(this.random() * height), DROPSIZE, DROPSTRENGTH);
      this.rainIn = Math.floor(this.random() * 15);
    }
    this.page ^= 1;
    const now = this.pages[this.page];
    const old = this.pages[this.page ^ 1];
    for (let y = 1; y < height - 1; y++) {
      for (let i = y * width + 1, end = i + width - 2; i < end; i++) {
        const level =
          ((old[i + width] + old[i - width] + old[i + 1] + old[i - 1] + old[i - width - 1] + old[i - width + 1] + old[i + width - 1] + old[i + width + 1]) >> 2) - now[i];
        now[i] = level - (level >> DENSITY);
      }
    }
    const last = width * height - 1;
    for (let y = 1; y < height - 1; y++) {
      for (let i = y * width + 1, end = i + width - 2; i < end; i++) {
        const dx = Math.abs(now[i] - now[i + 1]);
        const dy = Math.abs(now[i] - now[i + width]);
        const from = i + width * (dy >> 3) + (dx >> 3);
        out[i] = from < last ? picture[from] : 0xff000000;
      }
    }
  }
}
