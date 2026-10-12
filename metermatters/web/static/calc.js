/* "What's this costing me?" - a thing, how long it runs, at this home's own price.

   Presets are typical figures, not measurements of this home; the power field
   is editable so a nameplate can be typed in. Electric is watts and hours;
   gas is therms an hour (a furnace, a water heater); water is gallons a minute. */

import { $, esc, money } from "./ui.js";

const PRESETS = {
  Electric: [
    ["Space heater", 1500, 4], ["Window air conditioner", 1000, 8], ["Central AC", 3500, 6],
    ["Electric water heater", 4500, 2], ["Clothes dryer", 3000, 1], ["Oven", 2400, 1],
    ["Dishwasher", 1200, 1], ["Refrigerator (average draw)", 150, 24], ["Chest freezer", 90, 24],
    ["Desktop computer", 200, 8], ["Gaming console", 150, 3], ["Large TV", 100, 5],
    ["LED bulb", 10, 5], ["Incandescent bulb", 60, 5], ["Wi-Fi router", 10, 24],
    ["EV charging (Level 2)", 7000, 2], ["Dehumidifier", 500, 8], ["Pool pump", 1500, 8],
    ["Custom", 100, 1],
  ],
  Gas: [
    ["Furnace (running)", 0.8, 4], ["Gas water heater (running)", 0.4, 2], ["Gas range burner", 0.1, 1],
    ["Gas clothes dryer", 0.22, 1], ["Gas fireplace", 0.3, 2], ["Custom", 0.5, 1],
  ],
  Water: [
    ["Shower", 2.1, 10], ["Bath (filling)", 4, 10], ["Toilet flush", 1.6, 5],
    ["Dishwasher cycle", 4, 1], ["Washing machine load", 20, 1], ["Lawn sprinkler", 2.5, 30],
    ["Garden hose", 9, 15], ["Dripping tap", 0.05, 1440], ["Custom", 1, 10],
  ],
};
// What the "power" and "runs for" fields mean for each fuel.
const SHAPE = {
  Electric: { power: "watts", time: "hours a day", perUnit: (w, h) => (w / 1000) * h },
  Gas: { power: "therms an hour", time: "hours a day", perUnit: (t, h) => t * h },
  Water: { power: "gallons a minute", time: "minutes a day", perUnit: (g, m) => g * m },
};

let state = null;

export function init(report) {
  const section = $("calc-section");
  const price = report.summary && report.summary.rate_latest;
  const fuel = report.fuel;
  if (!price || !PRESETS[fuel]) { section.hidden = true; return; }
  section.hidden = false;
  state = { fuel, price, unit: report.series[report.series.length - 1].unit || "kWh",
            kg: report.carbon_factor || 0 };
  const shape = SHAPE[fuel];
  $("calc-preset").innerHTML = PRESETS[fuel].map(([name], i) =>
    `<option value="${i}">${esc(name)}</option>`).join("");
  $("calc-power-unit").textContent = shape.power;
  $("calc-hours-unit").textContent = shape.time;
  $("calc-hours").max = fuel === "Water" ? 1440 : 24;
  $("calc-hours").step = fuel === "Water" ? 1 : 0.25;
  $("calc-note").textContent = `At your current ${money(price, true).replace("$", "$")}/${state.unit}. Typical figures - type in the nameplate for your own.`;
  if (!section.dataset.ready) {
    section.dataset.ready = "1";
    $("calc-preset").addEventListener("change", pick);
    $("calc-power").addEventListener("input", draw);
    $("calc-hours").addEventListener("input", draw);
  }
  pick();
}

function pick() {
  const [, power, time] = PRESETS[state.fuel][Number($("calc-preset").value)];
  $("calc-power").value = power;
  $("calc-hours").value = time;
  draw();
}

function draw() {
  const shape = SHAPE[state.fuel];
  const perDay = shape.perUnit(Number($("calc-power").value) || 0, Number($("calc-hours").value) || 0);
  const perMonth = perDay * 30.4;
  const cost = perMonth * state.price;
  const kg = perMonth * state.kg;
  const unit = state.unit;
  $("calc-out").innerHTML = `
    <div class="calc-fig"><b>${money(cost)}</b><span>a month</span></div>
    <div class="calc-fig"><b>${money(cost * 12, false)}</b><span>a year</span></div>
    <div class="calc-fig"><b>${perMonth.toLocaleString("en-US", { maximumFractionDigits: perMonth < 10 ? 1 : 0 })} ${esc(unit)}</b><span>a month</span></div>
    ${kg > 0 ? `<div class="calc-fig"><b>${kg.toLocaleString("en-US", { maximumFractionDigits: 0 })} kg</b><span>of CO₂e a month</span></div>` : ""}`;
}
