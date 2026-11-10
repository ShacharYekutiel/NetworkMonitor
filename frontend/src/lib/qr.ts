// [3] Minimal QR code encoder (ISO/IEC 18004): byte mode, error correction level M,
// versions 1–10 (≤ 213 bytes — enough for a kiosk URL). Pure; used for the "Show URL once" dialog.

// Per version (1–10), level M: [total codewords, ec codewords per block, [blocks group1, data cw], [blocks group2, data cw]]
const M_TABLE: [number, number, [number, number], [number, number]][] = [
  [26, 10, [1, 16], [0, 0]],
  [44, 16, [1, 28], [0, 0]],
  [70, 26, [1, 44], [0, 0]],
  [100, 18, [2, 32], [0, 0]],
  [134, 24, [2, 43], [0, 0]],
  [172, 16, [4, 27], [0, 0]],
  [196, 18, [4, 31], [0, 0]],
  [242, 22, [2, 38], [2, 39]],
  [292, 22, [3, 36], [2, 37]],
  [346, 26, [4, 43], [1, 44]],
];
const ALIGN: number[][] = [
  [],
  [6, 18],
  [6, 22],
  [6, 26],
  [6, 30],
  [6, 34],
  [6, 22, 38],
  [6, 24, 42],
  [6, 26, 46],
  [6, 28, 50],
];

// GF(256) with polynomial 0x11d
const EXP = new Array<number>(512);
const LOG = new Array<number>(256);
(() => {
  let x = 1;
  for (let i = 0; i < 255; i++) {
    EXP[i] = x;
    LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
})();
const mul = (a: number, b: number) => (a && b ? EXP[LOG[a] + LOG[b]] : 0);

function rsGenerator(n: number): number[] {
  let g = [1];
  for (let i = 0; i < n; i++) {
    const next = new Array<number>(g.length + 1).fill(0);
    for (let j = 0; j < g.length; j++) {
      next[j] ^= g[j];
      next[j + 1] ^= mul(g[j], EXP[i]);
    }
    g = next;
  }
  return g;
}

function rsRemainder(data: number[], n: number): number[] {
  const gen = rsGenerator(n);
  const res = data.concat(new Array<number>(n).fill(0));
  for (let i = 0; i < data.length; i++) {
    const c = res[i];
    if (!c) continue;
    for (let j = 0; j < gen.length; j++) res[i + j] ^= mul(gen[j], c);
  }
  return res.slice(data.length);
}

function utf8(s: string): number[] {
  return Array.from(new TextEncoder().encode(s));
}

function dataCapacity(v: number): number {
  const [, , g1, g2] = M_TABLE[v - 1];
  return g1[0] * g1[1] + g2[0] * g2[1];
}

/** Smallest version (1–10) whose level-M capacity fits `len` bytes, else null. */
export function qrVersionFor(len: number): number | null {
  for (let v = 1; v <= 10; v++) {
    const countBits = v < 10 ? 8 : 16;
    if (4 + countBits + len * 8 <= dataCapacity(v) * 8) return v;
  }
  return null;
}

function codewords(bytes: number[], v: number): number[] {
  const cap = dataCapacity(v);
  const bits: number[] = [];
  const put = (val: number, n: number) => {
    for (let i = n - 1; i >= 0; i--) bits.push((val >>> i) & 1);
  };
  put(0b0100, 4);
  put(bytes.length, v < 10 ? 8 : 16);
  for (const b of bytes) put(b, 8);
  put(0, Math.min(4, cap * 8 - bits.length));
  while (bits.length % 8) bits.push(0);
  const data: number[] = [];
  for (let i = 0; i < bits.length; i += 8)
    data.push(bits.slice(i, i + 8).reduce((a, b) => (a << 1) | b, 0));
  for (let pad = 0xec; data.length < cap; pad = pad === 0xec ? 0x11 : 0xec) data.push(pad);
  // blocks + interleave
  const [, ec, g1, g2] = M_TABLE[v - 1];
  const blocks: number[][] = [];
  let off = 0;
  for (const [count, size] of [g1, g2])
    for (let i = 0; i < count; i++) {
      blocks.push(data.slice(off, off + size));
      off += size;
    }
  const ecBlocks = blocks.map((b) => rsRemainder(b, ec));
  const out: number[] = [];
  const maxLen = Math.max(...blocks.map((b) => b.length));
  for (let i = 0; i < maxLen; i++) for (const b of blocks) if (i < b.length) out.push(b[i]);
  for (let i = 0; i < ec; i++) for (const b of ecBlocks) out.push(b[i]);
  return out;
}

type Grid = { size: number; m: (boolean | null)[][]; fn: boolean[][] };

function newGrid(v: number): Grid {
  const size = 17 + 4 * v;
  return {
    size,
    m: Array.from({ length: size }, () => new Array<boolean | null>(size).fill(null)),
    fn: Array.from({ length: size }, () => new Array<boolean>(size).fill(false)),
  };
}

function setFn(g: Grid, r: number, c: number, val: boolean) {
  g.m[r][c] = val;
  g.fn[r][c] = true;
}

function placeFunctionPatterns(g: Grid, v: number) {
  const n = g.size;
  const finder = (r0: number, c0: number) => {
    for (let r = -1; r <= 7; r++)
      for (let c = -1; c <= 7; c++) {
        const rr = r0 + r;
        const cc = c0 + c;
        if (rr < 0 || cc < 0 || rr >= n || cc >= n) continue;
        const on =
          r >= 0 &&
          r <= 6 &&
          c >= 0 &&
          c <= 6 &&
          (r === 0 || r === 6 || c === 0 || c === 6 || (r >= 2 && r <= 4 && c >= 2 && c <= 4));
        setFn(g, rr, cc, on);
      }
  };
  finder(0, 0);
  finder(0, n - 7);
  finder(n - 7, 0);
  for (let i = 8; i < n - 8; i++) {
    setFn(g, 6, i, i % 2 === 0);
    setFn(g, i, 6, i % 2 === 0);
  }
  const pos = ALIGN[v - 1];
  for (const r of pos)
    for (const c of pos) {
      if (
        (r === 6 && c === 6) ||
        (r === 6 && c === pos[pos.length - 1]) ||
        (r === pos[pos.length - 1] && c === 6)
      )
        continue;
      for (let dr = -2; dr <= 2; dr++)
        for (let dc = -2; dc <= 2; dc++)
          setFn(g, r + dr, c + dc, Math.max(Math.abs(dr), Math.abs(dc)) !== 1);
    }
  // reserve format areas + dark module
  for (let i = 0; i < 9; i++) {
    if (!g.fn[8][i]) setFn(g, 8, i, false);
    if (!g.fn[i][8]) setFn(g, i, 8, false);
  }
  for (let i = 0; i < 8; i++) {
    setFn(g, 8, n - 1 - i, false);
    setFn(g, n - 1 - i, 8, false);
  }
  setFn(g, n - 8, 8, true);
  if (v >= 7) {
    const bits = versionBits(v);
    for (let i = 0; i < 18; i++) {
      const bit = ((bits >>> i) & 1) === 1;
      const a = Math.floor(i / 3);
      const b = (i % 3) + n - 11;
      setFn(g, a, b, bit);
      setFn(g, b, a, bit);
    }
  }
}

function versionBits(v: number): number {
  let rem = v;
  for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
  return (v << 12) | rem;
}

function formatBits(mask: number): number {
  const data = (0b00 << 3) | mask; // level M = 00
  let rem = data;
  for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
  return ((data << 10) | rem) ^ 0x5412;
}

function placeFormat(g: Grid, mask: number) {
  const bits = formatBits(mask);
  const n = g.size;
  const bit = (i: number) => ((bits >>> i) & 1) === 1;
  // first copy (around the top-left finder)
  for (let i = 0; i <= 5; i++) g.m[i][8] = bit(i);
  g.m[7][8] = bit(6);
  g.m[8][8] = bit(7);
  g.m[8][7] = bit(8);
  for (let i = 9; i < 15; i++) g.m[8][14 - i] = bit(i);
  // second copy (top-right / bottom-left)
  for (let i = 0; i < 8; i++) g.m[8][n - 1 - i] = bit(i);
  for (let i = 8; i < 15; i++) g.m[n - 15 + i][8] = bit(i);
  g.m[n - 8][8] = true;
}

function placeData(g: Grid, cw: number[]) {
  const n = g.size;
  let i = 0;
  const total = cw.length * 8;
  for (let right = n - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vert = 0; vert < n; vert++)
      for (let j = 0; j < 2; j++) {
        const c = right - j;
        const upward = ((right + 1) & 2) === 0;
        const r = upward ? n - 1 - vert : vert;
        if (g.fn[r][c]) continue;
        g.m[r][c] = i < total ? ((cw[i >>> 3] >>> (7 - (i & 7))) & 1) === 1 : false;
        i++;
      }
  }
}

const MASKS: ((r: number, c: number) => boolean)[] = [
  (r, c) => (r + c) % 2 === 0,
  (r) => r % 2 === 0,
  (_r, c) => c % 3 === 0,
  (r, c) => (r + c) % 3 === 0,
  (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0,
  (r, c) => ((r * c) % 2) + ((r * c) % 3) === 0,
  (r, c) => (((r * c) % 2) + ((r * c) % 3)) % 2 === 0,
  (r, c) => (((r + c) % 2) + ((r * c) % 3)) % 2 === 0,
];

function applyMask(g: Grid, mask: number) {
  for (let r = 0; r < g.size; r++)
    for (let c = 0; c < g.size; c++) if (!g.fn[r][c] && MASKS[mask](r, c)) g.m[r][c] = !g.m[r][c];
}

function penalty(m: boolean[][]): number {
  const n = m.length;
  let p = 0;
  const runs = (get: (i: number, j: number) => boolean) => {
    for (let i = 0; i < n; i++) {
      let run = 1;
      for (let j = 1; j < n; j++) {
        if (get(i, j) === get(i, j - 1)) {
          run++;
          if (run === 5) p += 3;
          else if (run > 5) p += 1;
        } else run = 1;
      }
    }
  };
  runs((i, j) => m[i][j]);
  runs((i, j) => m[j][i]);
  for (let r = 0; r < n - 1; r++)
    for (let c = 0; c < n - 1; c++) {
      const v = m[r][c];
      if (v === m[r][c + 1] && v === m[r + 1][c] && v === m[r + 1][c + 1]) p += 3;
    }
  const pat = [true, false, true, true, true, false, true];
  const check = (get: (k: number) => boolean, len: number) => {
    for (let k = 0; k + 7 <= len; k++) {
      if (!pat.every((x, i) => get(k + i) === x)) continue;
      const before = k >= 4 && [1, 2, 3, 4].every((d) => !get(k - d));
      const after = k + 11 <= len && [7, 8, 9, 10].every((d) => !get(k + d));
      if (before || after) p += 40;
    }
  };
  for (let i = 0; i < n; i++) {
    check((k) => m[i][k], n);
    check((k) => m[k][i], n);
  }
  const dark = m.flat().filter(Boolean).length;
  p += Math.floor(Math.abs((dark * 20) / (n * n) - 10)) * 10;
  return p;
}

/** The module matrix (true = dark) of `text`, or null when it is too long; `mask` forces a mask (tests). */
export function qrMatrix(text: string, mask?: number): boolean[][] | null {
  const bytes = utf8(text);
  const v = qrVersionFor(bytes.length);
  if (v === null) return null;
  const cw = codewords(bytes, v);
  let best: boolean[][] | null = null;
  let bestScore = Infinity;
  for (let k = 0; k < 8; k++) {
    if (mask !== undefined && k !== mask) continue;
    const g = newGrid(v);
    placeFunctionPatterns(g, v);
    placeData(g, cw);
    applyMask(g, k);
    placeFormat(g, k);
    const m = g.m.map((row) => row.map((x) => x === true));
    const s = mask !== undefined ? 0 : penalty(m);
    if (s < bestScore) {
      bestScore = s;
      best = m;
    }
  }
  return best;
}

/** SVG path data ("M x y h1 v1 h-1 z" per dark module) with a 4-module quiet zone. */
export function qrPath(m: boolean[][]): { d: string; size: number } {
  const q = 4;
  let d = '';
  m.forEach((row, r) =>
    row.forEach((on, c) => {
      if (on) d += `M${c + q} ${r + q}h1v1h-1z`;
    }),
  );
  return { d, size: m.length + 2 * q };
}

/** Internals exposed for known-answer tests. */
export const qrInternals = { rsRemainder, formatBits, versionBits };
