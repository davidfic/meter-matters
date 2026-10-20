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
   change on a year ago, how much was the weather, the rate, and you. Each
   utility gets its own line on its card; the headline gets them added up. */
function renderSplits(d, found) {
  const covered = [];
  const sum = { total: 0, weather: 0, rate: 0, you: 0, weatherKnown: false };
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
    covered.push(f.fuel.toLowerCase());
    sum.total += latest.d_total;
    sum.rate += latest.d_rate || 0;
    sum.you += latest.d_you || 0;
    if (latest.weather_known) { sum.weather += latest.d_weather || 0; sum.weatherKnown = true; }
  }

  const why = $("ov-why");
  if (!why) return;
  if (!covered.length) { why.hidden = true; return; }
  const parts = [
    sum.weatherKnown ? ["the weather", sum.weather] : null,
    ["the rate", sum.rate],
    ["you", sum.you],
  ].filter(Boolean).filter(([, v]) => Math.abs(v) >= 0.5).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  const dir = sum.total < 0 ? "down" : "up";
  const who = covered.length === 1 ? covered[0]
    : covered.slice(0, -1).join(", ") + " and " + covered[covered.length - 1];
  why.hidden = false;
  why.innerHTML = `Your latest ${esc(who)} bills are <span class="${dir}">${dir} ${money(Math.abs(sum.total))}</span> on the same weeks last year`
    + (parts.length ? ": " + parts.map(([w, v]) => `<strong>${signedMoney(v)}</strong> ${w}`).join(", ") : "")
    + ".";
}

/* ------------------------------------------------------- combined totals */

function renderTotals(d) {
  const h = d.headline;
  if (!h) { $("ov-total").textContent = "—"; $("ov-delta").innerHTML = ""; $("ov-stats").innerHTML = ""; return; }
  const names = Object.fromEntries(FUELS.map((f) => [f.key, f.name.toLowerCase()]));

  $("ov-total").textContent = money(h.total_12, false);
  $("ov-delta").innerHTML = h.prior_12 != null
    ? `<span class="${h.total_12 < h.prior_12 ? "down" : "up"}">${signedMoney(h.total_12 - h.prior_12)}</span> vs the 12 months before`
    : `${h.months} month${h.months === 1 ? "" : "s"} of bills on file`;

  const stats = [
    { k: "Averaging", v: money(h.avg_month, false), foot: "a month" },
    h.next_bills != null && {
      k: "Next bills, about", v: money(h.next_bills, false),
      foot: h.next_count === 1 ? "one utility forecast" : `${h.next_count} utilities, forecast`,
    },
    h.by_fuel.length > 1 && {
      k: "Split", v: h.by_fuel.map((f) => f.pct).join(" / "),
      foot: h.by_fuel.map((f) => `${money(f.amount, false)} ${names[f.key] || f.key}`).join(" · "),
    },
    { k: "Priciest month", v: money(h.peak.total, false), foot: h.peak.label },
    { k: "Cheapest month", v: money(h.low.total, false), foot: h.low.label },
  ].filter(Boolean);
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
    refValue: d.headline ? d.headline.avg_month : null,
    refLabel: "12-month average",
  });
  // A utility whose bills start later than the others is said so, by name,
  // rather than counted against the whole chart.
  const late = present
    .map((f) => ({ f, first: rows.findIndex((x) => x[f.key]) }))
    .filter((x) => x.first > 0)
    .map((x) => `${x.f.name.toLowerCase()} from ${rows[x.first].short}`);
  $("energy-note").textContent = `${rows.length} months`
    + (late.length ? ` · ${late.join(", ")}` : "");
  $("energy-table").classList.add("scroll");
  $("energy-table").innerHTML = table(
    [
      { head: "Month", cell: (x) => x.label },
      ...present.map((f) => ({ head: f.name, cell: (x) => money(x[f.key] || 0) })),
      { head: "Total", cell: (x) => money(x.total) },
    ],
    [...rows].reverse(),
  );

  // Seasonal shape: a typical bill for each calendar month, worked out by the
  // server from whatever years each utility has for that month.
  const season = d.season || [];
  charts.season?.destroy();
  if (season.length) {
    charts.season = new StackedColumns($("chart-season"), { height: 260, series, data: season });
  }
  $("season-table").innerHTML = table(
    [
      { head: "Month", cell: (x) => x.label },
      ...present.map((f) => ({ head: f.name, cell: (x) => money(x[f.key] || 0) })),
      { head: "Typical", cell: (x) => money(x.total) },
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
