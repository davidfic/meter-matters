/* Year by year: the average statement with the two what-ifs, and the split into usage and price. */

import { Lines, GroupedDeltaBars } from "./charts.js";
import { $, initTheme, money } from "./ui.js";

initTheme();

const view = JSON.parse($("long-view").textContent);
const years = view.years;
const wi = view.what_if;
const mark = (y) => `${y.year}${y.basis === "estimated" ? "*" : ""}${y.full ? "" : "†"}`;

new Lines($("chart-years"), {
  height: 250, zero: true,
  data: years.map((y) => ({ label: mark(y), bill: y.bill,
    title: `${y.year} · ${y.statements} statement${y.statements === 1 ? "" : "s"}${y.basis === "estimated" ? " · usage estimated" : y.basis === "dollars" ? " · usage not known" : ""}` })),
  series: [{ key: "bill", label: "Average statement", color: "var(--series-1)", area: true }],
  format: (v) => money(v), tickFormat: (v) => money(v, false),
  levels: wi ? [
    { from: 0, to: years.length - 1, value: wi.usage_now_price_then,
      label: `${wi.latest}'s usage at ${wi.base} prices: ${money(wi.usage_now_price_then, false)}`, short: `usage now, ${wi.base} prices` },
    { from: 0, to: years.length - 1, value: wi.usage_then_price_now,
      label: `${wi.base}'s usage at ${wi.latest} prices: ${money(wi.usage_then_price_now, false)}`, short: `${wi.base} usage, prices now` },
  ] : [],
  rows: (d) => { const y = years.find((x) => mark(x) === d.label); return y && y.kwh != null
    ? [{ label: "Per statement", value: `${Math.round(y.kwh).toLocaleString("en-US")} at $${y.rate.toFixed(3)}` }] : []; },
});

const split = years.filter((y) => !y.baseline && y.usage_added != null);
const series = [
  { key: "usage_added", label: "Usage", color: "var(--series-1)" },
  { key: "price_added", label: "Price", color: "var(--series-2)" },
];
if (view.weather) series.push({ key: "weather_added", label: "Of the usage, the weather", color: "var(--series-3)" });
if (split.length) {
  new GroupedDeltaBars($("chart-split"), {
    height: 250, data: split.map((y) => ({ ...y, label: mark(y), total: y.change, title: `${y.year} against ${view.base}, per statement` })),
    series, totalLabel: "Change",
  });
} else {
  $("chart-split").innerHTML = '<p class="note">Nothing to split yet.</p>';
}
