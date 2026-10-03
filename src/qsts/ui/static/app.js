"use strict";
const $ = (s) => document.querySelector(s);
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : (typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: d }) : String(v));
const pct = (v) => (v === null || v === undefined) ? "—" : (v * 100).toFixed(1) + "%";
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const t = await r.text();
  let body; try { body = JSON.parse(t); } catch { body = t; }
  if (!r.ok) throw new Error(body && body.detail ? body.detail : t);
  return body;
}
const post = (p, b) => api(p, { method: "POST", body: JSON.stringify(b || {}) });
function dl(el, obj) { el.innerHTML = Object.entries(obj).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join(""); }
function table(el, rows, cols) {
  if (!rows || !rows.length) { el.innerHTML = `<tr><td class="muted">Sin datos</td></tr>`; return; }
  el.innerHTML = `<tr>${cols.map(c => `<th>${c[0]}</th>`).join("")}</tr>` +
    rows.map(r => `<tr>${cols.map(c => `<td>${c[1](r)}</td>`).join("")}</tr>`).join("");
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------------------------------------------------------------- tabs
document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  b.classList.add("active"); $("#tab-" + b.dataset.tab).classList.add("active");
  ({ signals: loadSignals, charts: initCharts, strategies: loadStrategies, research: loadExperiments, logs: loadLogs }[b.dataset.tab] || (() => {}))();
});

// ---------------------------------------------------------------- dashboard
const MODES = ["OBSERVATION", "BACKTEST", "PAPER", "MANUAL_APPROVAL", "SEMI_AUTOMATIC", "FULL_AUTOMATIC"];
$("#mode-select").innerHTML = MODES.map(m => `<option>${m}</option>`).join("");
async function loadStatus() {
  const s = await api("/api/status");
  $("#env").textContent = s.environment.toUpperCase(); $("#mode").textContent = s.mode;
  $("#mode-select").value = s.mode;
  const b = s.broker || {};
  dl($("#portfolio"), { "Capital": fmt(b.equity), "Cash": fmt(b.cash), "Posiciones": Object.keys(b.positions || {}).length,
    "Pendientes aprobación": s.pending_approvals });
  dl($("#strat-counts"), Object.keys(s.strategies).length ? Object.fromEntries(Object.entries(s.strategies).map(([k, v]) => [k.toUpperCase(), v])) : { "Estrategias": 0 });
  dl($("#system"), { "Datos (símbolos)": s.symbols_with_data, "AI": s.ai, "Broker": `${b.name || "?"} (${b.environment || "?"})`,
    "Conexión": b.connected ? '<span class="good">OK</span>' : '<span class="bad">CAÍDA</span>',
    "Reconciliación": s.needs_reconcile ? '<span class="bad">PENDIENTE</span>' : "OK",
    "LIVE": s.live_enabled_by_config ? '<span class="bad">HABILITADO</span>' : "DESACTIVADO",
    "Kill switch": s.kill_switch.engaged ? '<span class="bad">ACTIVADO</span>' : '<span class="good">READY</span>' });
  const k = $("#kill"); k.classList.toggle("engaged", s.kill_switch.engaged);
  k.textContent = s.kill_switch.engaged ? "KILL SWITCH ACTIVO (liberar)" : "STOP ALL TRADING";
}
$("#kill").onclick = async () => {
  const s = await api("/api/status");
  if (!s.kill_switch.engaged) { const r = prompt("Motivo para detener todo el trading:", "manual"); if (r === null) return; await post("/api/kill-switch", { engage: true, reason: r }); }
  else if (confirm("¿Liberar el kill switch? Las posiciones no se han tocado.")) await post("/api/kill-switch", { engage: false, confirm: true });
  loadStatus();
};
$("#mode-apply").onclick = async () => {
  try { const r = await post("/api/mode", { mode: $("#mode-select").value, reason: $("#mode-reason").value || "ui", confirm: $("#mode-confirm").checked });
    $("#mode-msg").textContent = "Modo: " + r.mode; } catch (e) { $("#mode-msg").textContent = e.message; }
  loadStatus();
};
let lastScan = null;
$("#run-scan").onclick = async () => {
  $("#scan-text").textContent = "Escaneando…";
  try { lastScan = await post("/api/scan"); $("#scan-text").textContent = await api("/api/scan/text");
    dl($("#market"), { "Régimen": lastScan.regime.trend || "—", "Volatilidad": lastScan.regime.volatility || "—",
      "Escaneados": lastScan.assets_scanned, "Válidos": lastScan.valid_assets, "Setups": lastScan.potential_setups, "Señales": lastScan.final_signals });
  } catch (e) { $("#scan-text").textContent = "Error: " + e.message; }
};

// ---------------------------------------------------------------- signals
async function loadSignals() {
  const ap = await api("/api/approvals");
  table($("#approvals"), ap, [["Activo", r => r.signal.symbol], ["Dir", r => r.signal.direction > 0 ? "LONG" : "SHORT"],
    ["Entrada", r => fmt(r.signal.entry)], ["Stop", r => fmt(r.signal.stop)], ["Cantidad", r => fmt(r.qty, 4)],
    ["Riesgo", r => fmt(r.risk_amount)], ["Tier", r => r.tier], ["Estrategia", r => esc(r.signal.strategy_id)],
    ["", r => `<button onclick="decide('${r.key}',true)">Aprobar</button> <button onclick="decide('${r.key}',false)">Rechazar</button>`]]);
  const sig = lastScan ? lastScan.signals : [];
  table($("#signals"), sig, [["Activo", r => r.symbol], ["Dir", r => r.direction], ["Confianza", r => r.confidence == null ? '<span class="muted">sin calibrar</span>' : pct(r.confidence)],
    ["Estrategia", r => esc(r.strategy_id)], ["Entrada", r => fmt(r.entry)], ["Stop", r => fmt(r.stop)], ["Objetivo", r => fmt(r.target)],
    ["R:R", r => fmt(r.rr)], ["Riesgo", r => fmt(r.risk_amount)], ["Tier", r => r.tier]]);
  table($("#notrade"), lastScan ? lastScan.no_trade : [], [["Activo", r => r.symbol], ["Estrategia", r => esc(r.strategy_id)], ["Motivos", r => esc((r.reasons || []).join("; "))]]);
}
window.decide = async (key, approve) => {
  const reason = approve ? "" : (prompt("Motivo del rechazo:", "") || "");
  try { const r = await post(`/api/approvals/${key}`, { approve, reason }); alert(r.status + (r.reasons && r.reasons.length ? ": " + r.reasons.join("; ") : "")); }
  catch (e) { alert(e.message); }
  loadSignals(); loadStatus();
};

// ---------------------------------------------------------------- charts
let chart, candle, vol, overlays = [], osc, oscSeries = [];
async function initCharts() {
  const syms = await api("/api/symbols");
  $("#chart-symbol").innerHTML = syms.map(s => `<option>${s}</option>`).join("");
  if (!syms.length) $("#chart-msg").textContent = "Sin datos. Usa `qsts ingest` para descargar/importar precios.";
}
const opts = () => ({ layout: { background: { color: "#171b22" }, textColor: "#c9cdd4" }, grid: { vertLines: { color: "#20252e" }, horzLines: { color: "#20252e" } }, timeScale: { timeVisible: true } });
$("#chart-load").onclick = async () => {
  const sym = $("#chart-symbol").value; if (!sym) return;
  const ind = [$("#chart-ind").value, $("#chart-osc").value].filter(Boolean).join(",");
  const asof = $("#chart-asof").value;
  try {
    const d = await api(`/api/chart/${encodeURIComponent(sym)}?tf=${$("#chart-tf").value}&indicators=${encodeURIComponent(ind)}${asof ? "&asof=" + encodeURIComponent(asof) : ""}`);
    if (!chart) {
      chart = LightweightCharts.createChart($("#chart"), opts());
      candle = chart.addCandlestickSeries({ upColor: "#26a69a", downColor: "#ef5350", borderVisible: false, wickUpColor: "#26a69a", wickDownColor: "#ef5350" });
      vol = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "", color: "#3a4250" });
      vol.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
      osc = LightweightCharts.createChart($("#chart-osc-pane"), opts());
    }
    candle.setData(d.candles); vol.setData(d.volume);
    overlays.forEach(s => chart.removeSeries(s)); overlays = [];
    oscSeries.forEach(s => osc.removeSeries(s)); oscSeries = [];
    const oscNames = $("#chart-osc").value.split(",").map(x => x.split(":")[0]).filter(Boolean);
    const colors = ["#4c8dff", "#ffb74d", "#ba68c8", "#4dd0e1"]; let i = 0;
    for (const [k, series] of Object.entries(d.indicators)) {
      const target = oscNames.some(n => k.startsWith(n)) ? osc : chart;
      const s = target.addLineSeries({ color: colors[i++ % colors.length], lineWidth: 1, title: k });
      s.setData(series); (target === osc ? oscSeries : overlays).push(s);
    }
    chart.timeScale().fitContent(); osc.timeScale().fitContent();
    const warn = d.quality.filter(q => q.severity !== "INFO").map(q => q.code).join(", ");
    $("#chart-msg").textContent = `datos v${d.data_version}` + (warn ? ` · avisos: ${warn}` : "") + (asof ? ` · vista as-of ${asof}` : "");
  } catch (e) { $("#chart-msg").textContent = e.message; }
};

// ---------------------------------------------------------------- strategies
let stratDefs = [];
async function loadStrategies() {
  const st = await api("/api/strategies");
  stratDefs = st.map(r => r.versions[r.versions.length - 1]?.definition || {});
  table($("#strategies"), st, [["ID", r => `<a href="#" onclick="hist('${esc(r.id)}');return false">${esc(r.id)}</a>`], ["Nombre", r => esc(r.name)],
    ["Familia", r => esc(r.family)], ["Estado", r => r.status], ["Origen", r => r.origin], ["Versiones", r => r.versions.length],
    ["", (r) => `<button onclick="toLab(${st.indexOf(r)})">Duplicar en Lab</button>`]]);
}
window.hist = async (id) => { const h = await api(`/api/strategies/${encodeURIComponent(id)}/history`); $("#strategy-history").textContent = h.map(x => `${x[0] || "∅"} → ${x[1]}  (${x[2]})`).join("\n"); };
window.toLab = (i) => { $("#lab-def").value = JSON.stringify(stratDefs[i], null, 2); document.querySelector('nav button[data-tab="lab"]').click(); };

// ---------------------------------------------------------------- lab
$("#lab-def").value = JSON.stringify({
  name: "rsi_pullback", family: "pullback", hypothesis: "Oversold dips inside a long-term uptrend tend to mean-revert",
  direction: "long",
  entry_long: [{ left: { feature: "close" }, op: ">", right: { feature: "sma", params: { n: 200 } } },
               { left: { feature: "rsi", params: { n: 14 } }, op: "<", right: { value: "$rsi_lo" } }],
  exit_long: [{ left: { feature: "rsi", params: { n: 14 } }, op: ">", right: { value: 55 } }],
  stop: { kind: "atr", atr_n: 14, mult: 2.5 }, take_profit: { kind: "r_multiple", value: 2.0 },
  max_holding_bars: 10, params: { rsi_lo: 35 } }, null, 2);
let eqChart, eqSeries;
$("#lab-run").onclick = async () => {
  $("#lab-msg").textContent = "Ejecutando…";
  try {
    const body = { definition: JSON.parse($("#lab-def").value), symbols: $("#lab-symbols").value.split(",").map(s => s.trim()).filter(Boolean),
      start: $("#lab-start").value || null, end: $("#lab-end").value || null, initial_capital: +$("#lab-cap").value,
      risk_per_trade: +$("#lab-risk").value, spread_bps: +$("#lab-spread").value, slippage_bps: +$("#lab-slip").value };
    const r = await post("/api/lab/backtest", body);
    $("#lab-msg").textContent = `experimento ${r.experiment_id} · versión ${r.strategy_version_id}`;
    const m = r.metrics;
    dl($("#lab-metrics"), { "Retorno total": pct(m.total_return), "CAGR": pct(m.cagr), "Sharpe": fmt(m.sharpe), "Sortino": fmt(m.sortino),
      "Max DD": pct(m.max_drawdown), "Calmar": fmt(m.calmar), "Trades": m.n_trades, "Win rate": pct(m.win_rate),
      "Profit factor": fmt(m.profit_factor), "Expectancy (R)": fmt(m.expectancy_r), "Exposición": pct(m.exposure),
      "Complejidad": r.complexity.score, "Aviso": m.n_trades < 30 ? '<span class="bad">pocas operaciones: estadística no fiable</span>' : "—" });
    if (!eqChart) { eqChart = LightweightCharts.createChart($("#lab-equity"), opts()); eqSeries = eqChart.addLineSeries({ color: "#4c8dff" }); }
    eqSeries.setData(r.equity); eqChart.timeScale().fitContent();
    table($("#lab-trades"), r.trades, [["Activo", t => t.symbol], ["Dir", t => t.direction], ["Entrada", t => t.entry_ts.slice(0, 10)], ["Salida", t => t.exit_ts.slice(0, 10)],
      ["P. entrada", t => fmt(+t.entry_price)], ["P. salida", t => fmt(+t.exit_price)], ["P&L", t => `<span class="${+t.pnl >= 0 ? "up" : "down"}">${fmt(+t.pnl)}</span>`],
      ["R", t => fmt(+t.r_multiple)], ["Motivo", t => t.exit_reason]]);
  } catch (e) { $("#lab-msg").textContent = "Error: " + e.message; }
};

// ---------------------------------------------------------------- research
async function loadExperiments() {
  const ex = await api("/api/experiments");
  table($("#experiments"), ex, [["ID", r => r.id.slice(0, 12)], ["Tipo", r => r.kind], ["Estrategia v.", r => r.strategy_version_id.slice(0, 10)],
    ["Datos v.", r => r.dataset_version_id.slice(0, 10)], ["Seed", r => r.seed], ["Código", r => r.code_version], ["Sharpe", r => fmt(r.metrics?.sharpe)],
    ["Trades", r => r.metrics?.n_trades ?? "—"], ["Fecha", r => r.created_at.slice(0, 19)], ["", r => `<button onclick="repro('${r.id}')">REPRODUCE EXPERIMENT</button>`]]);
}
window.repro = async (id) => { $("#repro").textContent = "Reproduciendo…"; try { const r = await post(`/api/experiments/${id}/reproduce`);
  $("#repro").textContent = (r.reproduced ? "✔ Reproducido exactamente" : "✘ NO reproducido: " + r.reason) + "\n" + JSON.stringify(r, null, 2); } catch (e) { $("#repro").textContent = e.message; } };

// ---------------------------------------------------------------- logs
async function loadLogs() {
  table($("#notifications"), (await api("/api/notifications")).reverse(), [["Hora", r => r.ts.slice(0, 19)], ["Evento", r => r.event], ["Título", r => esc(r.title)], ["Detalle", r => esc(r.body)]]);
  table($("#journal"), (await api("/api/journal")).reverse(), [["Hora", r => r.ts.slice(0, 19)], ["Activo", r => r.signal?.symbol ?? "—"], ["Estado", r => r.status], ["Motivos", r => esc((r.reasons || []).join("; "))]]);
}

loadStatus(); setInterval(loadStatus, 10000);
