/* Dashboard controller: fetch the report, render every panel, handle input. */

import { url } from "./base.js";
import { LineChart, DeltaBars, Columns, GroupedDeltaBars, StackedColumns } from "./charts.js";
import * as insights from "./insights.js";
import * as calc from "./calc.js";
import * as compare from "./compare.js";
import { $, esc, raw, table, flash, money, signedMoney, pctText, fetchJSON, postJSON,
         fuelsIn, fuelNoun, initTheme, initTableToggles } from "./ui.js";

const charts = {};
let report = null;

/* --------------------------------------------------------------- icons */

const ICONS = {
  scissors: '<path d="M6 3l12 12M18 3L6 15" /><circle cx="5" cy="18" r="2.6"/><circle cx="19" cy="18" r="2.6"/>',
  bolt: '<path d="M13 2L4.5 13.5H11l-1 8.5 9-12h-6.5z"/>',
  flame: '<path d="M12 2c3 4 6 6 6 10a6 6 0 1 1-12 0c0-2 1-3.5 2-5 .5 1.5 1.5 2 2.5 2C10 7 11 4 12 2z"/>',
  trophy: '<path d="M7 4h10v5a5 5 0 0 1-10 0z"/><path d="M7 5H4v2a3 3 0 0 0 3 3M17 5h3v2a3 3 0 0 1-3 3"/><path d="M10 14h4v3h-4zM8 20h8"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  target: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/>',
  piggy: '<path d="M4 12a7 7 0 0 1 7-7h3a7 7 0 0 1 7 7v1a5 5 0 0 1-3 4.6V21h-3v-2h-5v2H7v-3.4A6 6 0 0 1 4 13z"/><circle cx="16" cy="11" r="1"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
};

const icon = (name) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ICONS.bolt}</svg>`;

/* ----------------------------------------------------------- utilities */

const unitAmount = (n, unit = "kWh") =>
  Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 }) + " " + unit;
/* Electric is metered in kWh, gas in therms; the report carries which. */
const unitOf = (r) => (r.tariff && r.tariff.unit)
  || (r.baseload && r.baseload.unit)
  || (r.rates && r.rates.unit) || "kWh";
const kwh = (n) => unitAmount(n);
const rateText = (r, unit = "kWh") => "$" + Number(r).toFixed(4) + "/" + unit;

/* -------------------------------------------------------------- panels */

function renderHero(r) {
  const s = r.summary;
  const latest = s.latest;
  $("coverage").textContent = `${r.series[0].label} – ${latest.label} · ${r.series.length} statements`
    + (r.merged_accounts ? ` · ${r.accounts.length} accounts, same address` : "");
  $("hero-label").textContent = latest.paid
    ? `Latest statement · ${latest.label}`
    : `Current statement · ${latest.label} · due ${latest.due}`;
  $("hero-figure").textContent = money(latest.amount);

  const d = latest.delta;
  const pill = $("hero-delta");
  if (d === null) {
    pill.innerHTML = "";
  } else {
    const cls = d < 0 ? "good" : d > 0 ? "bad" : "flat";
    const arrow = d < 0 ? "▼" : d > 0 ? "▲" : "■";
    pill.innerHTML =
      `<span class="delta-pill ${cls}"><span class="dot"></span>${arrow} ${signedMoney(d)} (${pctText(latest.pct)}) vs ${s.previous.label}</span>`;
  }

  const bits = [];
  if (s.is_record_low) bits.push("🏆 Lowest statement on record");
  bits.push(`12-month average ${money(s.avg_12)}`);
  if (s.goal) bits.push(latest.amount <= s.goal ? `Goal of ${money(s.goal)} met` : `Goal ${money(s.goal)} — ${money(latest.amount - s.goal)} over`);
  $("hero-meta").innerHTML = bits.join(" · ");
  renderForecast(r.forecast);

  // Streak - on the most honest basis there is for this home.
  const streak = s.streak;
  const basis = {
    weather: ["below the same weeks last year, with the weather taken out",
              "A statement below last year's, once the weather is taken out, starts one."],
    usage: ["used less than the statement before",
            "A statement that uses less than the one before starts one."],
    dollars: ["came in lower than the one before",
              `A statement lower than <strong>${money(s.previous ? s.previous.amount : s.latest.amount)}</strong> starts one.`],
  }[s.streak_basis || "dollars"];
  $("streak-count").textContent = streak > 0 ? `🔥${streak}` : "0";
  $("streak-copy").innerHTML = streak
    ? `<strong>${streak} statement${streak === 1 ? "" : "s"} in a row</strong> ${basis[0]}. Best run: ${s.best_streak}.`
    : `No active streak. ${basis[1]}`;

  const pips = r.series.slice(-12).map((row) =>
    `<span class="pip ${row.improved ? "on" : ""}" title="${row.label}"></span>`).join("");
  $("streak-pips").innerHTML = pips;

  // The dollar streak can be flattered by a rate cut; the kWh one cannot.
  const us = s.usage_streak;
  $("streak-usage").innerHTML = s.usage_count
    ? (us > 0
        ? `⚡ <strong>${us}</strong> of those used fewer kWh too — that part is genuinely you.`
        : `⚠️ No kWh streak: the last statement cost less but didn't use less.`)
    : "";
}

function renderForecast(f) {
  const box = $("hero-forecast");
  box.hidden = !f;
  if (!f) return;
  const when = f.due ? ` around ${f.due}` : "";
  const how = f.basis === "usage"
    ? "the same weeks last year, scaled by how you've been running lately, at today's price"
    : "the same month last year, scaled by your last three statements";
  box.innerHTML = `Next statement: about <strong>${money(f.amount, false)}</strong>${when}
    <span class="range">(${money(f.low, false)}–${money(f.high, false)})</span>
    <span class="note">— from ${how}${f.backtested ? `; this way of guessing has typically been ${f.typical_miss_pct}% out` : ""}.</span>`;
}

function renderTiles(r) {
  const s = r.summary;
  const tiles = [
    {
      label: "Last 12 statements",
      value: money(s.total_12, false),
      foot: `Averaging ${money(s.avg_12)} each`,
    },
    s.kwh_12 != null
      ? {
          // The unit follows the fuel, and the latest statement may not have a
          // metered period yet - the export always lags the bill.
          label: `${fuelNoun(r.fuel).replace(/^./, (c) => c.toUpperCase())} used (12 statements)`,
          value: unitAmount(s.kwh_12, unitOf(r)),
          foot: s.kwh_latest != null && s.latest.days
            ? `Latest statement: ${unitAmount(s.kwh_latest, unitOf(r))} over ${s.latest.days} days`
            : `Latest statement has no metered period yet — re-export to fill it in`,
        }
      : {
          label: "12-month running average",
          value: money(s.avg_12),
          foot: `${24 - s.count} more statement${24 - s.count === 1 ? "" : "s"} until a full year-on-year compare`,
        },
    s.rate_latest != null
      ? {
          label: "Rate you're paying now",
          value: rateText(s.rate_latest, unitOf(r)),
          foot: `Ranged ${rateText(s.rate_min, unitOf(r))} – ${rateText(s.rate_max, unitOf(r))} while tracked`,
        }
      : {
          label: "Lowest statement",
          value: money(s.lowest.amount),
          foot: `${s.lowest.label}${s.is_record_low ? " · that's this one" : ""}`,
        },
    {
      label: "Banked vs baseline",
      value: money(r.banked),
      foot: `Baseline ${money(r.baseline)} · first ${r.baseline_months} statements`,
    },
  ];

  $("tiles").innerHTML = tiles
    .map((t) => `<div class="card tile">
        <div class="label">${t.label}</div>
        <div class="value">${t.value}</div>
        <div class="foot">${t.foot}</div>
      </div>`)
    .join("");
}

function renderLevel(r) {
  const lv = r.scoreboard.level;
  $("level-title").textContent = `Level ${lv.level} · ${lv.title}`;
  $("level-num").textContent = `${lv.level} of ${lv.max_level}`;
  const pill = $("banked-pill");
  pill.className = "delta-pill " + (lv.deficit > 0 ? "bad" : "good");
  $("banked-text").textContent = lv.deficit > 0
    ? `${money(lv.deficit)} over baseline`
    : `${money(lv.banked)} banked`;
  $("level-meter").style.width = `${Math.round(lv.progress * 100)}%`;
  $("level-floor").textContent = money(lv.floor, false);
  $("level-ceiling").textContent = lv.ceiling === null ? "max level" : money(lv.ceiling, false);
  $("level-note").textContent = lv.deficit > 0
    ? `You're ${money(lv.deficit)} above your ${money(r.baseline)} baseline year, so the ladder hasn't started. `
      + `Levelling up means getting back under it — the usage streak is how.`
    : lv.next_title
      ? `${money(lv.remaining)} more banked to reach ${lv.next_title}.`
      : "You've topped the ladder. Impressive.";

  // Fill the rest of the card with what's already in the trophy case.
  const earned = r.scoreboard.badges.filter((b) => b.earned);
  $("earned-strip").innerHTML = earned.length
    ? earned.map((b) => `<span class="chip">${icon(b.icon)}${b.name}</span>`).join("")
    : `<span class="chip">Nothing unlocked yet — one lower statement starts it off.</span>`;
}

function badgeCard(b) {
  return `<div class="badge ${b.earned ? "earned" : ""}">
    <div class="icon">${icon(b.icon)}</div>
    <div style="min-width:0">
      <div class="name">${b.earned ? "" : "🔒 "}${b.name}</div>
      <div class="blurb">${b.blurb}</div>
      <div class="detail">${b.detail}${b.earned && b.earned_on ? ` · ${b.earned_on}` : ""}</div>
      ${b.earned ? "" : `<div class="mini-meter"><span style="width:${Math.round(b.progress * 100)}%"></span></div>`}
    </div>
  </div>`;
}

function renderBadges(r) {
  const sb = r.scoreboard;
  $("badge-count").textContent = `${sb.earned_count} of ${sb.total_count} unlocked`;
  const scope = $("badge-scope");
  if (scope) {
    scope.textContent = r.scored_on_all
      ? ""
      : `Scored on your last ${r.game_window} statements (from ${
          r.game_series[0].label}) — measuring today against the prices of years ago would make this unwinnable.`;
  }
  $("badges").innerHTML = sb.badges.map(badgeCard).join("");
  $("next-up").innerHTML = sb.next_up.length
    ? sb.next_up.map(badgeCard).join("")
    : `<p class="note">Everything is unlocked. Nothing left to chase.</p>`;
}

function renderCharts(r) {
  const s = r.summary;

  // 1. Trend - single series, so no legend box; the title names it.
  charts.trend?.destroy();
  charts.trend = new LineChart($("chart-trend"), {
    height: 280,
    data: r.series.map((row) => ({ label: row.short, value: row.amount, row })),
    refValue: s.avg_12,
    refLabel: "12-mo avg",
    tooltip: (d) => `<div class="t-title">${d.row.label}</div>
      <div class="t-row"><span>Bill</span><b>${money(d.value)}</b></div>
      ${d.row.delta === null ? "" :
        `<div class="t-row"><span>vs prior</span><b>${signedMoney(d.row.delta)}</b></div>`}
      ${d.row.kwh == null ? "" :
        `<div class="t-row"><span>Used</span><b>${unitAmount(d.row.kwh, d.row.unit || "kWh")}</b></div>
         ${d.row.rate == null ? "" : `<div class="t-row"><span>Rate</span><b>$${
           Number(d.row.rate).toFixed(4)}/${d.row.unit || "kWh"}</b></div>`}
         <div class="t-row"><span>Metered</span><b>${esc(d.row.service_label)}</b></div>`}
      <div class="t-row"><span>Status</span><b>${d.row.paid ? "Paid" : "Due " + esc(d.row.due)}</b></div>`,
  });
  const all = r.series;
  const hi = all.reduce((a2, b) => (b.amount > a2.amount ? b : a2));
  const lo = all.reduce((a2, b) => (b.amount < a2.amount ? b : a2));
  $("trend-note").textContent =
    `${all.length} statements · high ${money(hi.amount)} (${hi.label}) · low ${money(lo.amount)} (${lo.label})`;
  $("trend-table").classList.toggle("scroll", r.series.length > 24);
  $("trend-table").innerHTML = table(
    [
      { head: "Statement", cell: (x) => x.label },
      { head: "Due", cell: (x) => x.due },
      { head: "Amount", cell: (x) => money(x.amount) },
      { head: "vs prior", cell: (x) => x.delta === null ? "—" :
          `<span class="${x.delta < 0 ? "down" : "up"}">${signedMoney(x.delta)}</span>` },
    ],
    r.series,
  );

  // 2. Month-over-month deltas.
  const deltas = r.series.filter((x) => x.delta !== null);
  charts.delta?.destroy();
  charts.delta = new DeltaBars($("chart-delta"), {
    height: 240,
    data: deltas.map((x) => ({ label: x.short, value: x.delta, amount: x.amount, pct: x.pct })),
  });
  $("delta-table").classList.toggle("scroll", deltas.length > 24);
  $("delta-table").innerHTML = table(
    [
      { head: "Statement", cell: (x) => x.label },
      { head: "Amount", cell: (x) => money(x.amount) },
      { head: "Change", cell: (x) => raw(`<span class="${x.delta < 0 ? "down" : "up"}">${signedMoney(x.delta)}</span>`) },
      { head: "%", cell: (x) => raw(`<span class="${x.delta < 0 ? "down" : "up"}">${pctText(x.pct)}</span>`) },
    ],
    deltas,
  );

  // 3. Cumulative savings against the fixed baseline.
  // The running total can be negative, and calling that "banked" would be a lie.
  const banked = r.banked ?? 0;
  $("banked-title").textContent = banked < 0 ? "Running total vs baseline" : "Money banked";
  $("banked-note").textContent = banked < 0
    ? `You're ${money(-banked)} above where your ${money(r.baseline)} baseline `
      + `(the first ${r.baseline_months} scored statements) would have put you.`
    : `Running total saved against your ${money(r.baseline)} baseline (the first ${r.baseline_months} statements).`;
  charts.banked?.destroy();
  if (r.savings.length) {
    charts.banked = new LineChart($("chart-banked"), {
      height: 240,
      data: r.savings.map((x) => ({ label: x.short, value: x.cumulative, row: x })),
      tooltip: (d) => `<div class="t-title">${d.row.label}</div>
        <div class="t-row"><span>Saved this month</span><b>${signedMoney(d.row.saved)}</b></div>
        <div class="t-row"><span>Running total</span><b>${money(d.value)}</b></div>`,
    });
    $("banked-table").innerHTML = table(
      [
        { head: "Statement", cell: (x) => x.label },
        { head: "Saved", cell: (x) => raw(`<span class="${x.saved >= 0 ? "down" : "up"}">${signedMoney(x.saved)}</span>`) },
        { head: "Running total", cell: (x) => raw(`<span class="${x.cumulative >= 0 ? "down" : "up"}">${signedMoney(x.cumulative)}</span>`) },
      ],
      r.savings,
    );
  } else {
    $("chart-banked").innerHTML =
      `<p class="note">Savings start accruing once you have more statements than the baseline window.</p>`;
  }

  // 4. Seasonal profile - one series, one colour; the current month is highlighted.
  const thisMonth = s.latest.month;
  charts.season?.destroy();
  charts.season = new Columns($("chart-season"), {
    height: 240,
    data: r.seasonal.map((x) => ({
      label: x.month_name, value: x.avg, count: x.count, dim: x.month !== thisMonth,
    })),
    tooltip: (d) => `<div class="t-title">${d.label}</div>
      <div class="t-row"><span>Average</span><b>${money(d.value)}</b></div>
      <div class="t-row"><span>Statements</span><b>${d.count}</b></div>`,
  });
  $("season-table").innerHTML = table(
    [
      { head: "Month", cell: (x) => x.month_name },
      { head: "Average", cell: (x) => money(x.avg) },
      { head: "Statements", cell: (x) => x.count },
    ],
    r.seasonal,
  );
}

function renderUsage(r) {
  const section = $("usage-section");
  const rows = r.series.filter((x) => x.kwh != null);
  if (!r.has_usage || rows.length < 2) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const unit = unitOf(r);
  const t = $("usage-title");
  if (t) t.textContent = `${fuelNoun(r.fuel).replace(/^./, (c) => c.toUpperCase())} actually used`;
  const n = $("usage-note");
  const sent = rows.reduce((s, x) => s + (x.exported || 0), 0);
  if (n) n.textContent = `${unit} per day, so billing periods of different lengths `
    + `compare fairly. This is the number you control.`
    + (sent ? ` Your meter runs both ways: ${Math.round(sent).toLocaleString("en-US")} ${unit} went back to the grid over these statements, kept apart from what you used.` : "");

  // 1. Usage per day - normalised so a 34-day period doesn't look like a spike.
  charts.usage?.destroy();
  charts.usage = new LineChart($("chart-usage"), {
    height: 240,
    data: rows.map((x) => ({ label: x.short, value: x.kwh_per_day, row: x })),
    format: (v) => `${v.toFixed(1)} ${rows[rows.length - 1].unit || "kWh"}/d`,
    tickFormat: (v) => `${Math.round(v)}`,
    tooltip: (d) => `<div class="t-title">${d.row.label}</div>
      <div class="t-row"><span>Used</span><b>${Number(d.row.kwh).toLocaleString("en-US")} ${d.row.unit || "kWh"}</b></div>
      <div class="t-row"><span>Per day</span><b>${d.value.toFixed(1)} ${d.row.unit || "kWh"}</b></div>
      <div class="t-row"><span>Period</span><b>${d.row.days} days</b></div>
      <div class="t-row"><span>Metered</span><b>${d.row.service_label}</b></div>`,
  });
  $("usage-table").innerHTML = table(
    [
      { head: "Statement", cell: (x) => x.label },
      { head: "Metered", cell: (x) => x.service_label },
      { head: unit, cell: (x) => Math.round(x.kwh).toLocaleString("en-US") },
      { head: "Days", cell: (x) => x.days },
      { head: `${unit}/day`, cell: (x) => x.kwh_per_day.toFixed(unit === "kWh" ? 1 : 2) },
      ...(sent ? [{ head: "Sent to grid", cell: (x) => Math.round(x.exported || 0).toLocaleString("en-US") }] : []),
      // A period in which nothing was used has no price per unit.
      { head: `$/${unit}`, cell: (x) => (x.rate == null ? "—" : x.rate.toFixed(4)) },
    ],
    rows,
  );

  // 2. The exact split of each change into usage and rate.
  const split = r.series.filter((x) => x.usage_effect != null);
  charts.split?.destroy();
  if (split.length) {
    charts.split = new GroupedDeltaBars($("chart-split"), {
      height: 240,
      series: [
        { key: "usage_effect", label: "Your usage", color: "var(--series-1)" },
        { key: "rate_effect", label: "The rate", color: "var(--series-2)" },
      ],
      data: split.map((x) => ({
        label: x.short, usage_effect: x.usage_effect,
        rate_effect: x.rate_effect, total: x.delta,
      })),
    });
    $("split-table").innerHTML = table(
      [
        { head: "Statement", cell: (x) => x.label },
        { head: "Bill change", cell: (x) =>
            `<span class="${x.delta < 0 ? "down" : "up"}">${signedMoney(x.delta)}</span>` },
        { head: "…from usage", cell: (x) =>
            `<span class="${x.usage_effect < 0 ? "down" : "up"}">${signedMoney(x.usage_effect)}</span>` },
        { head: "…from rate", cell: (x) =>
            `<span class="${x.rate_effect < 0 ? "down" : "up"}">${signedMoney(x.rate_effect)}</span>` },
      ],
      split,
    );
  }
}

function renderAnomalies(r) {
  const rows = r.anomalies || [];
  $("anomaly-section").hidden = rows.length === 0;
  if (!rows.length) return;
  $("anomaly-note").textContent =
    `${rows.length} statement${rows.length === 1 ? "" : "s"} in the scored window sit well outside `
    + `the same month in nearby years. Usage comparisons ignore the rate, so these are about how the home ran.`;
  $("anomaly-list").innerHTML = table(
    [
      { head: "Statement", cell: (x) => x.label },
      { head: "Compared on", cell: (x) => x.basis },
      { head: "Actual", cell: (x) => x.basis === "usage" ? `${x.actual} ${x.unit}` : money(x.actual) },
      { head: "Typical", cell: (x) => x.basis === "usage" ? `${x.expected} ${x.unit}` : money(x.expected) },
      { head: "Difference", cell: (x) =>
          raw(`<span class="${x.high ? "up" : "down"}">${pctText(x.pct)}</span>`) },
      { head: "Bill", cell: (x) => money(x.amount) },
    ],
    rows,
  );
}

function renderBaseload(r) {
  const b = r.baseload;
  const rt = r.rates;
  $("baseload-section").hidden = !(b || rt);
  if (b) {
    $("baseload-note").innerHTML =
      `Your quietest stretch still used <strong>${b.kwh_per_day} ${b.unit || "kWh"}/day</strong> — `
      + `${b.share}% of your average, about <strong>${money(b.monthly_cost)}/month</strong> `
      + `(${money(b.annual_cost, false)}/year) before you switch anything on deliberately. `
      + `That's an upper bound; only interval data can pin it exactly.`;
    charts.baseload?.destroy();
    charts.baseload = new StackedColumns($("chart-baseload"), {
      height: 240,
      series: [
        { key: "baseload_kwh", label: "Always-on floor", color: "var(--series-1)" },
        { key: "variable_kwh", label: "Everything else", color: "var(--series-2)" },
      ],
      data: b.split.map((x) => ({ ...x, label: x.short })),
      tickFormat: (v) => `${Math.round(v)}`,
    });
    $("baseload-table").classList.toggle("scroll", b.split.length > 24);
    $("baseload-table").innerHTML = table(
      [
        { head: "Statement", cell: (x) => x.label },
        { head: `Total ${b.unit || "kWh"}`, cell: (x) => Math.round(x.kwh).toLocaleString("en-US") },
        { head: "Always-on", cell: (x) => Math.round(x.baseload_kwh).toLocaleString("en-US") },
        { head: "Everything else", cell: (x) => Math.round(x.variable_kwh).toLocaleString("en-US") },
      ],
      b.split,
    );
  }

  if (rt) {
    const moves = rt.changes.length;
    const u = rt.unit || "kWh";
    const per = (v) => `$${Number(v).toFixed(4)}/${u}`;
    $("rate-title").textContent = `The price you pay per ${u}`;
    const t = r.tariff;
    $("rate-note").innerHTML =
      `Now ${per(rt.current)}, ranged ${per(rt.min)}–${per(rt.max)}. `
      + `<strong>${moves}</strong> move${moves === 1 ? "" : "s"} of 3% or more — `
      + `bigger than most behaviour changes you could make.`
      + (rt.excluded ? ` (${rt.excluded} near-zero-usage month${rt.excluded === 1 ? "" : "s"} `
          + `excluded — a standing charge over almost no usage isn't a rate.)` : "")
      + (t ? `<br>Splitting the bill into its parts: about <strong>${money(t.standing_charge)}</strong> `
          + `standing charge per statement plus <strong>${per(t.unit_rate)}</strong> `
          + `(fits ${Math.round(t.r2 * 100)}% of the variation over ${t.n} statements). `
          + `The standing charge is owed whatever you do.` : "");
    charts.rate?.destroy();
    charts.rate = new LineChart($("chart-rate"), {
      height: 240,
      data: rt.series.map((x) => ({ label: x.short, value: x.rate, row: x })),
      format: (v) => `$${v.toFixed(3)}`,
      tickFormat: (v) => `$${v.toFixed(2)}`,
      tooltip: (d) => `<div class="t-title">${d.row.label}</div>
        <div class="t-row"><span>Rate</span><b>${rateText(d.value)}</b></div>`,
    });
    $("rate-table").classList.toggle("scroll", rt.changes.length > 12);
    $("rate-table").innerHTML = rt.changes.length ? table(
      [
        { head: "Statement", cell: (x) => x.label },
        { head: "From", cell: (x) => rateText(x.from) },
        { head: "To", cell: (x) => rateText(x.to) },
        { head: "Move", cell: (x) => raw(`<span class="${x.pct > 0 ? "up" : "down"}">${pctText(x.pct)}</span>`) },
        { head: "Effect on that bill", cell: (x) => x.dollars == null ? "—" :
            raw(`<span class="${x.dollars > 0 ? "up" : "down"}">${signedMoney(x.dollars)}</span>`) },
      ],
      rt.changes,
    ) : `<p class="note">No moves above the threshold.</p>`;
  }
}

function renderEnergy(r) {
  const present = fuelsIn(r.energy || []);
  const others = present.filter((f) => f.name !== r.fuel);
  const rows = (r.energy || []).filter((x) => others.some((f) => x[f.key]));
  $("energy-section").hidden = rows.length < 2 || !others.length;
  if (rows.length < 2 || !others.length) return;
  const last12 = rows.slice(-12);
  const sum = (key) => last12.reduce((s, x) => s + (x[key] || 0), 0);
  const total = present.reduce((s, f) => s + sum(f.key), 0);
  const mine = sum(r.fuel.toLowerCase());
  $("energy-note").innerHTML =
    `Last 12 months with every utility: <strong>${money(total, false)}</strong> total — `
    + present.map((f) => `${money(sum(f.key), false)} ${f.key}`).join(", ") + `. `
    + `${esc(fuelNoun(r.fuel)).replace(/^./, (c) => c.toUpperCase())} is <strong>${Math.round(mine / total * 100)}%</strong> `
    + `of what you spend on utilities, and the ${r.fuel.toLowerCase()}-only view above doesn't see the rest.`;
  $("energy-legend").innerHTML = present.map((f) =>
    `<span class="item"><span class="swatch" style="background:${f.color}"></span>${esc(f.name)}</span>`).join("");
  // "Total Amount Due" includes arrears, so a stretch where a balance rolled
  // up is not a run of monthly costs. Say so rather than charting it silently.
  const suspect = r.suspect_balances || [];
  if (suspect.length) {
    const runs = suspect.map((x) =>
      `${x.fuel.toLowerCase()} ${x.label} (${money(x.low)} climbing to ${money(x.peak)}, `
      + `then ${money(x.then)})`).join("; ");
    $("energy-note").innerHTML += `<br><strong>Read these with care.</strong> These bills report `
      + `<em>amount due</em>, which carries anything unpaid from earlier months. `
      + `${suspect.length} stretch${suspect.length === 1 ? "" : "es"} climb and then collapse, `
      + `which is what an accruing balance looks like: ${runs}. `
      + `Summing those months double-counts the carried part.`;
  }
  charts.energy?.destroy();
  charts.energy = new StackedColumns($("chart-energy"), {
    height: 260,
    series: present.map((f) => ({ key: f.key, label: f.name, color: f.color })),
    data: rows.map((x) => ({ ...x, label: x.short })),
  });
  $("energy-table").classList.toggle("scroll", rows.length > 24);
  $("energy-table").innerHTML = table(
    [
      { head: "Month", cell: (x) => x.label },
      ...present.map((f) => ({ head: f.name, cell: (x) => money(x[f.key] || 0) })),
      { head: "Total", cell: (x) => money(x.total) },
    ],
    [...rows].reverse(),
  );
}

function renderFuel(r) {
  const nav = $("fuel-nav");
  if (!nav) return;
  if (!r.fuels || r.fuels.length < 2) { nav.hidden = true; return; }
  nav.hidden = false;
  nav.innerHTML = r.fuels.map((f) =>
    `<a href="${url("/" + f.toLowerCase())}"${f === r.fuel ? ' aria-current="page"' : ""}>${f}</a>`).join("");
}

function renderYoY(r) {
  if (!r.yoy.length) {
    const need = 13 - r.summary.count;
    $("yoy-body").innerHTML =
      `<p class="note">No matched pairs yet — ${need > 0 ? `about ${need} more statement${need === 1 ? "" : "s"}` : "one more year of history"} and this fills in.</p>`;
    return;
  }
  const hasKwh = r.yoy.some((x) => x.kwh_delta != null);
  $("yoy-body").innerHTML = table(
    [
      { head: "Month", cell: (x) => x.label },
      { head: "Last year", cell: (x) => money(x.last_year) },
      { head: "This year", cell: (x) => money(x.this_year) },
      { head: "Change", cell: (x) =>
          raw(`<span class="${x.delta < 0 ? "down" : "up"}">${signedMoney(x.delta)} (${pctText(x.pct)})</span>`) },
      ...(hasKwh ? [{ head: `${unitOf(r)} change`, cell: (x) => x.kwh_delta == null ? "—" :
          raw(`<span class="${x.kwh_delta < 0 ? "down" : "up"}">${x.kwh_delta > 0 ? "+" : "\u2212"}${
            Math.abs(x.kwh_delta).toLocaleString("en-US")} (${pctText(x.kwh_pct)})</span>`) }] : []),
    ],
    r.yoy,
  );
}

function renderProjection(r) {
  const p = r.projection;
  if (!p || !p.months) { $("projection").textContent = ""; return; }
  $("projection").innerHTML =
    `Holding a <strong>${p.target_pct}%</strong> cut against your seasonal averages, the next twelve statements come to
     <strong>${money(p.projected_total, false)}</strong> instead of <strong>${money(p.baseline_total, false)}</strong> —
     <strong>${money(p.saved, false)} saved</strong> over the year.`;
}

function renderFullTable(r) {
  $("source-note").textContent = `Parsed from ${r.source}`;
  const verified = r.series.filter((x) => x.verified).length;
  $("provenance-note").innerHTML = verified === r.series.length ? "" :
    `<strong>${verified} of ${r.series.length}</strong> statements are corroborated by a metered `
    + `period, where the amount matched that period's own charge exactly. The rest come from bill `
    + `emails alone, which report <em>amount due</em> — reliable when paid monthly, inflated when a `
    + `balance carried. Pulling the portal's billing export for the older accounts would replace them.`;
  $("full-table").classList.toggle("scroll", r.series.length > 24);
  $("full-table").innerHTML = table(
    [
      { head: "Statement", cell: (x) => x.label },
      { head: "Due date", cell: (x) => x.due },
      { head: "Amount", cell: (x) => money(x.amount) },
      { head: "vs prior", cell: (x) => x.delta === null ? "—" :
          raw(`<span class="${x.delta < 0 ? "down" : "up"}">${signedMoney(x.delta)}</span>`) },
      { head: "%", cell: (x) => x.pct === null ? "—" :
          raw(`<span class="${x.delta < 0 ? "down" : "up"}">${pctText(x.pct)}</span>`) },
      { head: unitOf(r), cell: (x) => x.kwh == null ? "—" : Math.round(x.kwh).toLocaleString("en-US") },
      { head: `$/${unitOf(r)}`, cell: (x) => x.rate == null ? "—" : x.rate.toFixed(4) },
      { head: "Metered", cell: (x) => x.service_label || "—" },
      { head: "Verified", cell: (x) => x.verified ? "✓ charge" : x.kwh != null ? "paired by date" : "bill only" },
      { head: "Payment", cell: (x) => x.paid ? `${x.status} ${x.paid_on}` : "not yet due" },
    ],
    [...r.series].reverse(),
  );
}

function renderAccounts(r) {
  const sel = $("account");
  // Once merged there is one continuous series, so a selector does nothing.
  if (!r.accounts || r.accounts.length < 2 || r.merged_accounts) { sel.hidden = true; return; }
  sel.hidden = false;
  sel.innerHTML = r.accounts
    .map((a) => `<option value="${esc(a.id)}" ${a.id === r.account ? "selected" : ""}>${esc(a.label)}</option>`)
    .join("");
}

function renderSettings(r) {
  $("goal").value = r.settings.goal ?? "";
  $("target-pct").value = r.settings.target_pct;
  $("baseline-months").value = r.settings.baseline_months;
}

/* Nothing on file for this fuel: the card that says how to add some, and a
   way across to the fuels that do have bills. The rest of the page stays
   hidden - an empty dashboard reads as a broken one. */
function renderEmpty(r) {
  const box = $("empty-state");
  if (!box) { flash(r.error, true); return; }
  box.hidden = false;
  const others = (r.fuels || []).filter((f) => f !== r.fuel);
  const nav = $("fuel-nav");
  if (nav && others.length) {
    nav.hidden = false;
    nav.innerHTML = others.map((f) => `<a href="${url("/" + f.toLowerCase())}">${f}</a>`).join("");
  }
  const note = $("empty-others");
  if (note) {
    note.hidden = !others.length;
    note.innerHTML = others.length
      ? `Bills you do have: ${others.map((f) => `<a href="${url("/" + f.toLowerCase())}">${f}</a>`).join(" · ")}`
      : "";
  }
}

function renderAll(data) {
  calc.init(data);
  compare.init(data);
  report = data;
  if (data.error && !(data.series || []).length) {
    if (data.empty) renderEmpty(data); else flash(data.error, true);
    return;
  }
  // Each panel on its own: one that can't draw must not take the rest of the
  // page down with it.
  for (const panel of [renderAccounts, renderSettings, renderHero, renderTiles, renderLevel,
                       renderBadges, renderFuel, renderAnomalies, renderCharts, renderUsage,
                       renderBaseload, renderEnergy, renderYoY, renderProjection, renderFullTable]) {
    try {
      panel(data);
    } catch (err) {
      console.error(`${panel.name} could not be drawn`, err);
    }
  }
  // Worked out separately, and after: it may have to fetch the weather.
  insights.loadFuel(data.fuel);
}

/* ---------------------------------------------------------------- wiring */

async function load() {
  // Keep the route's fuel across refreshes - the stored setting may differ.
  const fuel = (report && report.fuel) || document.title.split(" ")[0];
  try {
    renderAll(await fetchJSON(url(`/api/report?fuel=${encodeURIComponent(fuel)}`)));
  } catch (err) {
    flash(`Couldn't load the report: ${err.message}`, true);
  }
}

function bootstrap() {
  const node = $("bootstrap");
  if (!node) return false;
  try {
    const data = JSON.parse(node.textContent);
    if (!data || !data.series) return false;
    renderAll(data);
    return true;
  } catch {
    return false;
  }
}

async function saveSettings() {
  const body = {
    goal: $("goal").value === "" ? null : $("goal").value,
    target_pct: $("target-pct").value,
    baseline_months: $("baseline-months").value,
    account: $("account").hidden ? (report && report.account) : $("account").value,
    fuel: report && report.fuel,
  };
  try {
    renderAll(await postJSON(url("/api/settings"), body));
    flash("Saved.");
  } catch (err) {
    flash(`Not saved: ${err.message}`, true);
  }
}

/* One line per uploaded file - what it was, and what it changed. */
function describeUpload(u) {
  const many = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  if (u.kind === "error") return `${u.file}: ${u.error}`;
  if (u.kind === "unsure") {
    return `${u.file}: ${u.reason} Open "Add bills" (top right) and drop it there to say which column is which.`;
  }
  if (u.kind === "bills") {
    return u.added
      ? `Merged ${many(u.added, "new row")} — ${u.total} total.`
      : "Nothing new in that statement history; every row was already on file.";
  }
  const span = `${many(u.periods, "metered period")}, ${u.first} to ${u.last}`;
  return u.stored
    ? `Added usage export: ${span}.`
    : `Already had that usage export (${span}).`;
}

async function upload(files) {
  const form = new FormData();
  for (const file of files) form.append("file", file);
  form.append("fuel", (report && report.fuel) || "");
  $("dropzone").setAttribute("aria-busy", "true");
  let res, data;
  try {
    res = await fetch(url("/api/upload"), { method: "POST", body: form });
    data = await res.json();
  } catch {
    flash("Upload failed — the server didn't answer.", true);
    return;
  } finally {
    $("dropzone").removeAttribute("aria-busy");
  }
  if (!res.ok) { flash(data.error || "Upload failed.", true); return; }
  renderAll(data);
  const uploads = data.uploads || [];
  flash(uploads.map(describeUpload).join(" "), uploads.some((u) => u.kind === "error"));
}

function redraw() {
  // Charts read CSS variables at draw time, so re-render for the new palette.
  if (report) {
    renderCharts(report); renderUsage(report);
    renderBaseload(report); renderEnergy(report);
    insights.redraw(); compare.redraw();
  }
}

function initUpload() {
  const zone = $("dropzone");
  const input = $("file");
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
  input.addEventListener("change", () => { if (input.files.length) upload([...input.files]); input.value = ""; });
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

initTheme(redraw);
initTableToggles();
initUpload();
insights.initFuel(document.title.split(" ")[0], flash);
$("refresh").addEventListener("click", load);
$("save-settings").addEventListener("click", saveSettings);
$("account").addEventListener("change", saveSettings);

for (const id of ["goal", "target-pct", "baseline-months"]) {
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") saveSettings(); });
}

// Render from the inlined payload; only hit the network if it isn't there.
if (!bootstrap()) load();
