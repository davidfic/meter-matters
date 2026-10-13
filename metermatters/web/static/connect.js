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

/* ------------------------------------------------------ scans and photos */

const SCAN_TYPES = /\.(png|jpe?g|webp|gif|tiff?|pdf)$/i;
// Bigger than this and the server won't take it; a phone photo or a flatbed
// scan is shrunk in the browser first, which also keeps the upload quick.
const SCAN_LIMIT = 4 * 1024 * 1024;
const SCAN_SIDE = 2500;

function isScan(file) {
  return SCAN_TYPES.test(file.name) || /^image\//.test(file.type) || file.type === "application/pdf";
}

/* A smaller JPEG of an image, made in the browser: the text reader needs
   nothing larger, and a 400 MB scan would never upload. PDFs pass as they are. */
async function shrink(file) {
  if (file.type === "application/pdf" || /\.pdf$/i.test(file.name)) return file;
  let bitmap;
  try { bitmap = await createImageBitmap(file); } catch { return file; }
  const scale = Math.min(1, SCAN_SIDE / Math.max(bitmap.width, bitmap.height));
  if (scale === 1 && file.size <= SCAN_LIMIT) return file;
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise((ok) => canvas.toBlob(ok, "image/jpeg", 0.85));
  return new File([blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" });
}

function money(v) { return `$${Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`; }

/* Put what was read into the by-hand form and say what was and wasn't found. */
function showScan(r) {
  const box = $("scan-result");
  const set = (id, value) => { const el = $(id); if (el && value != null && value !== "") el.value = value; };
  set("bh-fuel", r.fuel); set("bh-due", r.due); set("bh-amount", r.amount);
  set("bh-used", r.used); set("bh-unit", r.unit); set("bh-start", r.start); set("bh-end", r.end);
  if (r.used != null || r.start || r.end) {
    const details = document.querySelector("#byhand details");
    if (details) details.open = true;
  }
  const got = [];
  if (r.fuel) got.push(r.fuel.toLowerCase());
  if (r.amount != null) got.push(`${money(r.amount)}${r.due ? ` due ${r.due}` : ""}`);
  if (r.used != null) got.push(`${Number(r.used).toLocaleString("en-US")} ${r.unit}${r.start && r.end ? ` from ${r.start} to ${r.end}` : ""}`);
  const missing = [["amount", r.amount == null], ["due date", !r.due], ["usage", r.used == null],
                   ["service dates", !(r.start && r.end)]].filter(([, m]) => m).map(([n]) => n);
  box.hidden = false;
  box.innerHTML = `<strong>Read from ${esc(r.file)}:</strong> ${esc(got.join(", ") || "nothing it recognised")}. `
    + (missing.length ? `Couldn't find the ${esc(missing.join(", the "))}: fill ${missing.length === 1 ? "it" : "them"} in. ` : "")
    + "Check each number against the bill, then press <strong>Add bill</strong>.";
  document.getElementById("byhand").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function sendScan(file) {
  const zone = $("dropzone");
  zone.setAttribute("aria-busy", "true");
  let res, data;
  try {
    const small = await shrink(file);
    if (small.size > SCAN_LIMIT) { flash(`${file.name} is too big to send - under 4 MB, please.`, true); return; }
    const form = new FormData();
    form.append("file", small, small.name);
    res = await fetch(url("/api/scan"), { method: "POST", body: form });
    data = await res.json();
  } catch {
    flash("Couldn't send the scan - the server didn't answer.", true);
    return;
  } finally {
    zone.removeAttribute("aria-busy");
  }
  if (!res.ok) { flash(data.error || "Couldn't read that scan.", true); return; }
  showScan(data);
}

async function upload(files, mapping = null) {
  const scans = files.filter(isScan);
  files = files.filter((f) => !isScan(f));
  for (const scan of scans) await sendScan(scan);
  if (!files.length) return;
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

/* ------------------------------------------------- "what has arrived" */

/* A bill forwarded while this page is open should appear without a reload.
   Every few seconds for a while after the page opens, then more slowly; a
   fresh confirmation from Gmail changes the box above the list, so that
   reloads the page. */
const LABELS = { imported: "Filed", duplicate: "Already had it", confirm: "Confirmation",
                 confirmed: "Confirmed", rejected: "Not filed", held: "Needs a home" };

function watchArrivals() {
  const box = $("arrivals");
  if (!box) return;
  let newest = Number(box.dataset.newest || 0);
  const started = Date.now();
  async function check() {
    if (document.hidden) return;
    let data;
    try {
      const res = await fetch(url("/connect/arrivals"), { headers: { Accept: "application/json" } });
      if (!res.ok) return;
      data = await res.json();
    } catch { return; }
    const rows = data.rows || [];
    if (!rows.length || rows[0].id === newest) return;
    if (rows.some((r) => r.id > newest && r.outcome === "confirm")) { location.reload(); return; }
    $("arrivals-list").innerHTML = rows.map((r) =>
      `<li class="${esc(r.outcome)}${r.id > newest ? " fresh" : ""}">
         <span class="when">${esc(r.received_at)}</span>
         <span class="what">${esc(LABELS[r.outcome] || r.outcome)}</span>
         <span class="why">${esc(r.detail)}</span></li>`).join("");
    newest = rows[0].id;
    box.hidden = false;
  }
  const tick = () => {
    check();
    const age = Date.now() - started;
    setTimeout(tick, age < 3 * 60 * 1000 ? 4000 : 30000);
  };
  setTimeout(tick, 4000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) check(); });
}

watchArrivals();
