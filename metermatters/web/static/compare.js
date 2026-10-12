/* Compare: any two statements side by side, or one calendar month across every year.

   Everything comes from the report already on the page. Where the statements
   carry usage, the change in cost is split exactly into the usage part and the
   rate part (d(cost) = rate_then × d(usage) + usage_now × d(rate)); where the
   temperatures are known (the insights arrive later), the average for each
   period is shown too, so "it was colder" is a fact rather than a guess. */

import { $, esc, raw, table, money, signedMoney, pctText, MONTHS, Columns } from "./ui.js";

let report = null;
let temps = {};          // service_end -> average temperature, once the insights have arrived
let chart = null;

const rows = () => (report ? report.series : []);
const unit = () => (rows().length ? rows()[rows().length - 1].unit || "kWh" : "kWh");
const byPeriod = (p) => rows().find((r) => r.period === p);

/* ------------------------------------------------------------- state */

function readHash() {
  const m = location.hash.match(/compare=([0-9-]+),([0-9-]+)/);
  if (m) return { mode: "two", a: m[1], b: m[2] };
  const y = location.hash.match(/month=(\d{1,2})/);
  if (y) return { mode: "years", month: Number(y[1]) };
  return null;
}

function writeHash(state) {
  const text = state.mode === "two" ? `compare=${state.a},${state.b}` : `month=${state.month}`;
  history.replaceState(null, "", `#${text}`);
}

function defaults() {
  const all = rows();
  const last = all[all.length - 1];
  const yearAgo = all.find((r) => r.month === last.month && r.year === last.year - 1);
  const prev = all[all.length - 2];
  return { mode: "two", a: (yearAgo || prev || last).period, b: last.period, month: last.month };
}

/* ------------------------------------------------------------ two months */

function fact(r) {
  return {
    amount: r.amount, usage: r.kwh, perDay: r.kwh_per_day, rate: r.rate, days: r.days,
    temp: temps[r.service_end], label: r.label, metered: r.service_label,
  };
}

function renderTwo(a, b) {
  const A = fact(a), B = fact(b), u = unit();
  const diff = (x, y, fmt, invert = false) => {
    if (x == null || y == null) return "—";
    const d = y - x;
    const cls = d === 0 ? "" : (d < 0) !== invert ? "down" : "up";
    const pct = x ? ` (${pctText(Math.round(d / x * 1000) / 10)})` : "";
    return raw(`<span class="${cls}">${fmt(d, true)}${pct}</span>`);
  };
  const num = (v, s = false) => (s && v > 0 ? "+" : "") + Number(v).toLocaleString("en-US", { maximumFractionDigits: 1 });
  const lines = [
    ["Amount", money(A.amount), money(B.amount), diff(A.amount, B.amount, (d) => signedMoney(d))],
    A.usage != null || B.usage != null ? [`Used, ${u}`, A.usage == null ? "—" : num(A.usage), B.usage == null ? "—" : num(B.usage),
      diff(A.usage, B.usage, (d) => num(d, true))] : null,
    A.perDay != null || B.perDay != null ? [`${u} a day`, A.perDay == null ? "—" : num(A.perDay), B.perDay == null ? "—" : num(B.perDay),
      diff(A.perDay, B.perDay, (d) => num(d, true))] : null,
    A.rate != null || B.rate != null ? [`Price per ${u}`, A.rate == null ? "—" : `$${A.rate.toFixed(4)}`, B.rate == null ? "—" : `$${B.rate.toFixed(4)}`,
      diff(A.rate, B.rate, (d) => (d > 0 ? "+" : "−") + "$" + Math.abs(d).toFixed(4))] : null,
    A.days != null || B.days != null ? ["Days", A.days ?? "—", B.days ?? "—", diff(A.days, B.days, (d) => num(d, true))] : null,
    A.temp != null || B.temp != null ? ["Average temperature", A.temp == null ? "—" : `${Math.round(A.temp)}°F`, B.temp == null ? "—" : `${Math.round(B.temp)}°F`,
      diff(A.temp, B.temp, (d) => `${d > 0 ? "+" : "−"}${Math.abs(Math.round(d))}°`)] : null,
    A.metered || B.metered ? ["Metered", A.metered || "—", B.metered || "—", ""] : null,
  ].filter(Boolean);
  $("cmp-table").innerHTML = table([
    { head: "", cell: (r) => r[0] },
    { head: A.label, cell: (r) => r[1] },
    { head: B.label, cell: (r) => r[2] },
    { head: "Change", cell: (r) => r[3] },
  ], lines);

  // The sentence: what moved, and why, when the figures allow it.
  const d = B.amount - A.amount;
  let why = "";
  if (A.usage != null && B.usage != null && A.rate != null && B.rate != null) {
    const usagePart = A.rate * (B.usage - A.usage);
    const ratePart = B.usage * (B.rate - A.rate);
    why = ` Of that, <strong>${signedMoney(usagePart)}</strong> is how much you used and <strong>${signedMoney(ratePart)}</strong> is the price per ${u}.`;
  }
  let weather = "";
  if (A.temp != null && B.temp != null) {
    const dt = Math.round(B.temp - A.temp);
    weather = dt === 0 ? " The weather was much the same." : ` ${B.label} averaged ${Math.abs(dt)}° ${dt > 0 ? "warmer" : "colder"}.`;
  }
  $("cmp-note").innerHTML = d === 0
    ? `${esc(B.label)} cost the same as ${esc(A.label)}.`
    : `${esc(B.label)} cost <strong class="${d < 0 ? "down" : "up"}">${money(Math.abs(d))} ${d < 0 ? "less" : "more"}</strong> than ${esc(A.label)}`
      + (A.amount ? ` (${pctText(Math.round(d / A.amount * 1000) / 10)})` : "") + `.${why}${weather}`;
  chart?.destroy(); chart = null;
  $("chart-cmp").innerHTML = "";
}

/* ------------------------------------------------------------ every year */

function renderYears(month) {
  const u = unit();
  const years = rows().filter((r) => r.month === month).sort((a, b) => a.year - b.year);
  if (!years.length) {
    $("cmp-note").textContent = `No ${MONTHS[month - 1]} statement on file.`;
    $("cmp-table").innerHTML = ""; $("chart-cmp").innerHTML = "";
    return;
  }
  const first = years[0], last = years[years.length - 1];
  const d = last.amount - first.amount;
  const hasUsage = years.some((r) => r.kwh != null);
  $("cmp-note").innerHTML = years.length === 1
    ? `Only one ${MONTHS[month - 1]} so far (${esc(first.label)}).`
    : `${esc(MONTHS[month - 1])}, ${years.length} years: from ${money(first.amount)} in ${first.year} to ${money(last.amount)} in ${last.year} `
      + `(<strong class="${d < 0 ? "down" : "up"}">${signedMoney(d)}</strong>)`
      + (hasUsage && first.kwh != null && last.kwh != null
        ? `, using ${Number(last.kwh - first.kwh).toLocaleString("en-US", { maximumFractionDigits: 0, signDisplay: "always" })} ${u} ${last.kwh >= first.kwh ? "more" : "less"}.` : ".");
  chart?.destroy();
  chart = new Columns($("chart-cmp"), {
    height: 200,
    data: years.map((r) => ({ label: String(r.year), value: r.amount, row: r })),
    tooltip: (x) => `<div class="t-title">${esc(x.row.label)}</div>
      <div class="t-row"><span>Amount</span><b>${money(x.value)}</b></div>
      ${x.row.kwh != null ? `<div class="t-row"><span>Used</span><b>${Number(x.row.kwh).toLocaleString("en-US")} ${esc(u)}</b></div>` : ""}`,
  });
  $("cmp-table").innerHTML = table([
    { head: "Year", cell: (r) => r.label },
    { head: "Amount", cell: (r) => money(r.amount) },
    { head: "vs year before", cell: (r, i) => {
        const prev = years[years.indexOf(r) - 1];
        if (!prev) return "—";
        const dd = r.amount - prev.amount;
        return raw(`<span class="${dd < 0 ? "down" : "up"}">${signedMoney(dd)}${prev.amount ? ` (${pctText(Math.round(dd / prev.amount * 1000) / 10)})` : ""}</span>`);
      } },
    ...(hasUsage ? [
      { head: `Used, ${u}`, cell: (r) => r.kwh == null ? "—" : Math.round(r.kwh).toLocaleString("en-US") },
      { head: `${u}/day`, cell: (r) => r.kwh_per_day == null ? "—" : r.kwh_per_day.toFixed(1) },
      { head: `$/${u}`, cell: (r) => r.rate == null ? "—" : r.rate.toFixed(4) },
    ] : []),
    ...(years.some((r) => temps[r.service_end] != null)
      ? [{ head: "Avg °F", cell: (r) => temps[r.service_end] == null ? "—" : Math.round(temps[r.service_end]) }] : []),
  ], years);
}

/* ---------------------------------------------------------------- wiring */

function fill() {
  const options = rows().map((r) => `<option value="${esc(r.period)}">${esc(r.label)}</option>`).join("");
  $("cmp-a").innerHTML = options;
  $("cmp-b").innerHTML = options;
  $("cmp-month").innerHTML = MONTHS.map((m, i) => `<option value="${i + 1}">${m}</option>`).join("");
}

function state() {
  const mode = document.querySelector("input[name=cmp-mode]:checked").value;
  return { mode, a: $("cmp-a").value, b: $("cmp-b").value, month: Number($("cmp-month").value) };
}

function render() {
  const s = state();
  $("cmp-two").hidden = s.mode !== "two";
  $("cmp-years").hidden = s.mode !== "years";
  if (s.mode === "two") {
    const a = byPeriod(s.a), b = byPeriod(s.b);
    if (a && b) renderTwo(a, b);
  } else {
    renderYears(s.month);
  }
  writeHash(s);
}

export function init(r) {
  report = r;
  const section = $("cmp-section");
  if (!r.series || r.series.length < 2) { section.hidden = true; return; }
  section.hidden = false;
  fill();
  const s = readHash() || defaults();
  const d = defaults();
  document.querySelector(`input[name=cmp-mode][value=${s.mode}]`).checked = true;
  $("cmp-a").value = byPeriod(s.a) ? s.a : d.a;
  $("cmp-b").value = byPeriod(s.b) ? s.b : d.b;
  $("cmp-month").value = String(s.month || d.month);
  if (!section.dataset.ready) {
    section.dataset.ready = "1";
    for (const el of document.querySelectorAll("input[name=cmp-mode], #cmp-a, #cmp-b, #cmp-month")) {
      el.addEventListener("change", render);
    }
    $("cmp-swap").addEventListener("click", () => {
      const a = $("cmp-a").value; $("cmp-a").value = $("cmp-b").value; $("cmp-b").value = a; render();
    });
  }
  render();
}

/* The temperatures arrive with the insights; the comparison gains a row when they do. */
export function setTemps(map) {
  temps = map || {};
  if (report && !$("cmp-section").hidden) render();
}

export function redraw() {
  if (report && !$("cmp-section").hidden) render();
}
