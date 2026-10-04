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
let autoTimer = null;
function goTab(name) {
  document.querySelectorAll("nav button[data-tab]").forEach(x => x.classList.toggle("active", x.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  $("#tab-" + name).classList.add("active");
  if ($(`#adv button[data-tab="${name}"]`)) $("#adv").classList.add("open");
  clearInterval(autoTimer); autoTimer = null;
  ({ home: loadHome, data: openData, auto: openAuto, signals: loadSignals, charts: initCharts, strategies: loadStrategies,
     research: loadExperiments, logs: loadLogs }[name] || (() => {}))();
}
document.querySelectorAll("nav button[data-tab]").forEach(b => b.onclick = () => goTab(b.dataset.tab));
$("#adv-toggle").onclick = () => $("#adv").classList.toggle("open");
document.querySelectorAll("[data-goto]").forEach(b => b.onclick = () => {
  if (b.dataset.open === "best" && homeBest) arSel = homeBest.id, arShowBt = true;
  goTab(b.dataset.goto);
});

// ---------------------------------------------------------------- home (guided steps)
let homeBest = null;
async function loadHome() {
  try {
    const d = await api("/api/data/summary");
    const el = $("#step-data");
    el.classList.toggle("done", d.count >= 50); el.classList.toggle("todo", d.count < 50);
    el.querySelector(".body").innerHTML = d.count === 0 ? '<span class="bad">Aún no hay datos.</span> Empieza descargando acciones del S&P 500.'
      : `Tienes <b>${d.count}</b> acciones con precios del ${d.first} al ${d.last}.` +
        (d.count < 50 ? '<p class="warn small">Son pocas: con tan pocas acciones casi cualquier resultado puede ser casualidad. Añade una muestra del S&amp;P 500.</p>' : "");
  } catch (e) { $("#step-data .body").textContent = e.message; }
  try {
    const st = await api("/api/autoresearch/status");
    $("#step-search .body").innerHTML = (st.running ? '<span class="good">Investigando ahora…</span> ' : "Parado. ") +
      "La app combina indicadores y, con IA, propone ideas nuevas. Cada ciclo prueba decenas de estrategias.";
  } catch (e) { $("#step-search .body").textContent = e.message; }
  try {
    const lb = await api("/api/autoresearch/leaderboard?limit=40");
    const best = lb.rows.find(r => r.origin !== "baseline"), ref = lb.rows.find(r => r.origin === "baseline");
    homeBest = best || null;
    const el = $("#step-best");
    if (!best) { el.querySelector(".body").innerHTML = `Aún no hay resultados con tus datos actuales (${lb.universe.n_symbols} acciones). Lanza la investigación.`; return; }
    const pv = lb.passive || {};
    const bar = Math.max(ref ? ref.consistency : -Infinity, pv.consistency ?? -Infinity);
    const beats = best.consistency > bar;
    el.classList.toggle("done", beats); el.classList.toggle("todo", !beats);
    el.querySelector(".body").innerHTML = `<b>${esc(best.rules)}</b><br>Consistencia <b>${fmt(best.consistency, 3)}</b>` +
      ` frente a ${fmt(bar, 3)} del listón (no hacer nada o la mejor regla simple): ` + (beats ? '<span class="good">lo supera</span>' : '<span class="bad">todavía no lo supera</span>') +
      `<p class="muted small">${lb.n_trials_universe} estrategias probadas con estos datos.</p>`;
  } catch (e) { $("#step-best .body").textContent = e.message; }
}

// ---------------------------------------------------------------- data manager
let dataRows = [];
function openData() { loadData(); autoTimer = setInterval(loadJob, 2000); }
async function loadData() {
  try {
    const d = await api("/api/data/summary");
    $("#data-summary").innerHTML = d.count ? `<b>${d.count}</b> acciones, del <b>${d.first}</b> al <b>${d.last}</b>.` +
      (d.has_benchmark ? "" : ` <span class="bad">Falta ${d.benchmark} (referencia del mercado).</span>`) : '<span class="bad">No hay datos todavía.</span>';
    dataRows = d.symbols; renderDataTable();
  } catch (e) { $("#data-summary").textContent = e.message; }
  api("/api/data/sp500").then(sp => {
    $("#sp500-info").innerHTML = `Lista actual: <b>${sp.count}</b> empresas (ya tienes ${sp.loaded}). Fuente: ${esc(sp.source.replace("https://", ""))}.`;
  }).catch(e => { $("#sp500-info").innerHTML = `<span class="bad">${esc(e.message)}</span>`; });
  loadJob();
}
function renderDataTable() {
  const q = ($("#data-filter").value || "").toUpperCase();
  const rows = dataRows.filter(r => !q || r.symbol.includes(q) || (r.name || "").toUpperCase().includes(q) || (r.sector || "").toUpperCase().includes(q));
  table($("#data-table"), rows.slice(0, 600), [["Símbolo", r => r.symbol], ["Empresa", r => esc(r.name || "—")], ["Sector", r => esc(r.sector || "—")],
    ["Desde", r => r.first], ["Hasta", r => r.last], ["Días", r => r.bars], ["En el S&P 500 desde", r => r.sp500_since || "—"]]);
}
$("#data-filter").oninput = renderDataTable;
let jobWasRunning = false;
async function loadJob() {
  let j; try { j = await api("/api/data/job"); } catch (e) { return; }
  const pctDone = j.total ? Math.round(100 * j.done / j.total) : 0;
  $("#job-bar").style.width = (j.running ? pctDone : (j.total ? 100 : 0)) + "%";
  $("#job-stop").disabled = !j.running;
  ["#sp-download", "#sym-download", "#data-update"].forEach(id => $(id).disabled = j.running);
  $("#job-text").innerHTML = j.running ? `Descargando <b>${esc(j.current || "")}</b> · ${j.done}/${j.total} (${pctDone}%) · ${Object.keys(j.failed).length} con problemas`
    : (j.message ? "Última descarga: " + esc(j.message) : "Sin descargas en curso.");
  $("#job-log").textContent = (j.log || []).slice().reverse().join("\n");
  if (jobWasRunning && !j.running) loadData();
  jobWasRunning = j.running;
}
async function startIngest(body) {
  try { const r = await post("/api/data/ingest", body); $("#job-text").textContent = `Empezando: ${r.symbols} acciones…`; }
  catch (e) { alert(e.message); }
  loadJob();
}
$("#sp-download").onclick = () => { const n = +document.querySelector('input[name="sp-size"]:checked').value; startIngest({ mode: "sp500", sample: n || null }); };
$("#sym-download").onclick = () => startIngest({ mode: "symbols", symbols: $("#sym-input").value.split(/[,\s]+/), start: $("#sym-start").value || "2010-01-01" });
$("#data-update").onclick = () => startIngest({ mode: "update" });
$("#job-stop").onclick = async () => { await post("/api/data/job/stop"); loadJob(); };

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

// ---------------------------------------------------------------- automatic research
let arRows = [], arSel = null, arLastCycles = -1, arPolls = 0, arShowBt = false, btChart = null;
const ORIGIN = { evolution: "evolución", ai: "IA", baseline: "referencia" };
const STATUS = { EVALUATED: "probada", INVALID: "no puntuable", VALIDATED_PASS: '<span class="good">✔ validada</span>',
  VALIDATED_FAIL: '<span class="bad">✘ no pasa validación</span>', FINAL_PASS: '<span class="good">✔✔ aprobada en test final</span>',
  FINAL_FAIL: '<span class="bad">✘ suspende test final</span>' };
const GATES = { min_trades: "suficientes operaciones", overfit_risk: "riesgo de sobreajuste aceptable", max_drawdown: "caída máxima aceptable",
  walk_forward: "funciona en ventanas móviles (walk-forward)", beats_baselines: "supera a las estrategias de referencia",
  robustness: "aguanta pequeños cambios de parámetros", costs_2x: "sigue ganando con el doble de costes",
  beats_passive: "supera a mantener todas las acciones sin hacer nada (consistencia y Sharpe)" };
function openAuto() { loadAuto(true); autoTimer = setInterval(() => loadAuto(false), 3000); }
async function loadAuto(full) {
  let st;
  try { st = await api("/api/autoresearch/status"); } catch (e) { $("#ar-msg").textContent = e.message; return; }
  $("#ar-oos").textContent = st.oos_start;
  $("#ar-start").disabled = st.running; $("#ar-stop").disabled = !st.running;
  if (!st.ai_available) { $("#ar-ai").checked = false; $("#ar-ai").disabled = true; $("#ar-ai").parentElement.title = "Pon QSTS_GEMINI_API_KEY en .env"; }
  dl($("#ar-state"), { "Estado": st.running ? '<span class="good">investigando…</span>' : "parado", "Fase": esc(st.phase),
    "Ciclos (esta sesión)": st.cycles_done, "Probadas (esta sesión)": st.session_trials,
    "IA": st.ai_available ? "disponible" : '<span class="muted">no configurada</span>', ...(st.error ? { "Error": `<span class="bad">${esc(st.error)}</span>` } : {}) });
  $("#ar-log").textContent = (st.log || []).slice().reverse().join("\n") || "—";
  arPolls++;
  if (full || st.cycles_done !== arLastCycles || (st.running && arPolls % 5 === 0)) { arLastCycles = st.cycles_done; await loadBoard(); }
}
async function loadBoard() {
  let lb;
  try { lb = await api("/api/autoresearch/leaderboard?limit=25"); } catch (e) { $("#ar-board-note").textContent = e.message; return; }
  $("#ar-period").textContent = `${lb.research_period[0].slice(0, 4)}–${lb.research_period[1].slice(0, 4)}`;
  $("#ar-board-note").innerHTML = `Con tus <b>${lb.universe.n_symbols}</b> acciones se han probado <b>${lb.n_trials_universe}</b> estrategias ` +
    `(${lb.n_trials} en total contando otros conjuntos de datos; todas cuentan para la Fiabilidad) · veces que se ha abierto el periodo guardado: <b>${lb.final_tests_used}</b>. ` +
    `<b>Pulsa una fila para ver su backtest.</b> ` +
    (lb.equivalents_hidden ? `Ocultadas ${lb.equivalents_hidden} variantes equivalentes (mismas operaciones). ` : "") +
    `<b>Consistencia</b> = Sharpe del peor de los 3 tramos (más alto = mejor; por encima de 0,5 es bueno). <b>Fiabilidad</b> = probabilidad de que no sea suerte, teniendo en cuenta todas las pruebas hechas.`;
  const pv = lb.passive || {};
  const kp = (l, v, s) => `<div class="kpi"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${s || ""}</div></div>`;
  $("#ar-passive").innerHTML = kp("Consistencia", fmt(pv.consistency, 3), esc(pv.rules || "")) + kp("Sharpe", fmt(pv.sharpe)) +
    kp("Al año (CAGR)", pct(pv.cagr)) + kp("Caída máx.", pct(pv.max_drawdown)) + kp("Años en positivo", pct(pv.pct_positive_years));
  arRows = lb.rows;
  table($("#ar-board"), arRows, [["#", r => arRows.indexOf(r) + 1],
    ["Estrategia", r => `<span class="badge">${ORIGIN[r.origin] || r.origin}</span>${esc(r.rules)}`],
    ["Consistencia", r => `<span class="${r.consistency > (pv.consistency ?? Infinity) ? "up" : ""}">${fmt(r.consistency, 3)}</span>`], ["Sharpe", r => fmt(r.sharpe)], ["Años en positivo", r => pct(r.pct_positive_years)],
    ["Peor año", r => pct(r.worst_year)], ["Caída máx.", r => pct(r.max_drawdown)], ["Operaciones", r => r.n_trades],
    ["Fiabilidad", r => r.dsr == null ? "—" : pct(r.dsr)], ["Estado", r => STATUS[r.status] || r.status],
    ["", r => r.status === "VALIDATED_PASS" ? `<button onclick="event.stopPropagation();finalTest('${r.id}')">Test final</button>` : ""]]);
  [...$("#ar-board").querySelectorAll("tr")].slice(1).forEach((tr, i) => {
    tr.classList.add("click"); if (arRows[i].id === arSel) tr.classList.add("sel");
    tr.cells[1].classList.add("rules");
    tr.onclick = () => { arSel = arRows[i].id; showDetail(arRows[i]); loadBacktest(arSel); loadBoard();
      $("#ar-detail-card").scrollIntoView({ behavior: "smooth" }); };
  });
  const cur = arRows.find(r => r.id === arSel); if (cur) showDetail(cur);
  if (arShowBt && cur) { arShowBt = false; loadBacktest(cur.id); $("#ar-detail-card").scrollIntoView({ behavior: "smooth" }); }
}
const EXIT = { stop: "stop", stop_gap: "stop (hueco de apertura)", target: "objetivo", signal_exit: "señal de salida",
  time_stop: "tiempo máximo", end_of_data: "fin del periodo", reversal: "señal contraria" };
let btSeries = [];
async function loadBacktest(id) {
  $("#ar-bt").style.display = "block"; $("#ar-bt-period").textContent = "calculando…";
  let d; try { d = await api(`/api/autoresearch/${id}/backtest`); } catch (e) { $("#ar-bt-period").textContent = e.message; return; }
  if (id !== arSel) return;
  $("#ar-bt-period").textContent = `${d.period[0]} → ${d.period[1]}` + (d.includes_oos ? ` · incluye el test final (desde ${d.oos_start})`
    : ` · solo el periodo de investigación: los años desde ${d.oos_start} se ven después del test final`);
  const m = d.metrics, b = (d.benchmark || {}).metrics || {};
  const k = (label, v, sub) => `<div class="kpi"><div class="l">${label}</div><div class="v">${v}</div><div class="s">${sub || ""}</div></div>`;
  $("#ar-bt-kpis").innerHTML = k("Rentabilidad total", pct(m.total_return), `mercado (SPY): ${pct(b.total_return)}`) +
    k("Al año (CAGR)", pct(m.cagr), `mercado: ${pct(b.cagr)}`) + k("Sharpe", fmt(m.sharpe), `mercado: ${fmt(b.sharpe)}`) +
    k("Caída máx.", pct(m.max_drawdown), `mercado: ${pct(b.max_drawdown)}`) + k("Operaciones", m.n_trades ?? "—", `ganadoras: ${pct(m.win_rate)}`) +
    k("Tiempo invertido", pct(m.exposure), `duración media: ${fmt(m.avg_trade_bars, 0)} días`);
  if (!btChart) btChart = LightweightCharts.createChart($("#ar-bt-chart"), opts());
  btSeries.forEach(x => btChart.removeSeries(x)); btSeries = [];
  if (d.benchmark) { const sb = btChart.addLineSeries({ color: "#8b93a1", lineWidth: 1, title: "SPY" }); sb.setData(d.benchmark.equity); btSeries.push(sb); }
  const cut = Date.parse(d.oos_start) / 1000;
  const research = d.includes_oos ? d.equity.filter(p => p.time < cut) : d.equity;
  const s1 = btChart.addLineSeries({ color: "#4c8dff", lineWidth: 2, title: "estrategia" }); s1.setData(research); btSeries.push(s1);
  if (d.includes_oos) { const s2 = btChart.addLineSeries({ color: "#ffb74d", lineWidth: 2, title: "test final" });
    s2.setData(d.equity.filter(p => p.time >= cut)); btSeries.push(s2); }
  $("#ar-bt-oos-legend").style.display = d.includes_oos ? "inline" : "none";
  btChart.timeScale().fitContent();
  table($("#ar-bt-years"), d.yearly, [["Año", y => y.year], ["Estrategia", y => `<span class="${y.strategy >= 0 ? "up" : "down"}">${pct(y.strategy)}</span>`],
    ["Mercado (SPY)", y => pct(y.benchmark)], ["Diferencia", y => y.benchmark == null ? "—" : `<span class="${y.strategy >= y.benchmark ? "up" : "down"}">${pct(y.strategy - y.benchmark)}</span>`]]);
  $("#ar-bt-ntr").textContent = `(${m.n_trades} en total; se muestran las últimas ${d.trades.length})`;
  table($("#ar-bt-trades"), d.trades.slice().reverse(), [["Acción", t => t.symbol], ["Compra", t => t.entry], ["Venta", t => t.exit],
    ["Precio compra", t => fmt(t.entry_price)], ["Precio venta", t => fmt(t.exit_price)],
    ["Resultado", t => `<span class="${t.pnl >= 0 ? "up" : "down"}">${fmt(t.pnl)}</span>`], ["R", t => fmt(t.r)], ["Motivo de salida", t => EXIT[t.reason] || t.reason]]);
}
function showDetail(r) {
  const blocks = (r.blocks || []).map(b => `<tr><td>${b.start} → ${b.end}</td><td>${fmt(b.sharpe)}</td><td>${pct(b.return)}</td><td>${b.trades}</td></tr>`).join("");
  let h = `<p><span class="badge">${ORIGIN[r.origin] || r.origin}</span><b>${esc(r.rules)}</b></p>
    <p class="muted">Id ${r.id}${r.strategy_id ? " · en Estrategias como " + esc(r.strategy_id) : ""}</p>
    <h4>Los 3 tramos del pasado</h4><table><tr><th>Periodo</th><th>Sharpe</th><th>Rentabilidad</th><th>Operaciones</th></tr>${blocks}</table>`;
  const v = r.validation;
  if (v) {
    h += `<h4>Validación: ${v.passed ? '<span class="good">PASA</span>' : '<span class="bad">NO PASA</span>'}</h4><ul>` +
      Object.entries(v.gates).map(([k, ok]) => `<li>${ok ? '<span class="good">✔</span>' : '<span class="bad">✘</span>'} ${GATES[k] || k}</li>`).join("") + "</ul>" +
      `<p class="muted">Estrategias de referencia (Sharpe): ${Object.entries(v.baselines || {}).map(([k, x]) => `${k} ${fmt(x)}`).join(" · ")} — esta: ${fmt(v.is_metrics?.sharpe)}</p>`;
  } else if (r.origin === "baseline") h += `<p class="muted">Estrategia de referencia (muy simple): no se valida ni se opera; sirve de listón. Una estrategia nueva tiene que superarla.</p>`;
  else h += `<p class="muted">Aún no validada (se validan automáticamente las mejores de cada ciclo).</p>`;
  if (r.final) {
    const o = r.final.oos || {};
    h += `<h4>Test final con datos guardados (${r.final.period.join(" → ")}): ${r.final.decision === "FINAL_PASS" ? '<span class="good">APROBADA</span>' : '<span class="bad">SUSPENDE</span>'}</h4>` +
      `<p>Rentabilidad ${pct(o.total_return)} · Sharpe ${fmt(o.sharpe)} · Caída máx. ${pct(o.max_drawdown)} · Operaciones ${o.n_trades ?? "—"}</p>`;
  }
  $("#ar-detail").classList.remove("muted"); $("#ar-detail").innerHTML = h;
}
window.finalTest = async (id) => {
  if (!confirm("TEST FINAL: se probará esta estrategia con los datos guardados bajo llave.\n\nSolo se puede hacer UNA vez por estrategia y el resultado no se puede usar para seguir ajustándola. Úsalo solo con la estrategia que de verdad elegirías.\n\n¿Continuar?")) return;
  try { const f = await post(`/api/autoresearch/${id}/final-test`); arSel = id;
    alert(f.decision === "FINAL_PASS" ? "APROBADA en el test final. Pasa a estado CANDIDATE." : "SUSPENDE el test final. Se marca como rechazada.");
  } catch (e) { alert(e.message); }
  loadBoard();
};
$("#ar-start").onclick = async () => {
  try { const r = await post("/api/autoresearch/start", { use_ai: $("#ar-ai").checked, max_cycles: +$("#ar-cycles").value });
    $("#ar-msg").textContent = r.started ? "En marcha. Cada ciclo tarda unos minutos; puedes seguir usando la app." : "Ya estaba en marcha.";
  } catch (e) { $("#ar-msg").textContent = e.message; }
  loadAuto(false);
};
$("#ar-stop").onclick = async () => { await post("/api/autoresearch/stop"); $("#ar-msg").textContent = "Deteniendo (termina la prueba en curso)…"; loadAuto(false); };

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

loadStatus(); setInterval(loadStatus, 10000); loadHome();
