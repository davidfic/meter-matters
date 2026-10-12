/* Small SVG chart library built to the data-viz mark specs.
   No CDN, no dependencies - the dashboard works offline.

   Shared specs enforced here:
     line       2px, round cap/join
     marker     r >= 4, with a 2px surface ring
     area fill  ~10% opacity wash
     bars       <= 24px thick, 4px rounded data-end, square at the baseline,
                2px surface gap between neighbours
     grid/axes  solid 1px hairlines, recessive
*/

const NS = "http://www.w3.org/2000/svg";

let clipSeq = 0;

const BAR_MAX = 24;
const BAR_RADIUS = 4;
const GAP = 2;       // the surface gap that separates touching marks
const RING = 2;      // the surface ring on markers

const money = (n, cents = true) =>
  "$" + Number(n).toLocaleString("en-US", {
    minimumFractionDigits: cents ? 2 : 0,
    maximumFractionDigits: cents ? 2 : 0,
  });

const signedMoney = (n) => (n > 0 ? "+" : n < 0 ? "−" : "") + money(Math.abs(n));

/* Labels can come out of uploaded files. Anything that isn't ours is escaped. */
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* Tooltip body: a title, then rows of { color?, label, value }. Rows are keyed
   with a short stroke of the series colour, not a box. */
function tipBody(title, rows) {
  return `<div class="t-title">${esc(title)}</div>` + rows.map((r) =>
    `<div class="t-row"><span>${r.color ? `<span class="t-key" style="background:${r.color}"></span>` : ""}`
    + `${esc(r.label)}</span><b>${esc(r.value)}</b></div>`).join("");
}

function el(name, attrs = {}, parent = null) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  if (parent) parent.appendChild(node);
  return node;
}

/* Nice round axis ticks - 1/2/2.5/5 x 10^n. */
function niceTicks(min, max, count = 4) {
  if (min === max) { min = Math.min(0, min); max = max || 1; }
  const raw = (max - min) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(Math.abs(raw) || 1)));
  const norm = raw / mag;
  const step = (norm >= 5 ? 10 : norm >= 2.5 ? 5 : norm >= 2 ? 2.5 : norm >= 1 ? 2 : 1) * mag;
  const start = Math.floor(min / step) * step;
  const out = [];
  for (let v = start; v <= max + step * 0.5; v += step) out.push(Number(v.toFixed(10)));
  return out;
}

/* Bar/column path: rounded at the data end, square at the baseline. */
function columnPath(x, w, yBase, yVal) {
  const up = yVal <= yBase;
  const h = Math.abs(yBase - yVal);
  const r = Math.max(0, Math.min(BAR_RADIUS, w / 2, h));
  if (h < 0.5) return `M${x},${yBase} L${x + w},${yBase}`;
  return up
    ? `M${x},${yBase} L${x},${yVal + r} Q${x},${yVal} ${x + r},${yVal}
       L${x + w - r},${yVal} Q${x + w},${yVal} ${x + w},${yVal + r} L${x + w},${yBase} Z`
    : `M${x},${yBase} L${x},${yVal - r} Q${x},${yVal} ${x + r},${yVal}
       L${x + w - r},${yVal} Q${x + w},${yVal} ${x + w},${yVal - r} L${x + w},${yBase} Z`;
}

/* ------------------------------------------------------------------ base */

class Chart {
  constructor(host, opts = {}) {
    this.host = host;
    this.opts = opts;
    this.margin = Object.assign({ top: 26, right: 20, bottom: 30, left: 52 }, opts.margin);

    host.classList.add("chart-wrap");
    host.innerHTML = "";
    this.svg = el("svg", { role: "img" }, host);
    this.tip = document.createElement("div");
    this.tip.className = "tooltip";
    host.appendChild(this.tip);

    this._onResize = () => this.render();
    this.ro = new ResizeObserver(() => this.render());
    this.ro.observe(host);
  }

  size() {
    const w = Math.max(240, this.host.clientWidth || 320);
    const h = this.opts.height || 240;
    this.w = w;
    this.h = h;
    this.iw = w - this.margin.left - this.margin.right;
    this.ih = h - this.margin.top - this.margin.bottom;
    this.svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    this.svg.setAttribute("width", w);
    this.svg.setAttribute("height", h);
    this.svg.innerHTML = "";
    this.g = el("g", { transform: `translate(${this.margin.left},${this.margin.top})` }, this.svg);
  }

  /* Hairline horizontal grid + y tick labels. */
  grid(ticks, fmt = (v) => money(v, false)) {
    for (const t of ticks) {
      const y = this.yScale(t);
      if (y < -1 || y > this.ih + 1) continue;
      el("line", {
        x1: 0, x2: this.iw, y1: y, y2: y,
        stroke: "var(--gridline)", "stroke-width": 1,
        "shape-rendering": "crispEdges",
      }, this.g);
      el("text", { x: -10, y: y + 4, class: "tick y" }, this.g).textContent = fmt(t);
    }
  }

  xLabels(items, xFor, everyHint) {
    // Thin the labels out until they stop colliding.
    const every = everyHint || Math.max(1, Math.ceil((items.length * 58) / Math.max(this.iw, 1)));
    const last = items.length - 1;
    const picked = [];
    for (let i = 0; i <= last; i += every) picked.push(i);
    if (picked[picked.length - 1] !== last) {
      // The final label always shows - but drop its neighbour rather than
      // letting the two overlap into one unreadable smear.
      if (last - picked[picked.length - 1] < every) picked.pop();
      picked.push(last);
    }
    for (const i of picked) {
      el("text", {
        x: xFor(i), y: this.ih + 20, class: "tick x",
      }, this.g).textContent = items[i];
    }
  }

  showTip(x, y, html) {
    this.tip.innerHTML = html;
    this.tip.style.left = `${this.margin.left + x}px`;
    this.tip.style.top = `${this.margin.top + y}px`;
    this.tip.classList.add("on");
  }

  hideTip() { this.tip.classList.remove("on"); }

  destroy() { this.ro.disconnect(); }
}

/* ------------------------------------------------- line / area over time */

export class LineChart extends Chart {
  constructor(host, opts) {
    super(host, Object.assign({ height: 260 }, opts));
    this.render();
  }

  render() {
    const data = this.opts.data || [];
    if (!data.length) return;
    this.size();

    const values = data.map((d) => d.value);
    // Not every line is money - a usage chart writes kWh on the same geometry.
    const fmt = this.opts.format || ((v) => money(v));
    const tickFmt = this.opts.tickFormat || ((v) => money(v, false));
    const ref = this.opts.refValue;
    const lo = Math.min(...values, ref ?? Infinity);
    const hi = Math.max(...values, ref ?? -Infinity);
    const pad = (hi - lo) * 0.18 || Math.abs(hi) * 0.1 || 1;
    // Bills never go below zero, so a low floor reads better than a zero
    // baseline. Anything that CAN go negative - cumulative savings, deltas -
    // must keep its true range, or every point lands outside the plot.
    const allPositive = lo >= 0 && (ref === undefined || ref === null || ref >= 0);
    const min = allPositive ? Math.max(0, lo - pad) : lo - pad;
    const max = hi + pad;

    this.yScale = (v) => this.ih - ((v - min) / (max - min)) * this.ih;
    const step = data.length > 1 ? this.iw / (data.length - 1) : 0;
    const xFor = (i) => (data.length > 1 ? i * step : this.iw / 2);

    this.grid(niceTicks(min, max, 4), tickFmt);

    // Optional reference line (chart chrome, not a series - so no legend entry).
    if (ref !== undefined && ref !== null) {
      const y = this.yScale(ref);
      el("line", {
        x1: 0, x2: this.iw, y1: y, y2: y,
        stroke: "var(--axis)", "stroke-width": 1, "shape-rendering": "crispEdges",
      }, this.g);
      const label = el("text", { x: this.iw, y: y - 7, class: "ref-label", "text-anchor": "end" }, this.g);
      label.textContent = `${this.opts.refLabel || "average"} ${fmt(ref)}`;
    }

    const pts = data.map((d, i) => [xFor(i), this.yScale(d.value)]);
    const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x},${y}`).join(" ");

    // The SVG deliberately overflows so edge labels aren't clipped, which means
    // out-of-range geometry would paint over the rest of the page. Clipping the
    // fill and the line to the plot box keeps any such bug inside its own card.
    const clipId = `plot-clip-${++clipSeq}`;
    const defs = el("defs", {}, this.g);
    const clip = el("clipPath", { id: clipId }, defs);
    el("rect", { x: 0, y: 0, width: this.iw, height: this.ih }, clip);
    const plot = el("g", { "clip-path": `url(#${clipId})` }, this.g);

    const base = Math.min(Math.max(this.yScale(Math.max(min, 0)), 0), this.ih);

    // Area wash at ~10% - never a saturated block.
    el("path", {
      d: `${line} L${pts.at(-1)[0]},${base} L${pts[0][0]},${base} Z`,
      fill: this.opts.color || "var(--series-1)",
      "fill-opacity": 0.1,
    }, plot);

    el("path", {
      d: line, fill: "none",
      stroke: this.opts.color || "var(--series-1)",
      "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
    }, plot);

    // A zero rule, when the series crosses it - otherwise "negative" is invisible.
    if (min < 0 && max > 0) {
      const zy = this.yScale(0);
      el("line", {
        x1: 0, x2: this.iw, y1: zy, y2: zy,
        stroke: "var(--axis)", "stroke-width": 1, "shape-rendering": "crispEdges",
      }, this.g);
    }

    // Direct labels, sparingly: the endpoint and the extreme only - and only
    // where they don't collide. A label stacked on the reference line or on
    // the endpoint reads as noise, so the less important one is dropped.
    const lastIdx = data.length - 1;
    const maxIdx = values.indexOf(Math.max(...values));
    const taken = [];
    if (ref !== undefined && ref !== null) {
      // The reference label is right-anchored at x = iw, so it extends left.
      taken.push({ x: this.iw - 125, y: this.yScale(ref) - 19, w: 125, h: 17 });
    }

    const order = maxIdx === lastIdx ? [lastIdx] : [lastIdx, maxIdx];
    for (const i of order) {
      const [x, y] = pts[i];
      const flip = y < 34;
      const lx = Math.min(Math.max(x, 22), this.iw - 22);
      const ly = flip ? y + 20 : y - 14;
      const box = { x: lx - 32, y: ly - 11, w: 64, h: 15 };
      const clash = taken.some((t) =>
        Math.abs((t.x + t.w / 2) - (box.x + box.w / 2)) < (t.w + box.w) / 2 &&
        Math.abs((t.y + t.h / 2) - (box.y + box.h / 2)) < (t.h + box.h) / 2);

      el("circle", {
        cx: x, cy: y, r: 4.5,
        fill: this.opts.color || "var(--series-1)",
        stroke: "var(--surface-1)", "stroke-width": RING,
      }, this.g);
      if (clash) continue;   // the dot still marks it; the tooltip still reads it

      taken.push(box);
      const label = el("text", {
        x: lx, y: ly,
        class: i === lastIdx ? "mark-label" : "mark-label muted",
      }, this.g);
      label.textContent = fmt(data[i].value);
    }

    this.xLabels(data.map((d) => d.label), xFor);

    // Crosshair + tooltip across the whole plot.
    const cross = el("line", {
      y1: 0, y2: this.ih, stroke: "var(--axis)", "stroke-width": 1,
      opacity: 0, "shape-rendering": "crispEdges",
    }, this.g);
    const hover = el("circle", {
      r: 4.5, fill: this.opts.color || "var(--series-1)",
      stroke: "var(--surface-1)", "stroke-width": RING, opacity: 0,
    }, this.g);

    const hit = el("rect", {
      x: 0, y: 0, width: this.iw, height: this.ih, fill: "transparent",
    }, this.g);

    const pick = (clientX) => {
      const box = this.svg.getBoundingClientRect();
      const scale = this.w / box.width;
      const x = (clientX - box.left) * scale - this.margin.left;
      return Math.max(0, Math.min(data.length - 1, Math.round(step ? x / step : 0)));
    };

    const move = (ev) => {
      const i = pick(ev.clientX ?? ev.touches?.[0]?.clientX ?? 0);
      const d = data[i];
      const [x, y] = pts[i];
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("opacity", 1);
      hover.setAttribute("cx", x); hover.setAttribute("cy", y); hover.setAttribute("opacity", 1);
      this.showTip(x, y, this.opts.tooltip ? this.opts.tooltip(d, i) :
        `<div class="t-title">${d.label}</div><div class="t-row"><span>Value</span><b>${fmt(d.value)}</b></div>`);
    };

    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", () => {
      cross.setAttribute("opacity", 0);
      hover.setAttribute("opacity", 0);
      this.hideTip();
    });
  }
}

/* ------------------------------------------- diverging month-over-month */

export class DeltaBars extends Chart {
  constructor(host, opts) {
    super(host, Object.assign({ height: 230 }, opts));
    this.render();
  }

  render() {
    const data = this.opts.data || [];
    if (!data.length) return;
    this.size();

    const values = data.map((d) => d.value);
    const span = Math.max(Math.abs(Math.min(...values, 0)), Math.abs(Math.max(...values, 0))) || 1;
    const max = span * 1.22;
    this.yScale = (v) => this.ih / 2 - (v / max) * (this.ih / 2);

    this.grid(niceTicks(-max, max, 4), (v) => signedMoney(v).replace(".00", ""));

    const band = this.iw / data.length;
    const w = Math.min(BAR_MAX, band - GAP);
    const zero = this.yScale(0);

    data.forEach((d, i) => {
      const x = i * band + (band - w) / 2;
      const y = this.yScale(d.value);
      // Down is good, up is bad - this is genuine status, and it always
      // ships with the sign and the month label, never colour alone.
      const fill = d.value < 0 ? "var(--good)" : d.value > 0 ? "var(--critical)" : "var(--axis)";
      el("path", { d: columnPath(x, w, zero, y), fill }, this.g);

      const hit = el("rect", {
        x: i * band, y: 0, width: band, height: this.ih, fill: "transparent",
      }, this.g);
      const show = () => this.showTip(i * band + band / 2, Math.min(y, zero),
        `<div class="t-title">${d.label}</div>
         <div class="t-row"><span>Bill</span><b>${money(d.amount)}</b></div>
         <div class="t-row"><span>vs prior</span><b>${signedMoney(d.value)}</b></div>
         <div class="t-row"><span>Change</span><b>${d.pct > 0 ? "+" : d.pct < 0 ? "\u2212" : ""}${Math.abs(d.pct)}%</b></div>`);
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("pointermove", show);
      hit.addEventListener("pointerleave", () => this.hideTip());
    });

    // The zero rule sits above the bars so it reads as the baseline.
    el("line", {
      x1: 0, x2: this.iw, y1: zero, y2: zero,
      stroke: "var(--axis)", "stroke-width": 1, "shape-rendering": "crispEdges",
    }, this.g);

    this.xLabels(data.map((d) => d.label), (i) => i * band + band / 2);
  }
}

/* ------------------------------------------------- single-series columns */

export class Columns extends Chart {
  constructor(host, opts) {
    super(host, Object.assign({ height: 230 }, opts));
    this.render();
  }

  render() {
    const data = this.opts.data || [];
    if (!data.length) return;
    this.size();

    const max = Math.max(...data.map((d) => d.value)) * 1.16 || 1;
    this.yScale = (v) => this.ih - (v / max) * this.ih;
    this.grid(niceTicks(0, max, 4), this.opts.tickFormat);

    const band = this.iw / data.length;
    const w = Math.min(BAR_MAX, band - GAP);
    const zero = this.yScale(0);
    const peak = Math.max(...data.map((d) => d.value));

    data.forEach((d, i) => {
      const x = i * band + (band - w) / 2;
      const y = this.yScale(d.value);
      el("path", {
        d: columnPath(x, w, zero, y),
        fill: this.opts.color || "var(--series-1)",
        "fill-opacity": d.dim ? 0.35 : 1,
      }, this.g);

      // Only the peak is direct-labelled; the axis and tooltip carry the rest.
      if (d.value === peak) {
        el("text", { x: i * band + band / 2, y: y - 9, class: "mark-label" }, this.g)
          .textContent = (this.opts.format || ((v) => money(v, false)))(d.value);
      }

      const hit = el("rect", { x: i * band, y: 0, width: band, height: this.ih, fill: "transparent" }, this.g);
      const show = () => this.showTip(i * band + band / 2, y,
        this.opts.tooltip ? this.opts.tooltip(d) :
        `<div class="t-title">${d.label}</div><div class="t-row"><span>Average</span><b>${money(d.value)}</b></div>`);
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("pointermove", show);
      hit.addEventListener("pointerleave", () => this.hideTip());
    });

    el("line", {
      x1: 0, x2: this.iw, y1: zero, y2: zero,
      stroke: "var(--axis)", "stroke-width": 1, "shape-rendering": "crispEdges",
    }, this.g);

    this.xLabels(data.map((d) => d.label), (i) => i * band + band / 2);
  }
}

/* ------------------------------------- two series of signed contributions */

export class GroupedDeltaBars extends Chart {
  /* Each slot carries two bars that can each go above or below the baseline.
     Used for "how much of this change was usage, how much was the rate" - the
     two are identity, not status, so they take categorical slots 1 and 2. */
  constructor(host, opts) {
    super(host, Object.assign({ height: 240 }, opts));
    this.render();
  }

  render() {
    const data = this.opts.data || [];
    const series = this.opts.series || [];
    if (!data.length || !series.length) return;
    this.size();

    // Dollars unless told otherwise: the same geometry draws therms per day.
    const fmt = this.opts.format || signedMoney;
    const tickFmt = this.opts.tickFormat || ((v) => signedMoney(v).replace(".00", ""));
    const all = data.flatMap((d) => series.map((s) => d[s.key])).filter((v) => v != null);
    const span = Math.max(...all.map(Math.abs), this.opts.floor ?? 1);
    const max = span * 1.22;
    this.yScale = (v) => this.ih / 2 - (v / max) * (this.ih / 2);

    this.grid(niceTicks(-max, max, 4), tickFmt);

    const band = this.iw / data.length;
    // Two bars plus the 2px surface gap between them, inside one band.
    const w = Math.max(3, Math.min(BAR_MAX, (band - GAP * 3) / series.length));
    const groupW = w * series.length + GAP * (series.length - 1);
    const zero = this.yScale(0);

    data.forEach((d, i) => {
      series.forEach((s, j) => {
        const v = d[s.key];
        if (v == null) return;
        const x = i * band + (band - groupW) / 2 + j * (w + GAP);
        el("path", { d: columnPath(x, w, zero, this.yScale(v)), fill: s.color }, this.g);
      });

      const hit = el("rect", { x: i * band, y: 0, width: band, height: this.ih, fill: "transparent" }, this.g);
      const rows = series
        .map((s) => `<div class="t-row"><span>${s.label}</span><b>${
          d[s.key] == null ? "\u2014" : fmt(d[s.key])}</b></div>`)
        .join("");
      const show = () => this.showTip(i * band + band / 2, Math.min(zero, this.ih / 2),
        `<div class="t-title">${esc(d.title || d.label)}</div>${rows}` +
        (d.total == null ? "" :
          `<div class="t-row" style="margin-top:4px"><span>${this.opts.totalLabel || "Net"}</span><b>${fmt(d.total)}</b></div>`) +
        (this.opts.extra ? this.opts.extra(d) : ""));
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("pointermove", show);
      hit.addEventListener("pointerleave", () => this.hideTip());
    });

    el("line", {
      x1: 0, x2: this.iw, y1: zero, y2: zero,
      stroke: "var(--axis)", "stroke-width": 1, "shape-rendering": "crispEdges",
    }, this.g);

    this.xLabels(data.map((d) => d.label), (i) => i * band + band / 2);
  }
}

/* ------------------------------------------------- stacked part-to-whole */

export class StackedColumns extends Chart {
  /* Segments are separated by a 2px gap in the surface colour, never a stroke. */
  constructor(host, opts) {
    super(host, Object.assign({ height: 240 }, opts));
    this.render();
  }

  render() {
    const data = this.opts.data || [];
    const series = this.opts.series || [];
    if (!data.length || !series.length) return;
    this.size();

    const totals = data.map((d) => series.reduce((sum, s) => sum + (d[s.key] || 0), 0));
    const max = Math.max(...totals) * 1.14 || 1;
    this.yScale = (v) => this.ih - (v / max) * this.ih;
    this.grid(niceTicks(0, max, 4), this.opts.tickFormat || ((v) => money(v, false)));

    const band = this.iw / data.length;
    const w = Math.max(1, Math.min(BAR_MAX, band - GAP));
    const zero = this.yScale(0);

    data.forEach((d, i) => {
      const x = i * band + (band - w) / 2;
      let cursor = 0;
      series.forEach((s) => {
        const v = d[s.key] || 0;
        if (v <= 0) return;
        const yTop = this.yScale(cursor + v);
        const yBottom = this.yScale(cursor);
        // The gap lives inside the segment so the bar total stays honest.
        const height = Math.max(0, yBottom - yTop - (cursor > 0 ? GAP : 0));
        el("rect", {
          x, y: yTop, width: w, height,
          rx: height > 4 ? 1 : 0,
          fill: s.color,
        }, this.g);
        cursor += v;
      });

      const hit = el("rect", { x: i * band, y: 0, width: band, height: this.ih, fill: "transparent" }, this.g);
      const fmt = this.opts.format || money;
      const rows = series.map((s) =>
        `<div class="t-row"><span>${s.label}</span><b>${fmt(d[s.key] || 0)}</b></div>`).join("");
      const show = () => this.showTip(i * band + band / 2, this.yScale(totals[i]),
        `<div class="t-title">${d.label}</div>${rows}` +
        `<div class="t-row" style="margin-top:4px"><span>Total</span><b>${fmt(totals[i])}</b></div>`);
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("pointermove", show);
      hit.addEventListener("pointerleave", () => this.hideTip());
    });

    el("line", {
      x1: 0, x2: this.iw, y1: zero, y2: zero,
      stroke: "var(--axis)", "stroke-width": 1, "shape-rendering": "crispEdges",
    }, this.g);
    this.xLabels(data.map((d) => d.label), (i) => i * band + band / 2);
  }
}

/* ------------------------------------------------ usage against weather */

export class Scatter extends Chart {
  /* Dots for readings, a line for the rule they follow. Recent readings take
     the series colour and the rest recede - the point is where the latest
     ones sit against the line, not the cloud. */
  constructor(host, opts) {
    super(host, Object.assign({ height: 300 }, opts));
    this.margin.bottom = 46;
    this.render();
  }

  render() {
    const pts = this.opts.data || [];
    if (!pts.length) return;
    this.size();

    const xMax = Math.max(...pts.map((p) => p.x)) * 1.06 || 1;
    const yMax = Math.max(...pts.map((p) => p.y)) * 1.14 || 1;
    const xs = (v) => (v / xMax) * this.iw;
    this.yScale = (v) => this.ih - (v / yMax) * this.ih;
    const fmt = this.opts.format || ((v) => String(v));
    const n = (v) => Number(v).toLocaleString("en-US", { maximumFractionDigits: v % 1 ? 1 : 0 });

    this.grid(niceTicks(0, yMax, 4), n);
    el("line", { x1: 0, x2: this.iw, y1: this.ih, y2: this.ih, stroke: "var(--axis)",
                 "stroke-width": 1, "shape-rendering": "crispEdges" }, this.g);
    for (const t of niceTicks(0, xMax, 5)) {
      if (xs(t) > this.iw + 1) continue;
      el("text", { x: xs(t), y: this.ih + 20, class: "tick x" }, this.g).textContent = n(t);
    }
    el("text", { x: this.iw / 2, y: this.ih + 40, class: "axis-title", "text-anchor": "middle" }, this.g)
      .textContent = this.opts.xTitle || "";
    el("text", { x: -this.margin.left + 4, y: -12, class: "axis-title" }, this.g)
      .textContent = this.opts.yTitle || "";

    const [a, b] = this.opts.line;
    el("line", { x1: xs(0), y1: this.yScale(Math.max(a, 0)), x2: xs(xMax), y2: this.yScale(a + b * xMax),
                 stroke: "var(--series-3)", "stroke-width": 2, "stroke-linecap": "round" }, this.g);

    for (const p of [...pts].sort((p, q) => Number(p.recent) - Number(q.recent))) {
      el("circle", { cx: xs(p.x), cy: this.yScale(p.y), r: 4.5,
                     fill: p.recent ? "var(--series-1)" : "var(--text-muted)",
                     "fill-opacity": p.recent ? 1 : 0.55,
                     stroke: "var(--surface-1)", "stroke-width": RING }, this.g);
    }

    // One direct label: the recent reading furthest from the line.
    const recent = pts.filter((p) => p.recent);
    if (recent.length) {
      const odd = recent.reduce((m, p) => (Math.abs(p.y - p.fit) > Math.abs(m.y - m.fit) ? p : m));
      const gap = odd.y - odd.fit;
      const text = [odd.label.split(",")[0], `${n(Math.abs(gap))} ${gap < 0 ? "below" : "above"} the line`];
      const spot = this.room(text, xs(odd.x), this.yScale(odd.y),
        pts.map((p) => [xs(p.x), this.yScale(p.y)]),
        (x) => this.yScale(a + b * (x / this.iw) * xMax), gap < 0);
      if (spot) {
        el("line", { x1: xs(odd.x), y1: this.yScale(odd.y) + (spot.below ? 7 : -7),
                     x2: Math.min(xs(odd.x), spot.x + spot.w - 8), y2: spot.below ? spot.y : spot.y + spot.h,
                     stroke: "var(--axis)", "stroke-width": 1 }, this.g);
        text.forEach((line, k) => {
          el("text", { x: spot.x + spot.w - 4, y: spot.y + 12 + k * 14, class: "note-label",
                       "text-anchor": "end" }, this.g).textContent = line;
        });
      }
    }

    // Nearest point wins, so the pointer only has to be close.
    const ring = el("circle", { r: 8, fill: "none", stroke: "var(--text-primary)",
                                "stroke-width": 1.5, opacity: 0 }, this.g);
    const hit = el("rect", { x: 0, y: 0, width: this.iw, height: this.ih, fill: "transparent" }, this.g);
    hit.addEventListener("pointermove", (ev) => {
      const box = this.svg.getBoundingClientRect();
      const px = (ev.clientX - box.left) * (this.w / box.width) - this.margin.left;
      const py = (ev.clientY - box.top) * (this.h / box.height) - this.margin.top;
      const p = pts.reduce((m, q) =>
        (Math.hypot(xs(q.x) - px, this.yScale(q.y) - py) < Math.hypot(xs(m.x) - px, this.yScale(m.y) - py) ? q : m));
      ring.setAttribute("cx", xs(p.x)); ring.setAttribute("cy", this.yScale(p.y));
      ring.setAttribute("opacity", 1);
      this.showTip(xs(p.x), this.yScale(p.y) - 6, tipBody(p.label, [
        { color: p.recent ? "var(--series-1)" : "var(--text-muted)", label: "Used", value: fmt(p.y) },
        { color: "var(--series-3)", label: "Weather predicts", value: fmt(p.fit) },
        { label: "Average outside", value: `${Math.round(p.temp)}\u00b0F` },
      ]));
    });
    hit.addEventListener("pointerleave", () => { ring.setAttribute("opacity", 0); this.hideTip(); });
  }

  /* Somewhere near (px, py) that a label fits without sitting on a dot or on
     the line. Tries the side the point is already on first. */
  room(lines, px, py, dots, lineY, below) {
    const w = Math.max(...lines.map((l) => l.length)) * 6.1 + 10, h = 14 * lines.length + 4;
    const clear = (box) =>
      box.x >= 0 && box.x + box.w <= this.iw && box.y >= 0 && box.y + box.h <= this.ih
      && !dots.some(([cx, cy]) => cx > box.x - 9 && cx < box.x + box.w + 9
                                && cy > box.y - 9 && cy < box.y + box.h + 9)
      && [0, 0.25, 0.5, 0.75, 1].every((t) => {
        const y = lineY(box.x + box.w * t);
        return y < box.y - 6 || y > box.y + box.h + 6;
      });
    for (const side of below ? [1, -1] : [-1, 1]) {
      for (const away of [14, 24, 34, 46, 58, 72]) {
        for (const x of [px - w + 4, px - w * 0.75, px - w / 2, px - w * 0.25, px]) {
          const box = { x, y: side > 0 ? py + away : py - away - h, w, h, below: side > 0 };
          if (clear(box)) return box;
        }
      }
    }
    return null;
  }
}

/* -------------------------------------------- lines, with levels and bands */

export class Lines extends Chart {
  /* One or more series over time. `bands` wash a stretch worth pointing at;
     `levels` draw the flat reference a stretch sat at, labelled along the top
     where the data never reaches; `marks` direct-label the few points named. */
  constructor(host, opts) {
    super(host, Object.assign({ height: 280 }, opts));
    this.render();
  }

  render() {
    const data = this.opts.data || [], series = this.opts.series || [];
    if (!data.length || !series.length) return;
    this.size();
    const fmt = this.opts.format || ((v) => String(v));
    const levels = this.opts.levels || [];

    const values = data.flatMap((d) => series.map((s) => d[s.key])).filter((v) => v != null)
      .concat(levels.map((l) => l.value));
    const hi = Math.max(...values), lo = Math.min(...values);
    const pad = (hi - lo) * 0.18 || Math.abs(hi) * 0.1 || 1;
    const min = lo < 0 ? lo - pad : (this.opts.zero ? 0 : Math.max(0, lo - pad));
    const max = hi + pad + (levels.length ? (hi - min) * 0.2 : 0);
    this.yScale = (v) => this.ih - ((v - min) / (max - min)) * this.ih;
    const step = data.length > 1 ? this.iw / (data.length - 1) : 0;
    const xFor = (i) => i * step;
    const base = Math.min(Math.max(this.yScale(Math.max(min, 0)), 0), this.ih);

    this.grid(niceTicks(min, max, 4), this.opts.tickFormat || ((v) => v.toLocaleString("en-US")));

    for (const band of this.opts.bands || []) {
      const x0 = Math.max(0, xFor(band.from) - step / 2);
      const x1 = Math.min(this.iw, xFor(band.to) + step / 2);
      el("rect", { x: x0, y: 0, width: Math.max(0, x1 - x0), height: this.ih,
                   fill: "var(--series-1)", "fill-opacity": 0.07 }, this.g);
    }

    for (const level of levels) {
      const x0 = Math.max(0, xFor(level.from) - step / 2);
      const x1 = Math.min(this.iw, xFor(level.to) + step / 2);
      const y = this.yScale(level.value);
      el("line", { x1: x0, x2: x1, y1: y, y2: y, stroke: "var(--text-muted)", "stroke-width": 1.5 }, this.g);
      // The full label where the stretch is wide enough, the bare number
      // where it isn't, nothing where even that would collide.
      const fits = (text) => text.length * 6.1 + 8 <= x1 - x0;
      const text = [level.label, level.short].find((t) => t && fits(t));
      if (text) {
        el("text", { x: (x0 + x1) / 2, y: 14, class: "note-label", "text-anchor": "middle" }, this.g)
          .textContent = text;
      }
    }

    const clipId = `plot-clip-${++clipSeq}`;
    const clip = el("clipPath", { id: clipId }, el("defs", {}, this.g));
    el("rect", { x: 0, y: 0, width: this.iw, height: this.ih }, clip);
    const plot = el("g", { "clip-path": `url(#${clipId})` }, this.g);

    for (const s of series) {
      const pts = data.map((d, i) => [xFor(i), this.yScale(d[s.key])]);
      const path = pts.map(([x, y], i) => `${i ? "L" : "M"}${x},${y}`).join(" ");
      if (s.area) {
        el("path", { d: `${path} L${pts.at(-1)[0]},${base} L${pts[0][0]},${base} Z`,
                     fill: s.color, "fill-opacity": 0.1 }, plot);
      }
      el("path", { d: path, fill: "none", stroke: s.color, "stroke-width": 2,
                   "stroke-linejoin": "round", "stroke-linecap": "round" }, plot);
    }

    if (min < 0 && max > 0) {
      el("line", { x1: 0, x2: this.iw, y1: this.yScale(0), y2: this.yScale(0), stroke: "var(--axis)",
                   "stroke-width": 1, "shape-rendering": "crispEdges" }, this.g);
    }

    for (const mark of this.opts.marks || []) {
      const s = series.find((x) => x.key === mark.key);
      const x = xFor(mark.index), y = this.yScale(data[mark.index][mark.key]);
      el("circle", { cx: x, cy: y, r: 4.5, fill: s.color,
                     stroke: "var(--surface-1)", "stroke-width": RING }, this.g);
      el("text", { x: Math.min(Math.max(x + (mark.dx || 0), 30), this.iw - 30),
                   y: y + (mark.below ? 22 : -12), class: "mark-label" }, this.g).textContent = mark.label;
    }

    this.xLabels(data.map((d) => d.label), xFor);

    const cross = el("line", { y1: 0, y2: this.ih, stroke: "var(--axis)", "stroke-width": 1,
                               opacity: 0, "shape-rendering": "crispEdges" }, this.g);
    const dots = series.map((s) => el("circle", { r: 4.5, fill: s.color, opacity: 0,
                                                  stroke: "var(--surface-1)", "stroke-width": RING }, this.g));
    const hit = el("rect", { x: 0, y: 0, width: this.iw, height: this.ih, fill: "transparent" }, this.g);
    const move = (ev) => {
      const box = this.svg.getBoundingClientRect();
      const x = (ev.clientX - box.left) * (this.w / box.width) - this.margin.left;
      const i = Math.max(0, Math.min(data.length - 1, Math.round(step ? x / step : 0)));
      const d = data[i];
      cross.setAttribute("x1", xFor(i)); cross.setAttribute("x2", xFor(i)); cross.setAttribute("opacity", 1);
      series.forEach((s, k) => {
        dots[k].setAttribute("cx", xFor(i));
        dots[k].setAttribute("cy", this.yScale(d[s.key]));
        dots[k].setAttribute("opacity", 1);
      });
      this.showTip(xFor(i), Math.min(...series.map((s) => this.yScale(d[s.key]))),
        tipBody(d.title || d.label, [
          ...series.map((s) => ({ color: s.color, label: s.label, value: fmt(d[s.key]) })),
          ...(this.opts.rows ? this.opts.rows(d) : []),
        ]));
    };
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", () => {
      cross.setAttribute("opacity", 0);
      dots.forEach((dot) => dot.setAttribute("opacity", 0));
      this.hideTip();
    });
  }
}

export { money, signedMoney, esc };
