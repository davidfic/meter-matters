/* The raw-data explorer: filter, sort and export every record the app holds.
   Deliberately dumb about meaning - it shows what's there, nothing inferred. */

import { url } from "./base.js";
import { $, esc, initTheme } from "./ui.js";


const COLUMNS = {
  statements: [
    { key: "date", head: "Date" },
    { key: "fuel", head: "Fuel" },
    { key: "account", head: "Account" },
    { key: "type", head: "Type" },
    { key: "amount", head: "Amount", num: true, fmt: (v) => v == null ? "—" : `$${v.toFixed(2)}` },
    { key: "status", head: "Status" },
    { key: "source", head: "Source" },
    { key: "verified", head: "Verified", fmt: (v) => (v ? "✓" : "") },
    { key: "service_start", head: "Metered from" },
    { key: "service_end", head: "Metered to" },
    { key: "days", head: "Days", num: true },
    // Electric is metered in kWh, gas in therms - the unit rides with the row.
    { key: "kwh", head: "Usage", num: true,
      fmt: (v, r) => v == null ? "—" : `${v.toLocaleString("en-US")} ${r.unit || ""}`.trim() },
    { key: "kwh_per_day", head: "Per day", num: true,
      fmt: (v, r) => v == null ? "—" : `${v} ${r.unit || ""}`.trim() },
    { key: "rate", head: "Unit price", num: true,
      fmt: (v, r) => v == null ? "—" : `$${v.toFixed(4)}/${r.unit || "unit"}` },
  ],
  usage: [
    { key: "end", head: "Read date" },
    { key: "start", head: "Period from" },
    { key: "days", head: "Days", num: true },
    { key: "account", head: "Account" },
    { key: "kwh", head: "Usage", num: true,
      fmt: (v, r) => v == null ? "—" : `${v.toLocaleString("en-US")} ${r.unit || ""}`.trim() },
    { key: "kwh_per_day", head: "Per day", num: true,
      fmt: (v, r) => v == null ? "—" : `${v} ${r.unit || ""}`.trim() },
    { key: "cost", head: "Cost", num: true, fmt: (v) => v == null ? "—" : `$${v.toFixed(2)}` },
    { key: "rate", head: "Unit price", num: true,
      fmt: (v, r) => v == null ? "—" : `$${v.toFixed(4)}/${r.unit || "unit"}` },
    { key: "avg_temp", head: "Avg °F", num: true, fmt: (v) => v == null ? "—" : v.toFixed(0) },
    { key: "read_type", head: "Read type" },
    { key: "source", head: "File" },
  ],
};

const DATE_KEY = { statements: "date", usage: "end" };

let data = { statements: [], usage: [] };
let sort = { key: "date", dir: -1 };


function dataset() { return $("f-dataset").value; }

/* Rebuild the dropdowns from whatever the current dataset actually contains,
   so you can never filter to an empty set by picking a stale option. */
function fillFilters() {
  const rows = data[dataset()] || [];
  const opts = (id, key, label) => {
    const sel = $(id);
    const prev = sel.value;
    const values = [...new Set(rows.map((r) => r[key]).filter((v) => v != null && v !== ""))].sort();
    sel.innerHTML = `<option value="">All ${label}</option>`
      + values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
    sel.value = values.includes(prev) ? prev : "";
    sel.parentElement.hidden = values.length < 2;
  };
  opts("f-fuel", "fuel", "fuels");
  opts("f-account", "account", "accounts");
  opts("f-source", "source", dataset() === "usage" ? "files" : "sources");
}

function filtered() {
  const set = dataset();
  const rows = data[set] || [];
  const q = $("q").value.trim().toLowerCase();
  const fuel = $("f-fuel").value, account = $("f-account").value, source = $("f-source").value;
  const from = $("f-from").value, to = $("f-to").value;
  const dk = DATE_KEY[set];

  const out = rows.filter((r) => {
    if (fuel && r.fuel !== fuel) return false;
    if (account && r.account !== account) return false;
    if (source && r.source !== source) return false;
    if (from && r[dk] < from) return false;
    if (to && r[dk] > to) return false;
    if (q && !Object.values(r).some((v) => String(v ?? "").toLowerCase().includes(q))) return false;
    return true;
  });

  const col = COLUMNS[set].find((c) => c.key === sort.key);
  out.sort((a, b) => {
    const x = a[sort.key], y = b[sort.key];
    if (x == null && y == null) return 0;
    if (x == null) return 1;          // blanks sink, whichever way you sort
    if (y == null) return -1;
    const cmp = col && col.num ? x - y : String(x).localeCompare(String(y));
    return cmp * sort.dir;
  });
  return out;
}

/* Columns after "Verified" are all derived by joining a bill to a metered
   period. A fuel with no usage export loaded has nothing to join to, so those
   cells are blank for a structural reason rather than a missing value. */
function coverageNote() {
  if (dataset() !== "statements") return "";
  const bills = (data.statements || []).filter((r) => r.type === "Current Bill");
  const gaps = [...new Set(bills.map((r) => r.fuel))]
    .map((f) => {
      const all = bills.filter((r) => r.fuel === f);
      return { fuel: f, with: all.filter((r) => r.kwh != null).length, all: all.length };
    })
    .filter((x) => x.with < x.all);
  if (!gaps.length) return "";
  const disc = (data.discrepancies || []).length;
  const discNote = disc ? ` · <strong>${disc}</strong> metered period`
    + `${disc === 1 ? "" : "s"} had a charge that differs from what the bill asked for — `
    + `switch to the usage dataset and compare, the export is the authoritative side.` : "";
  if (!gaps.length) return discNote;
  const parts = gaps.map((x) => x.with === 0
    ? `<strong>${x.fuel.toLowerCase()}</strong> has no usage export loaded, so all ${x.all} of its statements lack them`
    : `<strong>${x.fuel.toLowerCase()}</strong> has them on ${x.with} of ${x.all}`);
  return ` · Usage, period and unit-price columns come from the utility's usage exports — `
    + parts.join("; ") + ". Download that account's billing export to fill them in." + discNote;
}

function render() {
  const set = dataset();
  const cols = COLUMNS[set];
  const rows = filtered();
  const total = (data[set] || []).length;

  $("count").innerHTML = (rows.length === total
    ? `${total} rows`
    : `${rows.length} of ${total} rows`) + coverageNote();

  const head = cols.map((c) => {
    const active = sort.key === c.key ? (sort.dir > 0 ? " ▲" : " ▼") : "";
    return `<th data-key="${c.key}" style="cursor:pointer;user-select:none">${c.head}${active}</th>`;
  }).join("");

  const body = rows.map((r) => "<tr>" + cols.map((c) => {
    const v = r[c.key];
    const text = esc(c.fmt ? c.fmt(v, r) : (v == null || v === "" ? "—" : v));
    return `<td>${text}</td>`;
  }).join("") + "</tr>").join("");

  $("grid").innerHTML = rows.length
    ? `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`
    : `<p class="note">Nothing matches those filters.</p>`;

  for (const th of $("grid").querySelectorAll("th")) {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      sort = { key, dir: sort.key === key ? -sort.dir : (COLUMNS[set].find((c) => c.key === key)?.num ? -1 : 1) };
      render();
    });
  }
}

function download() {
  const set = dataset();
  const cols = COLUMNS[set];
  const rows = filtered();
  // Raw values, not the display formatting - this is meant for a spreadsheet.
  const cell = (v) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [cols.map((c) => cell(c.head)).join(",")]
    .concat(rows.map((r) => cols.map((c) => cell(r[c.key])).join(",")))
    .join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `elec-${set}-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}


function paint() {
  const s = data.statements || [];
  $("coverage").textContent =
    `${s.length} records · ${(data.usage || []).length} metered periods · from ${data.source || "CSV"}`;
  $("files").textContent = (data.usage_files || []).length
    ? `Usage exports: ${data.usage_files.join(", ")}`
    : "No usage exports loaded.";
  fillFilters();
  render();
}

async function load() {
  const node = $("bootstrap");
  if (node) {
    try {
      data = JSON.parse(node.textContent);
      paint();
      return;
    } catch { /* fall through to the network */ }
  }
  const res = await fetch(url("/api/raw"));
  data = await res.json();
  paint();
}

initTheme();
$("f-dataset").addEventListener("change", () => {
  sort = { key: DATE_KEY[dataset()], dir: -1 };
  fillFilters();
  render();
});
for (const id of ["q", "f-fuel", "f-account", "f-source", "f-from", "f-to"]) {
  $(id).addEventListener("input", render);
}
$("reset").addEventListener("click", () => {
  for (const id of ["q", "f-from", "f-to"]) $(id).value = "";
  for (const id of ["f-fuel", "f-account", "f-source"]) $(id).value = "";
  render();
});
$("download").addEventListener("click", download);

load();
