/* Usage against last year with the weather taken out, what changed, the
   carbon, and what specific changes would be worth.

   Loaded after the page has drawn itself: working this out may mean fetching
   temperatures, and nothing about that should hold up the bills. */

import { url } from "./base.js";
import { GroupedDeltaBars, Lines, Scatter, StackedColumns } from "./charts.js";
import { $, esc, table, tiles, fuelColor, fuelNoun } from "./ui.js";
import * as compare from "./compare.js";

const charts = {};
let latest = null;

const num = (v, d = 0) => Number(v).toLocaleString("en-US",
  { minimumFractionDigits: d, maximumFractionDigits: d });
const sign = (v) => (v > 0 ? "+" : v < 0 ? "−" : "");
const signed = (v, d = 0) => sign(Number(v.toFixed(d))) + num(Math.abs(v), d);
const money = (v) => "$" + num(v);
const signedMoney = (v) => sign(Math.round(v)) + "$" + num(Math.abs(v));
const pct = (v, d = 1) => signed(v, d) + "%";
const more = (v) => (v > 0 ? "more" : "less");
const tonnes = (kg) => (kg >= 1000 ? `${num(kg / 1000, 1)} tonnes` : `${num(kg)} kg`);
const thing = fuelNoun;
// Small quantities need the decimals; large ones are only cluttered by them.
const places = (unit) => (unit === "kWh" ? 1 : 2);


function draw(name, Kind, host, opts) {
  charts[name]?.destroy();
  charts[name] = new Kind(host, opts);
}

/* What stands in for the weather when there isn't any, in words. */
function weatherGap(data) {
  return {
    off: "Weather is switched off on this server, so changes can't be split into weather and you.",
    "no-location": "Add your ZIP code and changes can be split into the weather's part and yours.",
    "no-weather-for-fuel": "",
    unavailable: "The weather service couldn't be reached just now, so this is the change as billed.",
  }[data.weather_status] || "";
}

/* What the weather did during the newest bill, on its own: the one thing
   that can be said before there is anything to compare with. */
function ownWeather(block) {
  const w = block.latest_weather;
  if (!w) return "";
  if (block.weather_kind === "dryness") {
    if (w.rain == null) return "";
    return `${w.span}: ${num(w.rain, 1)} in of rain fell, and the garden called for about ${num(w.need, 1)} in of watering.`;
  }
  return w.temp == null ? "" : `${w.span}: averaged ${num(w.temp)}°F outside.`;
}

/* ------------------------------------------------------- the hero's box */

function renderBox(block) {
  const row = block.latest;
  const w = block.weather;
  if (!row) {
    $("compare-figure").textContent = "—";
    $("compare-copy").textContent = block.need || "Nothing to compare with yet.";
    $("compare-pips").innerHTML = "";
    $("compare-weather").textContent = ownWeather(block);
    return;
  }
  const adjusted = Boolean(w && w.matters && row.weather_known);
  const change = adjusted ? row.you : row.raw;
  const share = adjusted ? row.you_pct : row.raw_pct;
  const u = block.unit, d = places(u);

  // A percentage of next to nothing is noise, so quiet months get the amount.
  const same = Math.abs(change) < 10 ** -d / 2;
  $("compare-figure").textContent = same ? "Same"
    : (row.pct_ok && share != null ? pct(share, 0) : signed(change, d));
  const amount = `${num(Math.abs(change), d)} ${u} a day ${more(change)}`;
  $("compare-copy").innerHTML = same
    ? `<strong>The same ${esc(thing(block.fuel))}</strong> as the same weeks last year.`
    : `<strong>${esc(amount)}</strong> than the same weeks last year`
      + (adjusted ? ", once the weather is taken out." : ".");

  $("compare-pips").innerHTML = block.compare.map((r) =>
    `<span class="pip ${r.you < 0 ? "on" : ""}" title="${esc(r.span)}"></span>`).join("");

  const lower = block.compare.filter((r) => r.you < 0).length;
  const bits = [`Lower than last year on ${lower} of the last ${block.compare.length} bills`];
  if (block.weather_kind === "dryness" && row.rain_now != null && row.rain_then != null) {
    bits.push(`${num(row.rain_now, 1)} in of rain, against ${num(row.rain_then, 1)} in`);
  } else if (row.temp_now != null && row.temp_then != null) {
    bits.push(`${num(row.temp_now)}°F outside, against ${num(row.temp_then)}°F`);
  }
  $("compare-weather").textContent = bits.join(" · ");
}

/* ------------------------------------------------------------ the section */

function lede(block, data) {
  const t = block.totals, w = block.weather, what = thing(block.fuel);
  const span = t.periods === 12 ? "the year before" : `the ${t.periods} bills a year earlier`;
  const billed = `You used ${num(Math.abs(t.raw_pct), 1)}% ${more(t.raw)} ${what} than ${span}`;
  if (!w) return block.weather_relevant === false ? `${billed}.` : `${billed}. ${weatherGap(data)}`;
  if (!w.matters) {
    return `${billed}. The weather explains ${num(w.r2 * 100)}% of your ${what} use, `
      + `so there is nothing to take out: the change is yours.`;
  }
  const dry = w.kind === "dryness";
  const colder = dry
    ? (t.weather > 0 ? "it was drier, so the garden called for more" : "it was wetter, so the garden called for less")
    : (t.weather > 0 ? "it called for more" : "it called for less");
  return `As billed, you used ${num(Math.abs(t.raw_pct), 1)}% ${more(t.raw)} ${what} than ${span}. `
    + `But the weather was different, and ${colder}: that alone accounts for ${pct(t.weather_pct)}. `
    + `Take the weather out and you used ${num(Math.abs(t.you_pct), 1)}% ${more(t.you)}.`;
}

function renderTiles(block) {
  const t = block.totals, w = block.weather, u = block.unit;
  const items = [
    { label: `Used, last ${t.periods} bills`, value: `${num(t.now)} ${u}`,
      foot: `${pct(t.raw_pct)} on a year earlier, as billed` },
  ];
  if (w) {
    items.push(
      { label: "Down to the weather", color: "var(--series-3)", value: `${signed(t.weather)} ${u}`,
        foot: w.matters
          ? `${pct(t.weather_pct)}: what the weather alone would have done`
          : `The weather explains ${num(w.r2 * 100)}% of your ${thing(block.fuel)} use` },
      { label: "Down to you", color: "var(--series-1)", value: `${signed(t.you)} ${u}`,
        foot: `${pct(t.you_pct)} once the weather is taken out` },
    );
  } else {
    items.push({ label: "Lower than a year earlier", value: `${t.lower} of ${t.periods} bills`,
                 foot: "Counted on usage, not on cost" });
  }
  if (block.carbon && block.carbon.kg > 0) items.push({ label: `Carbon, last ${block.carbon.months} months`, value: tonnes(block.carbon.kg),
               foot: `From ${thing(block.fuel)} alone` });
  const sim = block.similar;
  if (sim && sim.status === "ok") {
    const more = sim.vs_median_pct > 0;
    items.push({ label: `Similar homes in ${sim.state}`,
      value: `${Math.abs(sim.vs_median_pct)}% ${more ? "more" : "less"}`,
      foot: `than the middle of ${sim.count} homes in ${sim.state} (${num(sim.median, 1)} ${u}/day). ` +
            `You use less than ${100 - sim.percentile}% of them.` });
  } else if (sim && sim.status === "too-few") {
    items.push({ label: `Similar homes in ${sim.state}`, value: `${sim.count} of ${sim.needed}`,
      foot: `Shows once ${sim.needed} homes in ${sim.state} are in. Yours is one of them${data.benchmark_shared ? "" : " - or would be, if sharing were on"}.` });
  }
  tiles($("insights-tiles"), items);
}

function renderCompare(block) {
  const u = block.unit, d = places(u), w = block.weather;
  const split = Boolean(w);
  $("compare-title").textContent = split && w.matters
    ? "Was it you, or was it the weather?" : "Against the same weeks last year";
  $("compare-note").textContent = split
    ? (w.matters
        ? `Each bill against the same weeks a year earlier, in ${u} per day. The two bars add up to the change you were billed for.`
        : `In ${u} per day. The weather's share is drawn; it is just too small to see.`)
    : `Each bill against the same weeks a year earlier, in ${u} per day.`;
  $("compare-legend-weather").hidden = !split;

  const series = [{ key: "you", label: "You", color: "var(--series-1)" }];
  if (split) series.push({ key: "weather", label: "The weather", color: "var(--series-3)" });
  const fmt = (v) => `${signed(v, d)} ${u}/day`;
  draw("compare", GroupedDeltaBars, $("chart-compare"), {
    height: 280, series, floor: 0.01,
    format: fmt, tickFormat: (v) => signed(v, Math.abs(v) < 3 && v % 1 ? 1 : 0),
    totalLabel: "As billed",
    data: block.compare.map((r) => ({ ...r, title: r.span, total: r.raw })),
    extra: (r) => (r.temp_now == null || r.temp_then == null ? "" :
      `<div class="t-row"><span>Outside</span><b>${num(r.temp_now)}°F, was ${num(r.temp_then)}°F</b></div>`),
  });
  $("compare-table").innerHTML = table([
    { head: "Bill", cell: (r) => r.span },
    { head: `${u}/day`, cell: (r) => num(r.now, 2) },
    { head: "Year earlier", cell: (r) => num(r.then, 2) },
    { head: "As billed", cell: (r) => signed(r.raw, 2) },
    ...(split ? [{ head: "The weather", cell: (r) => signed(r.weather, 2) },
                 { head: "You", cell: (r) => signed(r.you, 2) }] : []),
    { head: "Outside", cell: (r) => (r.temp_now == null ? "—"
        : `${num(r.temp_now)}°F` + (r.temp_then == null ? "" : ` (was ${num(r.temp_then)}°F)`)) },
  ], [...block.compare].reverse());
}

function renderSplit(block) {
  const dollars = block.totals.dollars, split = Boolean(block.weather);
  const parts = [
    { key: "you", name: "You", color: "var(--series-1)" },
    ...(split ? [{ key: "weather", name: "The weather", color: "var(--series-3)" }] : []),
    { key: "rate", name: "The rate", color: "var(--series-2)" },
  ];
  const scale = Math.max(...parts.map((p) => Math.abs(dollars[p.key])), 1) * 1.08;
  $("split3-note").textContent =
    `The last ${block.totals.periods} bills against a year earlier, in dollars. `
    + `The parts add up to the change exactly.`;
  $("split3").innerHTML = `<div class="split">` + parts.map((p) => {
    const v = dollars[p.key];
    return `<div class="split-row" title="${esc(p.name)}: ${esc(signedMoney(v))}">
        <span class="name"><span class="key" style="background:${p.color}"></span>${esc(p.name)}</span>
        <span class="split-track"><span class="split-bar ${v < 0 ? "neg" : "pos"}"
              style="width:${(Math.abs(v) / scale * 50).toFixed(2)}%;background:${p.color}"></span></span>
        <span class="amount">${esc(signedMoney(v))}</span></div>`;
  }).join("") + `</div>
    <div class="split-net"><span>Change in ${block.totals.periods} bills</span>
      <b>${esc(signedMoney(dollars.total))}</b></div>`;
}

function renderChanged(block) {
  const s = block.shift;
  $("changed").hidden = !s;
  if (!s) return;
  const u = block.unit, d = places(u), raw = s.basis === "usage";
  const up = s.per_day > 0;
  $("changed-title").textContent = s.note
    ? `${s.start}: ${s.note}`
    : `Something changed in ${s.start}`;
  const size = s.watts ? `about ${num(Math.abs(s.watts))} watts running day and night`
    : `about ${num(Math.abs(s.per_day), d)} ${u} a day`;
  $("changed-note").textContent =
    `Usage stepped ${up ? "up" : "down"} by ${size}`
    + (s.ongoing ? " and has stayed there. " : `, and came back in ${s.end}. `)
    + `That is ${num(Math.abs(s.extra))} ${u} ${up ? "more" : "less"}, about ${money(Math.abs(s.cost))}, over ${num(s.days)} days`
    + (raw ? "." : ", beyond what the weather explains.");
  $("changed-legend").textContent = raw ? `${u} used per day` : `${u} per day, above or below what the weather predicts`;

  const n = s.series.length - 1;
  const level = (v) => (raw ? num(v, d > 1 ? 1 : 0) : signed(v, d));
  draw("changed", Lines, $("chart-changed"), {
    height: 280, zero: raw,
    format: (v) => `${raw ? num(v, 1) : signed(v, d)} ${u}/day`,
    tickFormat: (v) => (raw ? num(v) : signed(v, Math.abs(v) < 3 && v % 1 ? 1 : 0)),
    series: [{ key: "value", label: raw ? "Used" : "Against the weather", color: "var(--series-1)", area: raw }],
    data: s.series.map((r) => ({ ...r, title: r.span })),
    bands: [{ from: s.from, to: s.to }],
    levels: [
      { from: 0, to: s.from - 1, value: s.before, short: level(s.before),
        label: raw ? `usually about ${level(s.before)} ${u} a day` : `usually ${level(s.before)}` },
      { from: s.from, to: s.to, value: s.during, short: level(s.during),
        label: `${raw ? "about " : ""}${level(s.during)} (${signed(s.per_day, d > 1 ? 2 : 1)} a day)` },
      ...(s.after == null ? [] : [{ from: s.to + 1, to: n, value: s.after, short: level(s.after),
                                    label: `${level(s.after)} again` }]),
    ],
  });
  $("changed-table").innerHTML = table([
    { head: "Period", cell: (r) => r.span },
    { head: raw ? `${u}/day` : `${u}/day against the weather`, cell: (r) => (raw ? num(r.value, 1) : signed(r.value, 2)) },
  ], [...s.series].reverse());

  $("note-text").value = s.note || "";
  $("note-clear").hidden = !s.note;
  $("note-form").dataset.key = s.key;
}

function renderScatter(block) {
  const w = block.weather;
  const show = Boolean(w && w.matters && block.points.length);
  $("scatter-section").hidden = !show;
  if (!show) return;
  const u = block.unit, dry = w.kind === "dryness";
  const what = thing(block.fuel).replace(/^./, (ch) => ch.toUpperCase());
  $("scatter-title").textContent = dry ? `${what} follows the dry` : `${what} follows the cold`;
  $("scatter-note").textContent = dry
    ? `Each dot is one bill. The weather explains ${num(w.r2 * 100)}% of the difference between them; `
      + `this home puts about ${num(w.per_inch)} ${u} on the garden for every inch of watering the weather calls for.`
    : `Each dot is one bill. The weather explains ${num(w.r2 * 100)}% of the difference between them; `
      + `this home starts heating when the day averages below about ${w.heat}°F.`;
  draw("scatter", Scatter, $("chart-scatter"), {
    data: block.points, line: [w.base, w.per_heating_degree], height: 320,
    format: (v) => `${num(v, 2)} ${u}/day`,
    xTitle: dry ? "Inches of watering need, average per day" : `Degrees below ${w.heat}°F, average per day`,
    yTitle: `${u} per day`,
  });
  $("scatter-table").innerHTML = table([
    { head: "Bill", cell: (r) => r.label },
    dry ? { head: "Rain", cell: (r) => (r.rain == null ? "—" : `${num(r.rain, 1)} in`) }
        : { head: "Outside", cell: (r) => `${num(r.temp)}°F` },
    dry ? { head: "Need, in/day", cell: (r) => num(r.x, 2) }
        : { head: `Below ${w.heat}°F`, cell: (r) => num(r.x, 1) },
    { head: "Used", cell: (r) => num(r.y, 2) },
    { head: "Predicted", cell: (r) => num(r.fit, 2) },
  ], [...block.points].reverse());
}

function renderTips(block) {
  const list = block.tips || [];
  $("tips-part").hidden = list.length === 0;
  $("tips").innerHTML = list.map((t) => `<div class="card tip">
      <h3>${esc(t.title)}</h3><p>${esc(t.body)}</p>
      <div class="worth">${t.worth.map((w) =>
        `<div><b>${esc(w.value)}</b>${esc(w.label)}</div>`).join("")}</div></div>`).join("");
}

function renderEmpty(block, data) {
  $("insights").hidden = true;
  $("insights-empty").hidden = false;
  $("insights-need").textContent = (block && block.need)
    || "Upload a usage export to see how you compare with last year.";
  $("zip-form").hidden = true;
}

export function renderFuel(data, fuel) {
  latest = { data, fuel };
  const block = (data.fuels || {})[fuel];
  // The temperature of each period, for the Compare card.
  compare.setTemps((block && block.temps) || {});
  if (!block || block.status !== "ok" || !block.totals) {
    renderBox(block || {});
    renderEmpty(block, data);
    return;
  }
  $("insights-empty").hidden = true;
  $("insights").hidden = false;
  renderBox(block);
  $("insights-lede").textContent = lede(block, data);
  renderTiles(block);
  renderCompare(block);
  renderSplit(block);
  renderChanged(block);
  renderScatter(block);
  renderTips(block);

  // No weather, and a ZIP code would fix it: ask for one, in place.
  const gap = block.weather_relevant !== false && !block.weather && data.weather_status === "no-location";
  $("insights-empty").hidden = !gap;
  if (gap) {
    $("insights-need").textContent = weatherGap(data);
    $("zip-form").hidden = false;
  }

  const foot = [];
  if (data.place) foot.push(`Temperatures for ${data.place}.`);
  $("insights-foot").innerHTML = esc(foot.join(" "))
    + (data.attribution
      ? ` <a href="${esc(data.attribution.href)}" rel="noopener noreferrer" target="_blank">${esc(data.attribution.text)}</a>.`
      : "")
    + ((latest && latest.fuel && data.fuels && data.fuels[latest.fuel] && data.fuels[latest.fuel].weather_relevant === false)
      ? "" : ` Carbon uses EPA figures for ${esc(data.grid || "the U.S. grid")} and for natural gas.`);
}

/* Charts read their colours when drawn, so a theme change redraws them. */
export function redraw() {
  if (latest) renderFuel(latest.data, latest.fuel);
}

export async function loadFuel(fuel) {
  let data;
  try {
    const res = await fetch(url("/api/insights"));
    if (!res.ok) throw new Error(String(res.status));
    data = await res.json();
  } catch {
    $("compare-figure").textContent = "—";
    $("compare-copy").textContent = "The comparison with last year couldn't be loaded.";
    return null;
  }
  renderFuel(data, fuel);
  return data;
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || "That didn't save.");
  return res.json();
}

export function initFuel(fuel, flash) {
  $("note-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      renderFuel(await post(url("/api/notes"), { key: e.target.dataset.key, text: $("note-text").value }), fuel);
      flash($("note-text").value.trim() ? "Noted." : "Note removed.");
    } catch (err) { flash(err.message, true); }
  });
  $("note-clear").addEventListener("click", async () => {
    try {
      renderFuel(await post(url("/api/notes"), { key: $("note-form").dataset.key, text: "" }), fuel);
      flash("Note removed.");
    } catch (err) { flash(err.message, true); }
  });
  $("zip-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await post(url("/api/settings"), { zip: $("zip").value, fuel });
      await loadFuel(fuel);
    } catch (err) { flash(err.message, true); }
  });
}

/* ------------------------------------------------------------ the overview */

export function renderCarbon(data) {
  const c = data.carbon;
  $("carbon-section").hidden = !c;
  if (!c) return;
  const names = Object.keys(c.fuels);
  const color = Object.fromEntries(names.map((f) => [f, fuelColor(f)]));
  const spend = names.reduce((sum, f) => sum + c.fuels[f].cost, 0);

  const items = [{
    label: `Carbon dioxide, ${c.count} months`, value: tonnes(c.total),
    foot: c.prior_total
      ? `${pct((c.total - c.prior_total) / c.prior_total * 100)} on the year before`
      : `${c.first} to ${c.last}`,
  }];
  for (const f of names) {
    items.push({
      label: `From ${thing(f)}`, color: color[f], value: tonnes(c.fuels[f].kg),
      foot: `${num(c.fuels[f].kg / c.total * 100)}% of the carbon, ${num(c.fuels[f].cost / spend * 100)}% of the spend`,
    });
  }
  if (names.length === 2) {
    // Per dollar spent: where a dollar saved cuts the most carbon.
    const per = (f) => c.fuels[f].kg / c.fuels[f].cost;
    const [hi, lo] = per(names[0]) >= per(names[1]) ? names : [names[1], names[0]];
    items.push({
      label: "Carbon per dollar", value: `${num(per(hi), 1)} kg on ${thing(hi)}`,
      foot: `${num(per(lo), 1)} kg on ${thing(lo)}: a dollar saved on ${thing(hi)} cuts ${num(per(hi) / per(lo), 1)} times as much`,
    });
  }
  tiles($("carbon-tiles"), items);

  $("carbon-note").textContent = `${c.first} to ${c.last}, in kilograms.`
    + ((c.fuels_without_factor || []).length
      ? ` ${c.fuels_without_factor.join(", ")} isn't counted - there's no published carbon factor for it here.` : "");
  $("carbon-legend").innerHTML = names.map((f) =>
    `<span class="item"><span class="swatch" style="background:${color[f]}"></span>${esc(thing(f).replace(/^./, (ch) => ch.toUpperCase()))}</span>`).join("");
  draw("carbon", StackedColumns, $("chart-carbon"), {
    height: 280, format: (v) => `${num(v)} kg`, tickFormat: (v) => num(v),
    series: names.map((f) => ({ key: f.toLowerCase(), label: f, color: color[f] })),
    data: c.months,
  });
  $("carbon-table").innerHTML = table([
    { head: "Month", cell: (r) => r.label },
    ...names.map((f) => ({ head: `${f}, kg`, cell: (r) => num(r[f.toLowerCase()]) })),
    { head: "Total, kg", cell: (r) => num(r.total) },
  ], [...c.months].reverse());
}

export async function loadOverview() {
  try {
    const res = await fetch(url("/api/insights"));
    if (!res.ok) return null;
    const data = await res.json();
    latest = { data, overview: true };
    renderCarbon(data);
    return data;
  } catch {
    return null;
  }
}

export function redrawOverview() {
  if (latest && latest.overview) renderCarbon(latest.data);
}
