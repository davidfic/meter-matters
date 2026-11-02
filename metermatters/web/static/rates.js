/* The rates page: your price per kWh taken apart.

   Draws from /api/rates. Everything that depends on the supply price you're
   on is recomputed here, so trying one out is instant. */

import { url } from "./base.js";
import { LineChart, Columns } from "./charts.js";
import { $, esc, table, flash, money, MONTHS, fetchJSON, initTheme, initTableToggles } from "./ui.js";

const charts = {};

let data = null;
let supply = null;          // cents per kWh the household is on, as entered

const dollars = (v) => "$" + Number(v).toFixed(4);
const cents = (v) => Number(v).toFixed(2) + "¢";
const signed = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + money(Math.abs(v), false);
const pct = (p) => (p > 0 ? "+" : p < 0 ? "−" : "") + Math.abs(p) + "%";

const load = fetchJSON;

/* --------------------------------------------------------------- headline */

function renderTiles(d) {
  const b = d.breakdown;
  const s = d.standing;
  const tiles = [
    {
      label: "You pay, all in",
      value: dollars(d.now) + "/kWh",
      foot: s && s.year_pct != null
        ? `${pct(s.year_pct)} on a year ago · higher than ${s.percentile}% of your ${s.of} statements`
        : `${d.latest}`,
    },
    b ? {
      label: "…of which supply",
      value: cents(b.supply * 100),
      foot: `from your bill · <button class="link" id="edit-supply" type="button">change</button>`,
    } : {
      label: "…of which supply",
      value: "?",
      foot: `<button class="link" id="edit-supply" type="button">Enter it from your bill</button> to split the price`,
    },
    b && {
      label: "…and delivery, etc.",
      value: cents(b.delivery * 100),
      foot: "Set by your utility — the same whoever you buy from",
    },
    d.tariff && {
      label: "Standing charge",
      value: money(d.tariff.standing_charge) + "/stmt",
      foot: "Owed whatever you use",
    },
  ].filter(Boolean);
  $("tiles").innerHTML = tiles.map((t) => `<div class="card tile">
      <div class="label">${t.label}</div><div class="value">${t.value}</div>
      <div class="foot">${t.foot}</div></div>`).join("");
  $("edit-supply").addEventListener("click", askSupply);
}

function renderSplit(d) {
  const b = d.breakdown;
  if (!b) {
    $("split").innerHTML = "";
    $("split-note").textContent = supply != null
      ? "That supply price is at or above your all-in price, so it can't be the supply half. Change it on the tile above."
      : "Enter the supply price from your bill's supply line to split the price.";
    return;
  }
  const sh = b.supply_share;
  $("split-note").innerHTML =
    `Supply is about <strong>${Math.round(sh)}%</strong> of each kWh. `
    + `Delivery is a regulated charge on the same wires whoever supplies the electricity.`;
  $("split").innerHTML = `
    <div class="price-split" role="img" aria-label="Supply ${cents(b.supply * 100)}, delivery and other ${cents(b.delivery * 100)}">
      <div style="flex:${(b.supply * 1000).toFixed(0)} 1 0;background:var(--series-1)">Supply ${cents(b.supply * 100)}</div>
      <div style="flex:${(b.delivery * 1000).toFixed(0)} 1 0;background:var(--series-2)">Delivery &amp; other ${cents(b.delivery * 100)}</div>
    </div>
    <div class="split-key">
      <span>Per kWh, all in: <b>${dollars(b.all_in)}</b></span>
      ${b.standing_charge ? `<span>Plus a standing charge of about <b>${money(b.standing_charge)}</b> a statement</span>` : ""}
    </div>`;
}

/* ----------------------------------------------------------------- charts */

function renderCharts(d) {
  const rows = d.series;
  charts.history?.destroy();
  charts.season?.destroy();
  $("history-note").innerHTML = `Ranged ${dollars(d.min)}–${dollars(d.max)}; `
    + `<strong>${d.changes.length}</strong> move${d.changes.length === 1 ? "" : "s"} of 3% or more.`;
  if (rows.length) {
    charts.history = new LineChart($("chart-history"), {
      height: 240,
      data: rows.map((x) => ({ label: x.short, value: x.rate, row: x })),
      format: (v) => `$${v.toFixed(3)}`,
      tickFormat: (v) => `$${v.toFixed(2)}`,
      tooltip: (x) => `<div class="t-title">${x.row.label}</div>
        <div class="t-row"><span>All-in</span><b>${dollars(x.value)}/kWh</b></div>`,
    });
  }
  $("history-table").innerHTML = table([
    { head: "Statement", cell: (x) => x.label },
    { head: "$/kWh", cell: (x) => x.rate.toFixed(4) },
  ], [...rows].reverse());

  const seasons = d.seasons.map((s) => ({ ...s, label: MONTHS[s.month - 1], value: s.rate }));
  if (seasons.length) {
    const lo = seasons.reduce((a, b) => (b.rate < a.rate ? b : a));
    const hi = seasons.reduce((a, b) => (b.rate > a.rate ? b : a));
    $("season-note").innerHTML = `Cheapest in <strong>${MONTHS[lo.month - 1]}</strong> (${dollars(lo.rate)}), `
      + `dearest in <strong>${MONTHS[hi.month - 1]}</strong> (${dollars(hi.rate)}). `
      + `Averaged over the years held.`;
    charts.season = new Columns($("chart-season"), {
      height: 240,
      data: seasons,
      format: (v) => `$${v.toFixed(3)}`,
      tickFormat: (v) => `$${v.toFixed(2)}`,
      tooltip: (x) => `<div class="t-title">${MONTHS[x.month - 1]}</div>
        <div class="t-row"><span>Average</span><b>${dollars(x.rate)}/kWh</b></div>
        <div class="t-row"><span>Statements</span><b>${x.n}</b></div>`,
    });
    $("season-table").innerHTML = table([
      { head: "Month", cell: (x) => MONTHS[x.month - 1] },
      { head: "$/kWh", cell: (x) => x.rate.toFixed(4) },
      { head: "Statements", cell: (x) => x.n },
    ], seasons);
  }
}

/* --------------------------------------------------------------- what if */

function renderWhatIf(d) {
  const ok = d.breakdown && d.last_year && d.last_year.kwh > 0;
  $("whatif-section").hidden = !ok;
  if (!ok) return;
  const el = $("whatif");
  if (!el.dataset.ready) {
    el.dataset.ready = "1";
    el.addEventListener("input", drawWhatIf);
  }
  // Wide enough to hold whatever the supply price is, with room either side.
  el.min = Math.min(8, Math.floor(supply - 5)).toFixed(0);
  el.max = Math.max(30, Math.ceil(supply + 5)).toFixed(0);
  el.value = supply.toFixed(1);
  drawWhatIf();
}

function drawWhatIf() {
  const v = Number($("whatif").value);
  const kwhYear = data.last_year.kwh;
  const delta = (v - supply) / 100 * kwhYear;
  const allIn = data.now + (v - supply) / 100;
  $("whatif-out").innerHTML = `
    <div class="big">${cents(v)} <span style="font-weight:400;color:var(--text-muted);font-size:14px">supply</span></div>
    <div class="sub-line">All in: <b>${dollars(allIn)}/kWh</b> · over your last ${data.last_year.months} statements
      (${Math.round(kwhYear).toLocaleString("en-US")} kWh) that's
      <b style="color:${delta < 0 ? "var(--good-text)" : delta > 0 ? "var(--critical)" : "inherit"}">
      ${Math.abs(delta) < 0.5 ? "no change" : signed(delta)}</b></div>`;
}

/* ---------------------------------------------------------------- actions */

async function update(url, body) {
  try {
    data = await load(url, { method: "POST", headers: { "Content-Type": "application/json" },
                             body: JSON.stringify(body) });
    render();
  } catch (err) { flash(`That didn't save: ${err.message}`, true); }
}

function askSupply() {
  const answer = prompt(
    "What do you pay per kWh for supply (cents)? It's on your bill's supply line.\n"
    + "Leave empty to clear it.", supply != null ? supply.toFixed(2) : "");
  if (answer === null) return;
  update(url("/api/rates/supply"), { supply_cents: answer.trim() === "" ? null : answer });
}

/* ------------------------------------------------------------------- page */

function render() {
  $("loading").hidden = true;
  if (!data.series.length) {
    $("empty").hidden = false;
    $("empty").innerHTML = `<p class="note" style="margin:0">${esc(data.error || "No electric bills with usage yet.")} `
      + `<a href="${url("/connect")}">Add bills</a></p>`;
    return;
  }
  $("page").hidden = false;
  supply = data.settings.supply_cents;
  renderTiles(data);
  renderSplit(data);
  renderCharts(data);
  renderWhatIf(data);

  $("fineprint").innerHTML = `
    <p><strong>How to read this.</strong> Your statement shows one all-in price. Underneath it are two: supply, the
    electricity itself, and delivery, the wires and the meter, which your utility sets and which stays the same
    whoever supplies the power. In states with retail choice, supply is the part you can shop for.</p>
    <p>Enter the supply price from your bill's supply line to see the split, and drag the slider to see what another
    supply price would have done over your last year. Nothing on this page leaves the server.</p>`;
}

async function main() {
  initTheme(() => { if (data) renderCharts(data); });
  initTableToggles();
  try {
    data = await load(url("/api/rates"));
    render();
  } catch {
    $("loading").innerHTML = `<p class="note" style="margin:0">Couldn't load your rates. Try refreshing.</p>`;
  }
}

main();
