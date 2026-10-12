/* The test report: filter, search, expand. Everything is already on the page; this only hides rows. */

const $ = (id) => document.getElementById(id);
const modules = [...document.querySelectorAll("[data-module]")];
const rows = [...document.querySelectorAll(".suite-tests li")];
let show = "all";

function apply() {
  const q = ($("suite-search")?.value || "").trim().toLowerCase();
  let any = false;
  for (const li of rows) {
    const status = li.dataset.status;
    const okStatus = show === "all" || (show === "bad" ? status === "failed" || status === "error" : status === show);
    const hit = okStatus && (!q || li.dataset.text.includes(q));
    li.hidden = !hit;
    any ||= hit;
  }
  for (const m of modules) {
    const visible = [...m.querySelectorAll(".suite-tests li")].some((li) => !li.hidden);
    m.hidden = !visible;
    // A filter that has narrowed things down is one you want to see the results of.
    if (visible && (q || show !== "all")) m.open = true;
    for (const h of m.querySelectorAll(".suite-case")) {
      h.hidden = !h.nextElementSibling || ![...h.nextElementSibling.children].some((li) => !li.hidden);
    }
  }
  if ($("suite-empty")) $("suite-empty").hidden = any;
}

for (const button of document.querySelectorAll("[data-show]")) {
  button.addEventListener("click", () => {
    show = button.dataset.show;
    for (const b of document.querySelectorAll("[data-show]")) b.setAttribute("aria-pressed", String(b === button));
    apply();
  });
}
$("suite-search")?.addEventListener("input", apply);

let open = false;
$("suite-toggle")?.addEventListener("click", () => {
  open = !open;
  for (const m of modules) m.open = open;
  $("suite-toggle").textContent = open ? "Collapse all" : "Expand all";
});

// Anything that failed is open from the start: that is what you came for.
for (const m of modules) {
  if ([...m.querySelectorAll("li")].some((li) => li.dataset.status === "failed" || li.dataset.status === "error")) m.open = true;
}

$("theme")?.addEventListener("click", () => {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const cur = document.documentElement.dataset.theme || (dark ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("elec-theme", next); } catch { /* private mode */ }
});
