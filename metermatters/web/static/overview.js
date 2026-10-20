/* Combined overview: both fuels side by side, with the detail a click away. */

import { url } from "./base.js";
import { StackedColumns } from "./charts.js";
import * as insights from "./insights.js";
import { $, esc, table, money, signedMoney, pctText, MONTHS, FUELS, fuelColor, fuelsIn,
         initTheme, initTableToggles, boot } from "./ui.js";

const charts = {};
let data = null;


const unitAmount = (n, unit) =>
  `${Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 })} ${unit}`;

/* --------------------------------------------------------- fuel headlines */

function renderFuels(d) {
  $("fuel-cards").innerHTML = d.fuels.map((f) => {
    const l = f.latest;
    const delta = l.delta == null ? "" :
      `<span class="delta-pill ${l.delta < 0 ? "good" : l.delta > 0 ? "bad" : "flat"}">
         <span class="dot"></span>${l.delta < 0 ? "▼" : l.delta > 0 ? "▲" : "■"}
         ${signedMoney(l.delta)} (${pctText(l.pct)})</span>`;

    // Only facts this fuel actually has - gas has no usage on most statements.
    const facts = [
      f.forecast && { k: "Next, about", v: `${money(f.forecast.amount, false)}${f.forecast.due ? ` · ${f.forecast.due.slice(5)}` : ""}` },
      f.usage_12 != null && { k: "Used, last 12", v: unitAmount(f.usage_12, f.unit) },
      f.rate != null && { k: "Unit price", v: `$${f.rate.toFixed(4)}` },
      f.tariff && { k: "Standing charge", v: `${money(f.tariff.standing_charge)}/stmt` },
      { k: f.streak_basis === "weather" ? "Streak vs last year" : f.streak_basis === "usage" ? "Usage streak" : "Down-streak",
        v: f.streak ? `🔥 ${f.streak}` : "—" },
      { k: "Level", v: `${f.level.level} · ${f.level.title}` },
    ].filter(Boolean);

    return `<a class="frow" href="${esc(f.href)}">
      <div class="frow-top">
        <span class="fuel-name"><span class="dot-lg" style="background:${fuelColor(f.fuel)}"></span>${esc(f.fuel)}</span>
        <span class="figure">${money(l.amount)}</span>
      </div>
      <div class="frow-mid">
        <span class="sub-line">${l.label}${l.paid ? "" : " · due " + l.due} · ${money(f.total_12, false)} over the last 12</span>
        ${delta}
      </div>
      <div class="split-line" id="split-${esc(f.key)}" hidden></div>
      <div class="facts">
        ${facts.map((x) => `<div class="fact"><div class="k">${x.k}</div><div class="v">${x.v}</div></div>`).join("")}
      </div>
      <div class="go">${f.statements} statements · ${f.badges.earned}/${f.badges.total} badges <span aria-hidden="true">→</span></div>
    </a>`;
  }).join("");
}

/* The app's best sentence, lifted up to where people actually look: of the
   change on a year ago, how much was the weather, the rate, and you. */
function renderSplits(d, found) {
  for (const f of d.fuels) {
    const block = (found.fuels || {})[f.fuel];
    const latest = block && block.latest;
    const box = $(`split-${f.key}`);
    if (!box || !latest || latest.d_total == null) continue;
    const parts = [
      latest.weather_known ? ["the weather", latest.d_weather] : null,
      ["the rate", latest.d_rate],
      ["you", latest.d_you],
    ].filter(Boolean).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
    const dir = latest.d_total < 0 ? "down" : "up";
    box.hidden = false;
    box.innerHTML = `<span class="${dir}">${dir === "up" ? "Up" : "Down"} ${money(Math.abs(latest.d_total))}</span> on the same weeks last year: `
      + parts.map(([who, v]) => `<strong>${signedMoney(v)}</strong> ${who}`).join(", ")
      + (latest.weather_known ? "." : " <span class=\"note\">(weather not known for this period).</span>");
  }
}

/* ------------------------------------------------------- combined totals */

function renderTotals(d) {
  const clear = () => { $("ov-total").textContent = "—"; $("ov-delta").innerHTML = ""; $("ov-stats").innerHTML = ""; };
  const present = fuelsIn(d.energy || []);
  if (present.length < 2) { clear(); return; }
  const rows = (d.energy || []).filter((x) => present.every((f) => x[f.key]));
  if (rows.length < 2) { clear(); return; }
  const last12 = rows.slice(-12);
  const sums = present.map((f) => last12.reduce((s, x) => s + (x[f.key] || 0), 0));
  const total = sums.reduce((a, b) => a + b, 0);
  const prior = rows.slice(-24, -12);
  const priorTotal = prior.reduce((s, x) => s + x.total, 0);
  const peak = last12.reduce((a, b) => (b.total > a.total ? b : a));
  const low = last12.reduce((a, b) => (b.total < a.total ? b : a));

  $("ov-total").textContent = money(total, false);
  $("ov-delta").innerHTML = prior.length === 12
    ? `<span class="${total < priorTotal ? "down" : "up"}">${signedMoney(total - priorTotal)}</span> vs the 12 before`
    : `Averaging ${money(total / last12.length)} a month`;
  const stats = [
    { k: "Split", v: sums.map((s) => Math.round(s / total * 100)).join(" / "),
      foot: present.map((f, i) => `${money(sums[i], false)} ${f.key}`).join(" · ") },
    { k: "Priciest month", v: money(peak.total, false), foot: peak.label },
    { k: "Cheapest month", v: money(low.total, false), foot: low.label },
  ];
  $("ov-stats").innerHTML = stats.map((t) =>
    `<div><dt>${esc(t.k)}</dt><dd>${esc(t.v)}</dd><div class="sf">${esc(t.foot)}</div></div>`).join("");
}

/* ------------------------------------------------------------- alerts */

function renderAlerts(d) {
  // Changes in usage arrive later than the rest, and lead when they do.
  const rows = [...(d.changes || []), ...(d.alerts || [])];
  $("alerts-section").hidden = rows.length === 0;
  if (!rows.length) return;
  $("alerts-note").textContent =
    `${rows.length} thing${rows.length === 1 ? "" : "s"} across both fuels that the numbers alone won't tell you.`;
  $("alerts").innerHTML = rows.map((a) => `<div class="alert-row">
      <span class="tag">${esc(a.fuel)}</span>
      <div class="body"><div class="t">${esc(a.title)}</div><div class="d">${esc(a.detail)}</div></div>
      <a href="${esc(a.href)}">Look →</a>
    </div>`).join("");
}

/* ------------------------------------------------------------- charts */

function renderCharts(d) {
  const present = fuelsIn(d.energy || []);
  const rows = (d.energy || []).filter((x) => present.some((f) => x[f.key]));
  if (rows.length < 2) return;
  const series = present.map((f) => ({ key: f.key, label: f.name, color: f.color }));
  $("energy-legend").innerHTML = series.map((s) =>
    `<span class="item"><span class="swatch" style="background:${s.color}"></span>${esc(s.label)}</span>`).join("");
  $("season-legend").innerHTML = $("energy-legend").innerHTML;

  charts.energy?.destroy();
  charts.energy = new StackedColumns($("chart-energy"), {
    height: 300, series,
    data: rows.map((x) => ({ ...x, label: x.short })),
  });
  const all = rows.filter((x) => present.every((f) => x[f.key]));
  $("energy-note").textContent = present.length > 1
    ? `${rows.length} months · every utility on ${all.length} of them`
      + (all.length < rows.length ? " (some bills only start partway through)" : "")
    : `${rows.length} months`;
  $("energy-table").classList.add("scroll");
  $("energy-table").innerHTML = table(
    [
      { head: "Month", cell: (x) => x.label },
      ...present.map((f) => ({ head: f.name, cell: (x) => money(x[f.key] || 0) })),
      { head: "Total", cell: (x) => money(x.total) },
    ],
    [...rows].reverse(),
  );

  // Seasonal shape: average each calendar month across every year we have.
  const buckets = new Map();
  for (const row of all) {
    const m = Number(row.period.split("-")[1]);
    const b = buckets.get(m) || { n: 0 };
    for (const f of present) b[f.key] = (b[f.key] || 0) + row[f.key];
    b.n += 1;
    buckets.set(m, b);
  }
  const season = [...buckets.entries()].sort((a, b) => a[0] - b[0]).map(([m, b]) => {
    const out = { label: MONTHS[m - 1], month: m, n: b.n, total: 0 };
    for (const f of present) { out[f.key] = b[f.key] / b.n; out.total += out[f.key]; }
    return out;
  });
  charts.season?.destroy();
  charts.season = new StackedColumns($("chart-season"), { height: 260, series, data: season });
  $("season-table").innerHTML = table(
    [
      { head: "Month", cell: (x) => x.label },
      ...present.map((f) => ({ head: f.name, cell: (x) => money(x[f.key]) })),
      { head: "Total", cell: (x) => money(x.total) },
      { head: "Years", cell: (x) => x.n },
    ],
    season,
  );
}

/* -------------------------------------------------------------- wiring */

function paint(d) {
  data = d;
  if (!d.fuels || !d.fuels.length) {
    $("coverage").textContent = "No statements yet.";
    return;
  }
  const first = d.energy.length ? d.energy[0].label : "";
  const last = d.energy.length ? d.energy[d.energy.length - 1].label : "";
  $("coverage").textContent = `${first} – ${last} · ${d.energy.length} months`
    + (d.merged_accounts ? " · accounts merged" : "");
  $("foot").textContent =
    "Figures come straight from your bills. Each fuel's page has the full history, streaks and achievements.";
  renderFuels(d);
  renderTotals(d);
  renderAlerts(d);
  renderCharts(d);
  insights.loadOverview().then((found) => {
    if (found && found.alerts.length) renderAlerts({ ...d, changes: found.alerts });
    if (found) renderSplits(d, found);
  });
}

initTheme(() => {
  if (data) renderCharts(data);   // charts read CSS vars at draw time
  insights.redrawOverview();
});
initTableToggles();
boot(paint, url("/api/overview"));
