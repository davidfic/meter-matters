/* Tabs for the page's panes. The address hash picks one (#add opens Tools). */
(() => {
  const tabs = [...document.querySelectorAll(".tab")];
  const panes = [...document.querySelectorAll(".pane")];
  if (!tabs.length) return;
  const alias = { add: "tools" };
  const names = tabs.map((t) => t.dataset.pane);
  const show = (name, push) => {
    if (!names.includes(name)) name = names[0];
    tabs.forEach((t) => { const on = t.dataset.pane === name; t.classList.toggle("on", on); t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1; });
    panes.forEach((p) => { p.hidden = p.dataset.pane !== name; });
    if (push) { try { history.replaceState(null, "", "#" + name); } catch {} }
  };
  const fromHash = () => { const h = location.hash.slice(1); return alias[h] || h; };
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => show(t.dataset.pane, true));
    t.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      const n = tabs[(i + d + tabs.length) % tabs.length];
      n.focus(); show(n.dataset.pane, true);
    });
  });
  addEventListener("hashchange", () => show(fromHash()));
  show(fromHash());
  if (location.hash === "#add") document.getElementById("add")?.scrollIntoView();
})();
