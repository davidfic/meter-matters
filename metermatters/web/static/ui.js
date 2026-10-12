/* What every page needs: the one copy of it.

   Text that reaches the page from a file someone uploaded - an account
   nickname, a status, a unit - is never trusted. `table` escapes every cell;
   a cell that deliberately holds markup says so by returning `raw(...)`. */

import { esc, money, signedMoney, Columns } from "./charts.js";

export { esc, money, signedMoney, Columns };
export const $ = (id) => document.getElementById(id);
export const pctText = (p) => (p > 0 ? "+" : p < 0 ? "−" : "") + Math.abs(p) + "%";
export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/* The utilities, in the app's standing order. `key` is the JSON/CSS form. */
export const FUELS = [
  { name: "Electric", key: "electric", noun: "electricity", color: "var(--series-1)" },
  { name: "Gas", key: "gas", noun: "gas", color: "var(--series-2)" },
  { name: "Water", key: "water", noun: "water", color: "var(--series-3)" },
];
export const fuelColor = (name) => (FUELS.find((f) => f.name === name) || FUELS[0]).color;
export const fuelNoun = (name) => (FUELS.find((f) => f.name === name) || { noun: String(name).toLowerCase() }).noun;
/* The fuels that have any money in these monthly rows, in standing order. */
export const fuelsIn = (rows) => FUELS.filter((f) => rows.some((r) => r[f.key]));

/* Markup a cell means to be markup. Anything else is text. */
export const raw = (html) => ({ __html: String(html) });
const cell = (v) => (v && typeof v === "object" && "__html" in v) ? v.__html : esc(v ?? "");

export function table(columns, rows) {
  const head = columns.map((c) => `<th>${esc(c.head)}</th>`).join("");
  const body = rows
    .map((r) => "<tr>" + columns.map((c) => `<td>${cell(c.cell(r))}</td>`).join("") + "</tr>")
    .join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

export function tiles(host, items) {
  host.innerHTML = items.map((t) => `<div class="card tile">
      <div class="label">${t.color ? `<span class="key" style="background:${esc(t.color)}"></span>` : ""}${esc(t.label)}</div>
      <div class="value">${esc(t.value)}</div>
      <div class="foot">${cell(t.foot)}</div></div>`).join("");
}

/* A message the person sees wherever they are on the page: #flash is sticky. */
let flashTimer = null;
export function flash(message, isError = false) {
  const box = $("flash");
  if (!box) return;
  clearTimeout(flashTimer);
  if (!message) { box.hidden = true; return; }
  box.hidden = false;
  box.className = "flash" + (isError ? " err" : "");
  box.textContent = message;
  if (!isError) flashTimer = setTimeout(() => { box.hidden = true; }, 8000);
}

/* fetch, with the one thing every caller forgot: a non-2xx is a failure. */
export async function fetchJSON(target, options) {
  let res;
  try {
    res = await fetch(target, options);
  } catch {
    throw new Error("The server didn't answer.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw Object.assign(new Error(data.error || `Request failed (${res.status}).`),
                        { status: res.status, data });
  }
  return data;
}

export const postJSON = (target, body) =>
  fetchJSON(target, { method: "POST", headers: { "Content-Type": "application/json" },
                      body: JSON.stringify(body) });

/* Charts read CSS variables when drawn, so a page passes what to redraw. */
export function initTheme(onChange) {
  const stored = (() => { try { return localStorage.getItem("elec-theme"); } catch { return null; } })();
  if (stored) document.documentElement.dataset.theme = stored;
  const button = $("theme");
  if (!button) return;
  const isDark = () => (document.documentElement.dataset.theme
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";
  button.setAttribute("aria-pressed", String(isDark()));
  button.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    button.setAttribute("aria-pressed", String(next === "dark"));
    try { localStorage.setItem("elec-theme", next); } catch { /* private mode */ }
    if (onChange) onChange();
  });
}

export function initTableToggles() {
  for (const btn of document.querySelectorAll(".table-toggle")) {
    btn.addEventListener("click", () => {
      const target = $(btn.dataset.table);
      target.hidden = !target.hidden;
      btn.textContent = target.hidden ? "Table view" : "Hide table";
      btn.setAttribute("aria-expanded", String(!target.hidden));
    });
  }
}

/* Render the payload the server inlined; only ask the network when it isn't there. */
export async function boot(paint, fallbackUrl, isUsable = (d) => !!d) {
  const node = $("bootstrap");
  if (node) {
    try {
      const data = JSON.parse(node.textContent);
      if (isUsable(data)) { paint(data); return; }
    } catch { /* fall through to the network */ }
  }
  try {
    paint(await fetchJSON(fallbackUrl));
  } catch (err) {
    flash(err.message, true);
  }
}
