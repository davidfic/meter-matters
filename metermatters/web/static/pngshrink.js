/* Shrink a PNG the browser refuses to open.

   A flatbed scan at 2400 dpi is a 500-megapixel picture; no browser will
   decode one, and no server wants it uploaded. But a PNG is rows, inflated
   one after another, so it can be read a few rows at a time and averaged
   down into a small picture without the big one ever existing in memory.
   The browser's own inflater does the heavy part (DecompressionStream); this
   file does the PNG framing, the row unfiltering and the box average.

   Handles 8- and 16-bit greyscale, RGB, palette, with or without alpha, not
   interlaced - which is every scanner and phone. Alpha is ignored. */

const SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];
const CHANNELS = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 };

function u32(bytes, at) {
  return ((bytes[at] << 24) | (bytes[at + 1] << 16) | (bytes[at + 2] << 8) | bytes[at + 3]) >>> 0;
}

function paeth(a, b, c) {
  const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
  return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
}

/* Undo the row's filter in place, given the previous unfiltered row. */
function unfilter(type, row, prev, bpp) {
  const n = row.length;
  switch (type) {
    case 0: return;
    case 1: for (let i = bpp; i < n; i++) row[i] = (row[i] + row[i - bpp]) & 255; return;
    case 2: for (let i = 0; i < n; i++) row[i] = (row[i] + prev[i]) & 255; return;
    case 3:
      for (let i = 0; i < n; i++) row[i] = (row[i] + (((i >= bpp ? row[i - bpp] : 0) + prev[i]) >> 1)) & 255;
      return;
    case 4:
      for (let i = 0; i < n; i++) {
        const a = i >= bpp ? row[i - bpp] : 0, c = i >= bpp ? prev[i - bpp] : 0;
        row[i] = (row[i] + paeth(a, prev[i], c)) & 255;
      }
      return;
    default: throw new Error(`PNG row filter ${type} is not valid`);
  }
}

/* Read the chunks off the file as it streams, handing IDAT data to `sink`
   and returning the header once seen. Resolves when IEND arrives. */
async function readChunks(file, onHeader, sink) {
  const reader = file.stream().getReader();
  let buf = new Uint8Array(0), at = 0, header = null, palette = null;
  let need = 8, state = "signature", chunkType = "", chunkLeft = 0;

  const more = async () => {
    const { value, done } = await reader.read();
    if (done) return false;
    const rest = buf.subarray(at);
    const joined = new Uint8Array(rest.length + value.length);
    joined.set(rest); joined.set(value, rest.length);
    buf = joined; at = 0;
    return true;
  };

  for (;;) {
    if (state === "signature") {
      while (buf.length - at < 8) if (!(await more())) throw new Error("not a PNG");
      for (let i = 0; i < 8; i++) if (buf[at + i] !== SIGNATURE[i]) throw new Error("not a PNG");
      at += 8; state = "chunk-head";
    } else if (state === "chunk-head") {
      while (buf.length - at < 8) if (!(await more())) throw new Error("PNG ended early");
      chunkLeft = u32(buf, at);
      chunkType = String.fromCharCode(buf[at + 4], buf[at + 5], buf[at + 6], buf[at + 7]);
      at += 8;
      if (chunkType === "IEND") return;
      if (chunkType === "IHDR" || chunkType === "PLTE") {
        while (buf.length - at < chunkLeft) if (!(await more())) throw new Error("PNG ended early");
        const data = buf.subarray(at, at + chunkLeft);
        if (chunkType === "IHDR") {
          header = { width: u32(data, 0), height: u32(data, 4), depth: data[8], colour: data[9],
                     interlace: data[12] };
          if (!(header.depth === 8 || header.depth === 16)) throw new Error(`${header.depth}-bit PNGs aren't handled`);
          if (!(header.colour in CHANNELS)) throw new Error("that PNG's colour type isn't handled");
          if (header.interlace) throw new Error("interlaced PNGs aren't handled");
        } else {
          palette = new Uint8Array(data);
        }
        at += chunkLeft;
        if (header && (header.colour !== 3 || palette)) { onHeader(header, palette); }
        state = "chunk-crc";
      } else {
        state = chunkType === "IDAT" ? "idat" : "skip";
      }
    } else if (state === "idat" || state === "skip") {
      if (chunkLeft === 0) { state = "chunk-crc"; continue; }
      if (buf.length - at === 0 && !(await more())) throw new Error("PNG ended early");
      const take = Math.min(chunkLeft, buf.length - at);
      if (state === "idat") await sink(buf.slice(at, at + take));
      at += take; chunkLeft -= take;
    } else if (state === "chunk-crc") {
      while (buf.length - at < 4) if (!(await more())) throw new Error("PNG ended early");
      at += 4; state = "chunk-head";
    }
  }
}

/* The shrunk picture as a canvas, `maxSide` pixels on its longer side. */
export async function shrinkPng(file, maxSide, onProgress) {
  const inflate = new DecompressionStream("deflate");
  const writer = inflate.writable.getWriter();
  const reader = inflate.readable.getReader();

  let header = null, palette = null, ready;
  const headerSeen = new Promise((ok) => { ready = ok; });
  const produce = readChunks(file, (h, p) => { header = h; palette = p; ready(); },
                             (bytes) => writer.write(bytes))
    .then(() => writer.close(), (err) => { writer.abort(err); throw err; });

  await headerSeen;
  const { width, height, depth, colour } = header;
  const channels = CHANNELS[colour];
  const bytesPerSample = depth / 8;
  const bpp = channels * bytesPerSample;
  const rowBytes = width * bpp;
  const scale = Math.min(1, maxSide / Math.max(width, height));
  const outW = Math.max(1, Math.round(width * scale)), outH = Math.max(1, Math.round(height * scale));
  const xmap = new Uint32Array(width);
  for (let x = 0; x < width; x++) xmap[x] = Math.min(outW - 1, Math.floor(x * outW / width));
  const out = new Uint8ClampedArray(outW * outH * 4);
  const acc = new Float64Array(outW * 3);
  const cnt = new Float64Array(outW);

  let prev = new Uint8Array(rowBytes), row = new Uint8Array(rowBytes);
  let pending = new Uint8Array(0), y = 0, ty = 0;

  const flush = () => {
    for (let tx = 0; tx < outW; tx++) {
      const n = cnt[tx] || 1, o = (ty * outW + tx) * 4;
      out[o] = acc[tx * 3] / n; out[o + 1] = acc[tx * 3 + 1] / n; out[o + 2] = acc[tx * 3 + 2] / n;
      out[o + 3] = 255;
    }
    acc.fill(0); cnt.fill(0);
  };

  const addRow = () => {
    // Which output row this source row belongs to; flush when it moves on.
    const target = Math.min(outH - 1, Math.floor(y * outH / height));
    if (target !== ty) { flush(); ty = target; }
    if (colour === 3) {
      for (let x = 0, i = 0; x < width; x++, i += bpp) {
        const p = row[i] * 3, tx = xmap[x] * 3;
        acc[tx] += palette[p]; acc[tx + 1] += palette[p + 1]; acc[tx + 2] += palette[p + 2]; cnt[xmap[x]]++;
      }
    } else if (channels <= 2) {
      for (let x = 0, i = 0; x < width; x++, i += bpp) {
        const v = row[i], tx = xmap[x] * 3;
        acc[tx] += v; acc[tx + 1] += v; acc[tx + 2] += v; cnt[xmap[x]]++;
      }
    } else {
      const g = bytesPerSample, b2 = 2 * bytesPerSample;
      for (let x = 0, i = 0; x < width; x++, i += bpp) {
        const tx = xmap[x] * 3;
        acc[tx] += row[i]; acc[tx + 1] += row[i + g]; acc[tx + 2] += row[i + b2]; cnt[xmap[x]]++;
      }
    }
    y++;
    if (onProgress && (y & 255) === 0) onProgress(y / height);
  };

  const consume = (async () => {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      let data = value;
      if (pending.length) {
        const joined = new Uint8Array(pending.length + value.length);
        joined.set(pending); joined.set(value, pending.length);
        data = joined; pending = new Uint8Array(0);
      }
      let at = 0;
      while (data.length - at >= rowBytes + 1 && y < height) {
        const filter = data[at];
        row.set(data.subarray(at + 1, at + 1 + rowBytes));
        unfilter(filter, row, prev, bpp);
        addRow();
        [prev, row] = [row, prev];
        at += rowBytes + 1;
      }
      pending = data.slice(at);
    }
  })();

  await Promise.all([produce, consume]);
  if (y < height) throw new Error("the PNG ended before its last row");
  flush();

  const canvas = document.createElement("canvas");
  canvas.width = outW; canvas.height = outH;
  canvas.getContext("2d").putImageData(new ImageData(out, outW, outH), 0, 0);
  return canvas;
}
