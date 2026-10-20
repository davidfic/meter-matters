/* One month close up: the theme, and a temperature trace for each statement that has one.
   The page itself is server-drawn. */

import { Lines } from "./charts.js";
import { initTheme } from "./ui.js";

initTheme();

for (const host of document.querySelectorAll(".temps[data-temps]")) {
  let days = [];
  try { days = JSON.parse(host.dataset.temps); } catch { continue; }
  if (!days.length) continue;
  new Lines(host, {
    height: 170,
    margin: { top: 12, right: 12, bottom: 28, left: 40 },
    data: days,
    series: [{ key: "temp", label: "Average temperature", color: "var(--series-3)", area: true }],
    format: (v) => `${v}°F`,
    tickFormat: (v) => `${v}°`,
  });
}
