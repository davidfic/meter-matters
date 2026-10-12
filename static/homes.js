/* All homes at once: what they add up to, and a way into each.

   Only totals are added here - spend, usage, carbon. What changed since last
   year is about one house in one place, so it stays on that home's own pages. */

import { url } from "./base.js";
import { StackedColumns } from "./charts.js";
import { $, esc, table, money, initTheme, initTableToggles, boot } from "./ui.js";

const COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)"];
const OTHER = "var(--seq-250)";
let data = null;
let chart = null;

const whole = (n) => Math.round(n).toLocaleString("en-US");


/* Three colours exist for categories; any further homes share one muted one. */
function seriesFor(homes) {
  const out = homes.slice(0, 3).map((h, i) => ({ key: h.id, label: h.name, color: COLORS[i] }));
  if (homes.length > 3) out.push({ key: "other", label: "Other homes", color: OTHER });
  return out;
}

function renderTotals(d) {
  const t = d.totals;
  // Usage comes from a separate export; without it there is spend, not kWh.
  const fuels = Object.entries(t.by_fuel).map(([fuel, v]) => ({
    label: `${fuel}, last 12 statements`, value: money(v.total_12, false),
    foot: v.usage_12 > 0 ? `${whole(v.usage_12)} ${esc(v.unit)} used across all homes` : "across all homes" }));
  const tiles = [
    { label: "Energy, last 12 statements", value: money(t.total_12, false),
      foot: `${d.homes.length} homes added together` },
    ...fuels,
    t.carbon_kg_12 > 0 && { label: "Carbon", value: `${(t.carbon_kg_12 / 1000).toFixed(1)} t`,
      foot: "CO₂e from that usage, at published grid and gas factors" },
  ].filter(Boolean);
  $("totals").innerHTML = tiles.slice(0, 4).map((x) => `<div class="card tile">
      <div class="label">${x.label}</div><div class="value">${x.value}</div>
      <div class="foot">${x.foot}</div></div>`).join("");
}

function renderChart(d) {
  const series = seriesFor(d.homes);
  const rows = d.monthly.map((m) => {
    const row = { label: m.short, period: m.period };
    let other = 0;
    d.homes.forEach((h, i) => {
      const v = m.values[h.id] || 0;
      if (i < 3) row[h.id] = v; else other += v;
    });
    if (d.homes.length > 3) row.other = other;
    row.total = Object.values(m.values).reduce((a, b) => a + b, 0);
    return row;
  });
  $("monthly-note").textContent = rows.length
    ? "Every fuel, every home, by statement month."
    : "Nothing to chart yet — add bills to a home.";
  $("monthly-legend").innerHTML = series.map((s) =>
    `<span class="item"><span class="swatch" style="background:${s.color}"></span>${esc(s.label)}</span>`).join("");

  chart?.destroy();
  if (rows.length) {
    chart = new StackedColumns($("chart-monthly"), {
      height: 240, series, data: rows,
      tickFormat: (v) => money(v, false),
    });
  }
  $("monthly-table").innerHTML = table([
    { head: "Month", cell: (r) => r.label },
    ...d.homes.map((h) => ({ head: h.name,
      cell: (r) => { const m = d.monthly.find((x) => x.period === r.period); return money(m.values[h.id] || 0); } })),
    { head: "Total", cell: (r) => money(r.total) },
  ], [...rows].reverse());
}

function renderCards(d) {
  $("home-cards").innerHTML = d.homes.map((h) => `<a class="card fuel-card home-card" href="${esc(url(h.url))}">
      <div class="fuel-head"><span class="fuel-name">${esc(h.name)}</span>
        <span class="go">${h.alerts ? `${h.alerts} to look at · ` : ""}Open →</span></div>
      ${h.has_bills
        ? `<div class="figure">${money(h.total_12, false)}</div>
           <div class="sub-line">over the last 12 statements</div>
           ${h.fuels.map((f) => `<div class="fuel-row"><span>${esc(f.fuel)} · ${esc(f.label)}</span>
             <span>${money(f.latest)}${f.rate != null ? ` · $${Number(f.rate).toFixed(4)}/${esc(f.unit)}` : ""}</span></div>`).join("")}`
        : `<p class="note" style="margin:8px 0 0">No bills yet. Open it to add some.</p>`}
    </a>`).join("");
}

function renderAlerts(d) {
  $("alerts-section").hidden = !d.alerts.length;
  $("alerts").innerHTML = d.alerts.map((a) => `<div class="alert-row">
      <span class="tag">${esc(a.home)}</span>
      <div class="body"><div class="t">${esc(a.title)}</div><div class="d">${esc(a.detail)}</div></div>
      <a href="${esc(url(a.href))}">Look →</a></div>`).join("");
}

function paint(d) {
  data = d;
  renderTotals(d); renderChart(d); renderCards(d); renderAlerts(d);
}

initTableToggles();
initTheme(() => { if (data) renderChart(data); });
boot(paint, url("/api/homes/overview"));
