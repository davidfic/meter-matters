/* The "add bills" page: uploading, and copying the forwarding address. */

import { url } from "./base.js";
import { $, esc, flash } from "./ui.js";

function describe(u) {
  const many = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const notes = (u.notes || []).map((n) => ` ${n}`).join("");
  if (u.kind === "error") return `${u.file}: ${u.error}`;
  if (u.kind === "unsure") return `${u.file}: ${u.reason}`;
  if (u.kind === "bills") {
    return (u.added
      ? `Added ${many(u.added, "new row")} of statement history.`
      : "That statement history was already on file.") + notes;
  }
  const span = `${many(u.periods, "metered period")}, ${u.first} to ${u.last}`;
  const made = u.bills_made ? ` Made ${many(u.bills_made, "bill")} from it, `
    + "since there was no bill history. Their due dates are estimates." : "";
  return (u.stored ? `Added usage: ${span}.` : `Already had that usage export (${span}).`) + made + notes;
}

/* ------------------------------------------------ "which column is which?" */

const ROLES = [
  ["start", "Period start"], ["end", "Period end"], ["date", "Date (one per row)"],
  ["due", "Due date"], ["billdate", "Statement date"], ["usage", "Amount used"],
  ["money", "Cost or amount ($)"], ["account", "Account (optional)"],
];
const UNITS = ["kWh", "MWh", "therms", "ccf", "mcf"];
const asking = [];                     // files waiting for an answer, one at a time

function pick(id, label, options, chosen, blank) {
  const opts = (blank ? [`<option value="">${esc(blank)}</option>`] : [])
    .concat(options.map((o) => `<option value="${esc(o)}"${o === chosen ? " selected" : ""}>${esc(o)}</option>`));
  return `<div class="field"><label for="${id}">${esc(label)}</label><select id="${id}">${opts.join("")}</select></div>`;
}

function askMapping() {
  const next = asking[0];
  $("mapping").hidden = !next;
  if (!next) return;
  const { file, q } = next;
  const guess = q.guess || {};
  const cols = guess.columns || {};
  $("mapping-reason").textContent = `${file.name}: ${q.reason}`;
  const head = q.headers.map((h) => `<th>${esc(h)}</th>`).join("");
  const body = q.sample.map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("");
  $("mapping-sample").innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  $("mapping-form").innerHTML =
    pick("m-kind", "This file lists", ["usage", "bills"], guess.kind, "Choose…")
    + pick("m-fuel", "Fuel", ["Electric", "Gas"], guess.fuel, "Choose…")
    + pick("m-unit", "Usage is measured in", UNITS, guess.unit, "Choose…")
    + ROLES.map(([role, label]) => pick(`m-${role}`, label, q.headers, cols[role], "—")).join("")
    + `<div class="acts"><button type="submit" class="primary">Import this file</button>
       <button type="button" id="m-skip">Skip it</button></div>`;
  $("mapping").scrollIntoView({ behavior: "smooth", block: "nearest" });
  $("m-skip").addEventListener("click", () => { asking.shift(); askMapping(); });
}

async function sendMapping(e) {
  e.preventDefault();
  const { file } = asking[0];
  const columns = {};
  for (const [role] of ROLES) {
    const v = $(`m-${role}`).value;
    if (v) columns[role] = v;
  }
  const mapping = { kind: $("m-kind").value, fuel: $("m-fuel").value, unit: $("m-unit").value, columns };
  asking.shift();
  askMapping();
  await upload([file], mapping);
}

async function upload(files, mapping = null) {
  const form = new FormData();
  for (const file of files) form.append("file", file);
  if (mapping) form.append("mapping", JSON.stringify(mapping));
  const zone = $("dropzone");
  zone.setAttribute("aria-busy", "true");
  let res, data;
  try {
    res = await fetch(url("/api/upload"), { method: "POST", body: form });
    data = await res.json();
  } catch {
    flash("Upload failed — the server didn't answer.", true);
    return;
  } finally {
    zone.removeAttribute("aria-busy");
  }
  if (!res.ok) { flash(data.error || "Upload failed.", true); return; }
  const uploads = data.uploads || [];
  uploads.forEach((u, i) => { if (u.kind === "unsure") asking.push({ file: files[i], q: u }); });
  askMapping();
  const failed = uploads.some((u) => u.kind === "error");
  const waiting = uploads.some((u) => u.kind === "unsure");
  flash(uploads.map(describe).join(" ")
    + ((data.series || []).length && !failed && !waiting ? " Opening your dashboard…" : ""), failed);
  if ((data.series || []).length && !failed && !waiting) setTimeout(() => { location.href = url("/"); }, 1600);
}

function initUpload() {
  $("mapping-form").addEventListener("submit", sendMapping);
  const zone = $("dropzone");
  const input = $("file");
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
  input.addEventListener("change", () => {
    if (input.files.length) upload([...input.files]);
    input.value = "";
  });
  for (const type of ["dragenter", "dragover"]) {
    zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.add("over"); });
  }
  for (const type of ["dragleave", "drop"]) {
    zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.remove("over"); });
  }
  zone.addEventListener("drop", (e) => {
    const files = [...(e.dataTransfer?.files || [])];
    if (files.length) upload(files);
  });
}

function initCopy() {
  const button = $("copy");
  if (!button) return;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      button.textContent = "Copied";
    } catch {
      // No clipboard access (plain http, or permission refused): select it instead.
      getSelection().selectAllChildren($("address"));
      button.textContent = "Press Ctrl+C";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 2000);
  });
}

initUpload();
initCopy();
