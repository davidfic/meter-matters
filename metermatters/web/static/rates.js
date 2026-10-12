/* The rates page: your price per kWh taken apart, and the supply offers around you.

   Draws from /api/rates, which may be slow (it asks the state's comparison
   site), so the shell paints first. Everything that depends on the supply
   price you're on is recomputed here, so trying one out is instant. */

import { url } from "./base.js";
import { LineChart, Columns } from "./charts.js";
import { $, esc, table, flash, money, MONTHS, fetchJSON, initTheme, initTableToggles } from "./ui.js";

const charts = {};

let data = null;
let supply = null;          // cents per kWh the household is on, as shown
let open = null;            // the offer whose detail is expanded

const dollars = (v) => "$" + Number(v).toFixed(4);
const cents = (v) => Number(v).toFixed(2) + "¢";
const signed = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + money(Math.abs(v), false);
const pct = (p) => (p > 0 ? "+" : p < 0 ? "−" : "") + Math.abs(p) + "%";
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);

const load = fetchJSON;

/* --------------------------------------------------------------- headline */

function renderTiles(d) {
  const b = d.breakdown;
  const s = d.standing;
  const cmp = d.comparison;
  const youWord = cmp && cmp.supply.assumed
    ? `assumed: ${esc(cmp.supply.name || "Basic Service")}`
    : "as you told us";
  const tiles = [
    {
      label: "You pay, all in",
      value: dollars(d.now) + "/kWh",
      foot: s && s.year_pct != null
        ? `${pct(s.year_pct)} on a year ago · higher than ${s.percentile}% of your ${s.of} statements`
        : `${d.latest}`,
    },
    b && {
      label: "…of which supply",
      value: cents(b.supply * 100),
      foot: `${youWord} · <button class="link" id="edit-supply" type="button">change</button>`,
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
  const edit = $("edit-supply");
  if (edit) edit.addEventListener("click", askSupply);
}

function renderSplit(d) {
  const b = d.breakdown;
  if (!b) {
    $("split").innerHTML = "";
    $("split-note").textContent = d.status === "ok"
      ? "Needs the supply price to split the bill."
      : "Needs the supplier offers to split the bill — they aren't available right now.";
    return;
  }
  const sh = b.supply_share;
  $("split-note").innerHTML =
    `Only the supply part — about <strong>${Math.round(sh)}%</strong> of each kWh — can be shopped for. `
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

/* ---------------------------------------------------------------- offers */

function view() {
  const cmp = data.comparison;
  const term = Number($("term").value) || null;
  let rows = cmp.offers.filter((o) => {
    if ($("f-green").checked && !(o.renewable_pct >= 100)) return false;
    if ($("f-nofee").checked && o.exit_fee !== 0) return false;
    if ($("f-existing").checked && o.new_customers_only) return false;
    if (term && !(o.term_months && o.term_months <= term) && o.kind === "supplier") return false;
    return true;
  });
  const by = $("sort").value;
  const key = {
    cents: (o) => o.cents,
    renewable: (o) => -(o.renewable_pct || 0) * 1000 + o.cents,
    term: (o) => (o.term_months ?? 999) * 1000 + o.cents,
  }[by];
  rows = [...rows].sort((a, b) => key(a) - key(b));
  return rows;
}

function changeFor(o) {
  return (o.cents - supply) / 100 * data.comparison.kwh
    + o.monthly_fee * data.comparison.months + o.enrollment_fee;
}

function renderOffers() {
  const cmp = data.comparison;
  if (!cmp || !cmp.offers.length) { $("market").hidden = true; return; }
  const rows = view();
  const top = Math.max(...cmp.offers.map((o) => o.cents));
  const label = { basic: "Utility Basic Service", municipal: "Town program", supplier: "Competitive supplier" };
  const m = cmp.market;

  $("market-note").innerHTML = `Showing <strong>${rows.length}</strong> of ${cmp.offers.length} offers, `
    + `priced on your last ${cmp.months} statements (${Math.round(cmp.kwh).toLocaleString("en-US")} kWh). `
    + `Click one for the details.`;

  $("offers").innerHTML = rows.map((o) => {
    const ch = changeFor(o);
    const isMine = cmp.supply.assumed ? o.id === cmp.supply.id : Math.abs(o.cents - supply) < 0.0005;
    const sub = [label[o.kind], o.term, o.renewable_pct != null ? `${Math.round(o.renewable_pct)}% renewable` : null]
      .filter(Boolean).join(" · ");
    const dir = ch < -0.5 ? "down" : ch > 0.5 ? "up" : "";
    const detail = open === o.id ? detailHtml(o) : "";
    return `<div class="offer${isMine ? " mine" : ""}">
      <button type="button" class="offer-row" data-id="${esc(o.id)}" aria-expanded="${open === o.id}">
        <span class="who"><b>${esc(o.supplier)}</b><span>${esc(sub)}</span></span>
        <span class="cents">${cents(o.cents)}</span>
        <span class="bar" aria-hidden="true"><i style="width:${(o.cents / top * 100).toFixed(1)}%"></i><u style="left:${(supply / top * 100).toFixed(1)}%"></u></span>
        <span class="change ${dir}">${Math.abs(ch) < 0.5 ? "same" : signed(ch)}<small>a year</small></span>
      </button>${detail}</div>`;
  }).join("") || `<p class="note">Nothing matches those filters.</p>`;
}

function detailHtml(o) {
  const fee = o.exit_fee == null ? "Not stated" : o.exit_fee === 0 ? "None" : money(o.exit_fee);
  const items = [
    ["Price", `${o.cents.toFixed(3)}¢/kWh${o.structure ? ` · ${esc(o.structure.toLowerCase())}` : ""}`],
    ["Term", esc(o.term || "—") + (o.ends ? ` (${esc(o.ends.trim())})` : "")],
    ["Exit fee", fee],
    ["Renews automatically", o.auto_renews ? "Yes — check the price it renews at" : "No"],
    ["Monthly fee", o.monthly_fee ? money(o.monthly_fee) : "None"],
    ["Sign-up fee", o.enrollment_fee ? money(o.enrollment_fee) : "None"],
    ["Renewable", o.renewable_pct != null ? `${Math.round(o.renewable_pct)}%` : "Not stated"],
    o.new_customers_only && ["Availability", "New customers only"],
    o.extras && ["Also includes", esc(o.extras)],
    o.phone && ["Phone", esc(o.phone)],
  ].filter(Boolean);
  const url = safeUrl(o.url);
  return `<div class="offer-detail">
    ${items.map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}
    <div class="acts">
      <button type="button" data-use="${esc(o.id)}">This is what I pay</button>
      ${url ? `<a class="btn" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Supplier's page ↗</a>` : ""}
    </div></div>`;
}

function renderMarket(d) {
  const cmp = d.comparison;
  const ok = d.status === "ok" && cmp && cmp.offers.length;
  $("market").hidden = !ok;
  $("whatif-section").hidden = !ok;
  if (!ok) {
    $("market-lede").innerHTML = {
      off: "Supplier comparison is switched off on this server.",
      "no-location": `We need your ZIP code to look up offers. `
        + `<input type="text" id="zip" inputmode="numeric" maxlength="5" placeholder="02144" style="width:90px"> `
        + `<button id="zip-go" type="button">Look up</button>`,
      unavailable: "The state's comparison site didn't answer. Your own numbers above are unaffected — try again later.",
      "no-state": `Tell us which state this home is in (Add bills → About this home) and, if there's a `
        + `comparison for it, the offers appear here. Your ZIP code is only ever sent to its own state's site.`,
      "not-covered": `Supplier comparison isn't available${d.state_name ? ` in ${esc(d.state_name)}` : " for this ZIP code"} yet. `
        + `Everything above still works — use “change” on the supply tile to enter your own supply price and see the split.`,
    }[d.status] || "There's nothing to compare yet.";
    const go = $("zip-go");
    if (go) go.addEventListener("click", () => update(url("/api/rates/supply"), { zip: $("zip").value }));
    return;
  }
  const m = cmp.market;
  const you = cmp.supply;
  $("market-lede").innerHTML =
    `Your supply is <strong>${cents(supply)}</strong> (${you.assumed ? `assumed from ${esc(you.name || "Basic Service")}` : "as you told us"}). `
    + `Of <strong>${m.count}</strong> other offers in ${esc(d.zip)}, `
    + `<strong>${m.cheaper_than_you}</strong> ${m.cheaper_than_you === 1 ? "is" : "are"} cheaper, the middle one is ${cents(m.median)}, `
    + `and they run from ${cents(m.min)} to ${cents(m.max)}. `
    + `Cheaper isn't always better — read the term and exit fee.`;
  renderOffers();
  setupWhatIf();
}

function setupWhatIf() {
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
  const kwhYear = data.comparison.kwh;
  const delta = (v - supply) / 100 * kwhYear;
  const allIn = data.now + (v - supply) / 100;
  $("whatif-out").innerHTML = `
    <div class="big">${cents(v)} <span style="font-weight:400;color:var(--text-muted);font-size:14px">supply</span></div>
    <div class="sub-line">All in: <b>${dollars(allIn)}/kWh</b> · over your last ${data.comparison.months} statements
      that's <b class="${delta < 0 ? "down" : delta > 0 ? "up" : ""}" style="color:${delta < 0 ? "var(--good-text)" : delta > 0 ? "var(--critical)" : "inherit"}">
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
    + "Leave empty to go back to the default.", supply ? supply.toFixed(2) : "");
  if (answer === null) return;
  update(url("/api/rates/supply"), { supply_cents: answer.trim() === "" ? null : answer });
}

function initEvents() {
  $("offers").addEventListener("click", (e) => {
    const use = e.target.closest("[data-use]");
    if (use) {
      const o = data.comparison.offers.find((x) => String(x.id) === use.dataset.use);
      if (o) update(url("/api/rates/supply"), { supply_cents: o.cents });
      return;
    }
    const row = e.target.closest(".offer-row");
    if (!row) return;
    const id = data.comparison.offers.find((x) => String(x.id) === row.dataset.id)?.id;
    open = open === id ? null : id;
    renderOffers();
  });
  for (const id of ["sort", "term", "f-green", "f-nofee", "f-existing"]) {
    $(id).addEventListener("change", renderOffers);
  }
  initTableToggles();
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
  supply = data.comparison ? data.comparison.supply.cents : null;
  renderTiles(data);
  renderSplit(data);
  renderCharts(data);
  renderMarket(data);

  const f = data.fetched_at && new Date(data.fetched_at);
  $("fineprint").innerHTML = `
    <p><strong>How to read this.</strong> Your statement shows one all-in price. Offers are quoted for supply only, so
    each one is priced by swapping its supply price for yours and keeping delivery exactly as is.</p>
    <p>Where your supply price isn't known it is assumed — your town's default program if it has one, otherwise the
    utility's Basic Service. If that isn't what you're on, say so with “change” above.</p>
    <p>Offers come from <a href="${esc(data.source.href)}" target="_blank" rel="noopener noreferrer">${esc(data.source.text)}</a>,
    the state's own comparison site${f ? `, as of ${f.toLocaleString()}` : ""}. Prices change and some offers are only open
    to new customers, so confirm the terms with the supplier before switching. Only your ZIP code is sent; your usage stays here.</p>`;
}

async function main() {
  initTheme(() => { if (data) renderCharts(data); });
  initEvents();
  try {
    data = await load(url("/api/rates"));
    render();
  } catch {
    $("loading").innerHTML = `<p class="note" style="margin:0">Couldn't load your rates. Try refreshing.</p>`;
  }
}

main();
