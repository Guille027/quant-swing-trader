"use strict";
// Thin client: every number comes from the local API. Spanish number format.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const nf = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toLocaleString("es-ES", { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (v, d = 1, sign = false) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : (sign && v > 0 ? "+" : "") + nf(v * 100, d) + "%";
const usd = (v) => v === null || v === undefined ? "—" : nf(v, 2) + " $";
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const t = await r.text();
  let body; try { body = JSON.parse(t); } catch { body = t; }
  if (!r.ok) throw new Error(body && body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : t);
  return body;
}
const post = (p, b) => api(p, { method: "POST", body: JSON.stringify(b || {}) });
function table(el, rows, cols, empty = "Sin datos") {
  if (!rows || !rows.length) { el.innerHTML = `<tr><td class="muted">${empty}</td></tr>`; return; }
  el.innerHTML = `<tr>${cols.map(c => `<th>${c[0]}</th>`).join("")}</tr>` +
    rows.map(r => `<tr>${cols.map(c => `<td>${c[1](r)}</td>`).join("")}</tr>`).join("");
}
const pill = (v, good = (x) => x > 0, d = 1, fmt = null) => v === null || v === undefined
  ? '<span class="pill na">—</span>' : `<span class="pill ${good(v) ? "" : "neg"}">${fmt ? fmt(v) : pct(v, d)}</span>`;
const when = (iso) => iso ? new Date(iso).toLocaleString("es-ES", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
const chartOpts = () => ({ autoSize: true, layout: { background: { color: "#ffffff" }, textColor: "#5b6170", fontFamily: "Inter, system-ui, sans-serif" },
  grid: { vertLines: { color: "#f2f2f7" }, horzLines: { color: "#f2f2f7" } }, rightPriceScale: { borderColor: "#ececf3" },
  timeScale: { borderColor: "#ececf3" }, crosshair: { mode: 0 } });

// ---------------------------------------------------------------- routing
let timer = null;
function route() {
  const h = location.hash || "#/";
  clearInterval(timer); timer = null;
  const [, page, arg] = h.match(/^#\/?([a-z]*)\/?(.*)$/) || [];
  const name = page === "bot" ? "bot" : (page || "library");
  $$(".page").forEach(p => p.classList.toggle("on", p.id === "page-" + name));
  $$(".side a").forEach(a => a.classList.toggle("on", a.dataset.page === (name === "bot" ? "library" : name)));
  window.scrollTo(0, 0);
  ({ library: loadLibrary, bot: () => loadBot(decodeURIComponent(arg)), paper: openPaper, data: openData, settings: loadSettings }[name] || loadLibrary)();
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- library
let lib = null, filter = "all", sortKey = "d90", sortDir = -1, page = 0;
const PER_PAGE = 20;
function spark(vals) {
  if (!vals || vals.length < 2) return "";
  const w = 120, h = 32, lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const pts = vals.map((v, i) => `${(i / (vals.length - 1) * w).toFixed(1)},${(h - 3 - (v - lo) / span * (h - 6)).toFixed(1)}`).join(" ");
  const color = vals[vals.length - 1] >= vals[0] ? "#22b45a" : "#e5484d";
  return `<svg class="spark" width="${w}" height="${h}"><polyline fill="none" stroke="${color}" stroke-width="1.6" points="${pts}"/></svg>`;
}
// "spy pf>1.5 win>55 dd<20 p30>2 p90>5 sharpe>0.8 paper": numbers in %, like the screener in the screenshots
function matches(r, q) {
  const field = { pf: r.profit_factor, win: r.win_rate != null ? r.win_rate * 100 : null, dd: r.max_drawdown != null ? -r.max_drawdown * 100 : null,
    p7: r.d7 != null ? r.d7 * 100 : null, p30: r.d30 != null ? r.d30 * 100 : null, p90: r.d90 != null ? r.d90 * 100 : null,
    sharpe: r.sharpe, trades: r.n_trades, net: r.net_profit_pct != null ? r.net_profit_pct * 100 : null, dias: r.avg_bars };
  for (const tok of q.toLowerCase().split(/\s+/).filter(Boolean)) {
    const m = tok.match(/^([a-z0-9]+)([<>]=?)(-?[\d.,]+)$/);
    if (m && m[1] in field) {
      const v = field[m[1]], x = parseFloat(m[3].replace(",", "."));
      if (v === null || v === undefined) return false;
      if (m[2] === ">" && !(v > x) || m[2] === ">=" && !(v >= x) || m[2] === "<" && !(v < x) || m[2] === "<=" && !(v <= x)) return false;
    } else if (tok === "paper") { if (r.paper_status !== "active") return false; }
    else if (tok === "short") { if (!r.allow_short) return false; }
    else if (tok === "cartera" || tok === "sp500") { if (r.kind !== "universe") return false; }
    else if (!(r.symbol.toLowerCase().includes(tok) || r.name.toLowerCase().includes(tok) || r.strategy.includes(tok))) return false;
  }
  return true;
}
async function loadLibrary() {
  try { lib = await api("/api/library"); } catch (e) { $("#lib-count").textContent = e.message; return; }
  $("#n-tested").textContent = lib.n_tested;
  const ms = $("#missing");
  if (lib.missing.length) {
    ms.style.display = "block";
    ms.innerHTML = `Faltan los precios de <b>${lib.missing.map(esc).join(", ")}</b>. <button class="btn primary" id="get-missing">Descargarlos</button> <span id="gm-msg" class="muted"></span>`;
    $("#get-missing").onclick = async () => {
      try { await post("/api/data/ingest", { mode: "symbols", symbols: lib.missing }); $("#gm-msg").textContent = "Descargando… la tabla se actualizará sola.";
        clearInterval(timer); timer = setInterval(async () => { const j = await api("/api/data/job"); if (!j.running) { clearInterval(timer); loadLibrary(); } }, 2000);
      } catch (e) { $("#gm-msg").textContent = e.message; }
    };
  } else if (lib.need_universe) {
    ms.style.display = "block";
    ms.innerHTML = `Las pruebas en <b>cartera S&amp;P 500</b> necesitan los precios de las ~500 acciones del índice (unos minutos, una sola vez). ` +
      `<button class="btn primary" id="get-sp">Descargar el S&amp;P 500</button> <span id="gm-msg" class="muted"></span>`;
    $("#get-sp").onclick = async () => {
      try { await post("/api/data/ingest", { mode: "sp500" }); $("#gm-msg").innerHTML = 'Descargando… puedes ver el avance en <a href="#/data">Datos</a>. La tabla se actualizará sola.';
        clearInterval(timer); timer = setInterval(async () => { const j = await api("/api/data/job"); $("#gm-msg").textContent = j.running ? `Descargando ${j.done} de ${j.total} acciones…` : "";
          if (!j.running) { clearInterval(timer); loadLibrary(); } }, 3000);
      } catch (e) { $("#gm-msg").textContent = e.message; }
    };
  } else ms.style.display = "none";
  if (lib.rows.some(r => r.computing) && !timer) timer = setInterval(async () => {
    if (!$("#page-library").classList.contains("on")) return;
    try { lib = await api("/api/library"); renderLibrary(); if (!lib.rows.some(r => r.computing)) { clearInterval(timer); timer = null; } } catch (e) { /* next round */ }
  }, 3000);
  const sel = $("#ab-strategy");
  if (!sel.options.length) sel.innerHTML = lib.strategies.map(s => `<option value="${esc(s.key)}">${esc(s.name)}</option>`).join("");
  renderLibrary();
}
function renderLibrary() {
  if (!lib) return;
  const q = $("#q").value.trim();
  let rows = lib.rows.filter(r => (filter === "all" || (filter === "fav" && r.favorite) || (filter === "paper" && r.paper_status === "active") ||
    (filter === "universe" && r.kind === "universe") || (filter === "stock" && r.kind !== "universe")) && matches(r, q) && durOk(r));
  rows.sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    if (x === y) return 0; if (x === null || x === undefined) return 1; if (y === null || y === undefined) return -1;
    return (x > y ? 1 : -1) * sortDir;
  });
  const pages = Math.max(1, Math.ceil(rows.length / PER_PAGE)); page = Math.min(page, pages - 1);
  const shown = rows.slice(page * PER_PAGE, (page + 1) * PER_PAGE);
  $("#lib-count").innerHTML = `Mostrando <b>${shown.length}</b> de <b>${rows.length}</b> · orden: ${esc(HEAD[sortKey] || sortKey)} ${sortDir < 0 ? "↓" : "↑"}`;
  $("#page-n").innerHTML = `Página <b>${page + 1}</b> / ${pages}`;
  $("#prev").disabled = page === 0; $("#next").disabled = page >= pages - 1;
  const th = (k, label) => `<th class="sort" data-k="${k}">${label}${sortKey === k ? (sortDir < 0 ? " ↓" : " ↑") : " ⇅"}</th>`;
  const where = (r) => r.kind === "universe" ? `<b>S&amp;P 500</b><span class="kind" title="Escanea todo el índice; como máximo ${r.max_positions} posiciones a la vez">cartera · ${r.max_positions} a la vez</span>`
    : `<b>${esc(r.symbol)}</b><span class="kind one">una acción</span>`;
  $("#lib").innerHTML = `<tr><th>★</th>${th("name", "Nombre")}<th>Curva</th>${th("symbol", "Dónde")}${th("d7", "7 días")}${th("d30", "30 días")}${th("d90", "90 días")}` +
    `${th("net_profit_pct", "Total")}${th("win_rate", "Ganadoras")}${th("profit_factor", "F. benef.")}` +
    `${th("avg_bars", "Días media")}` +
    `<th title="Probada en cada acción del S&amp;P 500 por separado: en qué parte de ellas gana">Gana en</th><th></th></tr>` +
    shown.map(r => r.error ? `<tr class="lr" data-id="${esc(r.id)}"><td></td><td class="name">${esc(r.name)}</td><td>${where(r)}</td><td colspan="9" class="muted">${esc(r.error)}</td><td></td></tr>`
      : r.computing ? `<tr class="lr" data-id="${esc(r.id)}"><td></td><td class="name">${esc(r.name)}</td><td></td><td>${where(r)}</td><td colspan="8" class="muted">` +
        `${r.computing.state === "error" ? `<span class="bad">${esc(r.computing.msg)}</span>` : `<span class="spin"></span>${esc(r.computing.msg || "calculando")}…`}</td><td></td></tr>`
      : `<tr class="lr" data-id="${esc(r.id)}"><td><button class="star ${r.favorite ? "on" : ""}" data-fav="${esc(r.id)}">${r.favorite ? "★" : "☆"}</button></td>` +
      `<td class="name">${esc(r.name)}${r.custom ? '<span class="badge">ajustada</span>' : ""}${r.in_position ? '<span class="badge">dentro</span>' : ""}</td>` +
      `<td>${spark(r.spark)}</td><td>${where(r)}</td>` +
      `<td>${pill(r.d7)}</td><td>${pill(r.d30)}</td><td>${pill(r.d90)}</td><td>${pill(r.net_profit_pct, x => x > 0, 0)}</td>` +
      `<td>${pct(r.win_rate)}</td><td>${pill(r.profit_factor, x => x > 1, 2, v => nf(v, 2))}</td><td title="Sesiones que se aguanta de media cada operación">${days(r.avg_bars)}</td>` +
      `<td>${r.kind === "universe" && r.breadth != null ? `<span class="${r.breadth >= 0.6 ? "up" : "down"}" title="de las acciones del S&amp;P 500, probándola en cada una por separado">${pct(r.breadth, 0)}</span>` : '<span class="muted">—</span>'}</td>` +
      `<td>${r.paper_status === "active" ? '<button class="copy off" data-open="1">En paper</button>' : '<button class="copy" data-open="1">Activar en paper</button>'}</td></tr>`).join("");
  $$("#lib th.sort").forEach(h => h.onclick = () => { const k = h.dataset.k; sortDir = sortKey === k ? -sortDir : -1; sortKey = k; renderLibrary(); });
  $$("#lib tr.lr").forEach(tr => tr.onclick = (ev) => {
    const fav = ev.target.closest("[data-fav]");
    if (fav) { ev.stopPropagation(); const r = lib.rows.find(x => x.id === fav.dataset.fav);
      post(`/api/bots/${encodeURIComponent(r.id)}/update`, { favorite: !r.favorite }).then(() => { r.favorite = !r.favorite; renderLibrary(); }); return; }
    location.hash = "#/bot/" + encodeURIComponent(tr.dataset.id) + (ev.target.closest("[data-open]") ? "?paper" : "");
  });
}
function durOk(r) {
  const d = $("#dur").value, b = r.avg_bars;
  if (!d) return true; if (b === null || b === undefined) return false;
  return d === "intra" ? b < 0.5 : d === "short" ? b >= 0.5 && b <= 5 : d === "weeks" ? b > 5 && b <= 40 : b > 40;
}
const days = (b) => b === null || b === undefined ? "—" : b < 0.5 ? '<span class="kind">intradía</span>' : nf(b, b < 10 ? 1 : 0) + " días";
$("#dur").onchange = () => { page = 0; renderLibrary(); };
const HEAD = { avg_bars: "días de media", name: "nombre", symbol: "dónde", d7: "7 días", d30: "30 días", d90: "90 días", net_profit_pct: "total", win_rate: "ganadoras", profit_factor: "factor de beneficio" };
$("#q").oninput = () => { page = 0; renderLibrary(); };
$$(".chip").forEach(c => c.onclick = () => { filter = c.dataset.filter; $$(".chip").forEach(x => x.classList.toggle("on", x === c)); page = 0; renderLibrary(); });
$("#prev").onclick = () => { page--; renderLibrary(); }; $("#next").onclick = () => { page++; renderLibrary(); };
$("#add-bot-open").onclick = () => { const p = $("#add-bot"); p.style.display = p.style.display === "none" ? "flex" : "none"; showStrategyInfo(); };
$("#ab-strategy").onchange = () => showStrategyInfo();
function showStrategyInfo() {
  const s = lib && lib.strategies.find(x => x.key === $("#ab-strategy").value);
  $("#ab-info").textContent = s ? s.summary + (s.universe ? "" : " (El autor la pensó para un solo activo.)") : "";
}
$("#ab-symbol").onfocus = () => { $$('input[name="ab-kind"]').forEach(x => x.checked = x.value === "stock"); };
$("#ab-go").onclick = async () => {
  $("#ab-msg").textContent = "Creando…";
  const uni = $('input[name="ab-kind"]:checked').value === "universe";
  try { const r = await post("/api/bots", { strategy: $("#ab-strategy").value, symbol: uni ? "SP500" : $("#ab-symbol").value,
      max_positions: uni ? +$("#ab-maxpos").value : null });
    if (r.need_universe) { $("#ab-msg").innerHTML = 'Creado. Falta descargar el S&amp;P 500: pulsa el botón del aviso de arriba.'; loadLibrary(); return; }
    $("#ab-msg").textContent = r.downloading ? "Descargando sus precios… (aparecerá en la tabla en unos segundos)" : "Creado.";
    if (r.downloading) { clearInterval(timer); timer = setInterval(async () => { const j = await api("/api/data/job"); if (!j.running) { clearInterval(timer); loadLibrary(); } }, 2000); }
    else location.hash = "#/bot/" + encodeURIComponent(r.id);
  } catch (e) { $("#ab-msg").textContent = e.message; }
};

// ---------------------------------------------------------------- bot detail
let bot = null, metricMode = "bt", chart = null, series = [], mcChart = null, mcSeries = [];
const REASON_ICON = { "stop": "🛑", "objetivo": "🎯" };
async function loadBot(id) {
  const wantPaper = id.endsWith("?paper"); id = id.replace(/\?paper$/, "");
  $("#b-name").textContent = "Cargando…";
  try { bot = await api(`/api/bots/${encodeURIComponent(id)}`); } catch (e) { $("#b-name").textContent = e.message; return; }
  const b = bot.bot, st = b.strategy, uni = b.kind === "universe";
  $("#b-icon").textContent = uni ? "500" : b.symbol.slice(0, 1);
  $("#b-icon").style.fontSize = uni ? "13px" : "";
  $("#b-name").textContent = st.name;
  const pg = $("#page-bot");
  pg.classList.toggle("wait", !!bot.computing);
  $("#b-computing").style.display = bot.computing ? "block" : "none";
  if (bot.computing) {
    const c = bot.computing;
    $("#b-sub").innerHTML = `<b>Cartera S&amp;P 500</b> · hasta ${b.max_positions} acciones a la vez`;
    $("#b-comp-bar").style.width = (c.total ? Math.round(100 * c.done / c.total) : 3) + "%";
    $("#b-comp-msg").innerHTML = c.state === "error" ? `<span class="bad">${esc(c.msg)}</span>` : `<span class="spin"></span>${esc(c.msg || "en cola")}…`;
    clearInterval(timer); timer = setInterval(async () => {
      try { const d = await api(`/api/bots/${encodeURIComponent(id)}`); if (!d.computing) { clearInterval(timer); timer = null; loadBot(id + (wantPaper ? "?paper" : "")); }
        else { $("#b-comp-bar").style.width = (d.computing.total ? Math.round(100 * d.computing.done / d.computing.total) : 3) + "%";
          $("#b-comp-msg").innerHTML = `<span class="spin"></span>${esc(d.computing.msg || "en cola")}…`; } } catch (e) { /* next round */ }
    }, 2500);
    return;
  }
  $$(".uonly").forEach(x => x.style.display = uni ? "" : "none");
  if (!uni && ["today", "breadth"].includes(($("#b-tabs button.on") || {}).dataset?.tab)) $("#b-tabs button[data-tab=perf]").click();
  $("#b-sub").innerHTML = (uni ? `<b>Cartera S&amp;P 500</b> · ${nf(bot.universe.n_symbols, 0)} acciones · hasta ${b.max_positions} a la vez (${nf(100 / b.max_positions, 0)}% del capital cada una)`
    : `<b>${esc(b.symbol)}</b>`) + ` · velas diarias · ${st.allow_short ? "largos y cortos" : "solo largos"} · datos hasta el ${esc(bot.last_bar)}`;
  $("#b-hold-label").textContent = uni ? "comprar y mantener el S&P 500 (SPY)" : "comprar y mantener la acción";
  $("#act-follow-txt").textContent = uni ? "comprar en la próxima apertura las acciones que el backtest tiene abiertas ahora (si no, empieza con las próximas señales)"
    : "si el backtest está dentro de una operación ahora, entrar también en la próxima apertura";
  const live = b.paper_status === "active";
  $("#b-status").innerHTML = live ? `<span class="badge live">● En paper desde ${when(b.activated_at)}</span>` : (b.paper_status === "stopped" ? '<span class="badge">Paper detenido</span>' : "");
  $("#b-fav").textContent = b.favorite ? "★ Favorito" : "☆ Favorito";
  $("#b-paper").textContent = live ? "Detener paper trading" : "Activar en paper";
  $("#b-summary").textContent = st.summary;
  $("#b-source").innerHTML = (st.source ? `Fuente: ${esc(st.source)}` : "") + (st.style ? ` · <span class="kind">${esc(st.style)}</span>` : "");
  $("#act-intra").style.display = st.day_trade ? "block" : "none";
  $("#b-notes").innerHTML = (st.day_trade ? "<b>Intradía:</b> cada operación se abre y se cierra el mismo día (orden al cierre). " : "") +
    (st.notes ? `<b>Cómo se ha programado:</b> ${esc(st.notes)}` : "");
  $("#b-params").innerHTML = Object.keys(b.params).length ? "Ajustes: " + Object.entries(b.params).map(([k, v]) => `<code>${esc(k)} = ${esc(v)}</code>`).join(" ") +
    (Object.keys(b.custom_params).length ? ' <span class="badge warn">cambiados respecto al original</span>' : "") : "";
  $("#b-rank").innerHTML = uni ? `Si un día hay más acciones con señal que huecos libres, elige: <b>${esc(st.rank_rule)}</b>` +
    (st.rank_by_author ? " (regla del autor)." : " (regla fija de la app: el autor no da ninguna).") : "";
  const ot = bot.open_trade;
  if (uni) {
    const pos = bot.positions, nx = bot.next;
    $("#b-next").innerHTML = (pos.length ? `Ahora mismo la cartera tiene <b>${pos.length}</b> posición${pos.length > 1 ? "es" : ""}: ${pos.map(p => `<b>${esc(p.symbol)}</b>`).join(", ")}.` : "Ahora mismo la cartera no tiene posiciones.") +
      ` Con el cierre del ${esc(nx.day)}: ` + (nx.exits.length ? `vendería ${nx.exits.map(esc).join(", ")}; ` : "") +
      (nx.buys.filter(x => x.chosen).length ? `compraría <b>${nx.buys.filter(x => x.chosen).map(x => esc(x.symbol)).join(", ")}</b>.` : "no compraría nada.") +
      ' <a href="#" id="go-today">ver detalle</a>';
    $("#go-today").onclick = (ev) => { ev.preventDefault(); $("#b-tabs button[data-tab=today]").click(); $("#b-tabs").scrollIntoView({ behavior: "smooth" }); };
  } else
  $("#b-next").innerHTML = (ot ? `Ahora mismo el backtest está <b>${ot.side === "largo" ? "comprado" : "en corto"}</b> desde el ${ot.entry} (${pct(ot.pnl_pct, 1, true)}).` : "Ahora mismo el backtest está fuera del mercado.") +
    (bot.next_action ? ` Con el último cierre: <b>${esc(bot.next_action)}</b>.` : "");
  $$("#page-bot .toggle button").forEach(x => x.classList.toggle("on", x.dataset.mode === metricMode));
  renderMetrics(); drawChart(); renderPerf(); renderTrades(); renderMonthly(); renderMC(); renderBench(); renderReport(); renderPaperLog();
  if (uni) { renderToday(); renderBreadth(); }
  $("#b-audit").innerHTML = "Pulsa la pestaña para calcularla."; auditLoaded = false;
  if ($("#b-tabs button.on").dataset.tab === "audit") loadAudit();
  $("#b-activate").style.display = wantPaper && !live ? "flex" : "none";
}
function metricBox(label, v, cls = "") { return `<div class="metric"><div class="l">${label}</div><div class="v ${cls}">${v}</div></div>`; }
function renderMetrics() {
  const p = bot.paper, m = metricMode === "live" ? (p && p.metrics) || {} : bot.summary;
  if (metricMode === "live" && !(p && p.metrics && p.metrics.n_trades !== undefined)) {
    $("#b-metrics").innerHTML = '<p class="muted">Aún no hay resultados en paper trading para este bot.</p>'; $("#b-windows").innerHTML = ""; return; }
  const sgn = (v) => v > 0 ? "up" : v < 0 ? "down" : "";
  $("#b-metrics").innerHTML = metricBox("Beneficio neto", pct(m.net_profit_pct, 1, true), sgn(m.net_profit_pct)) + metricBox("Ganadoras", pct(m.win_rate)) +
    metricBox("Factor de beneficio", nf(m.profit_factor, 2)) + metricBox("Caída máxima", pct(m.max_drawdown)) + metricBox("Operaciones", m.n_trades ?? "—") +
    (metricMode === "bt" ? metricBox("Días de media por operación", m.avg_bars == null ? "—" : m.avg_bars < 0.5 ? "intradía" : nf(m.avg_bars, 1)) : "") +
    (metricMode === "bt" ? metricBox("Media por operación", pct(m.ev_pct, 2, true), sgn(m.ev_pct)) +
      metricBox("Ganancia media / operación", usd(m.ev_payoff), sgn(m.ev_payoff)) + metricBox("Al año", pct(m.cagr, 1, true)) +
      metricBox("Sharpe", nf(m.sharpe, 2)) + metricBox("Tiempo invertido", bot.bot.strategy.day_trade ? "solo de día" : pct(m.exposure, 0)) : "");
  $("#b-windows").innerHTML = [["7 días", m.d7], ["30 días", m.d30], ["90 días", m.d90]].map(([l, v]) => `<div>${l}<b class="${sgn(v)}">${pct(v, 1, true)}</b></div>`).join("");
}
$$("#page-bot .toggle button").forEach(x => x.onclick = () => { metricMode = x.dataset.mode; $$("#page-bot .toggle button").forEach(y => y.classList.toggle("on", y === x)); renderMetrics(); });
function drawChart() {
  if (!chart) chart = LightweightCharts.createChart($("#b-chart"), { ...chartOpts(), localization: { priceFormatter: v => nf(v, 0) + "%" } });
  series.forEach(s => chart.removeSeries(s)); series = [];
  if ($("#b-hold").checked) { const h = chart.addLineSeries({ color: "#a3a9b6", lineWidth: 1, title: "mantener", lastValueVisible: false, priceLineVisible: false }); h.setData(bot.hold); series.push(h); }
  if ($("#b-dd").checked) { const d = chart.addAreaSeries({ lineColor: "#e5484d", topColor: "rgba(229,72,77,0.05)", bottomColor: "rgba(229,72,77,0.25)", lineWidth: 1, title: "caída", priceLineVisible: false }); d.setData(bot.drawdown); series.push(d); }
  const s = chart.addLineSeries({ color: "#22b45a", lineWidth: 2, title: "backtest", priceLineVisible: false }); s.setData(bot.equity); series.push(s);
  const live = bot.paper && bot.paper.live && bot.paper.live.length ? bot.paper.live : null;
  if (live) {
    const l = chart.addLineSeries({ color: "#f0499b", lineWidth: 2, title: "paper", priceLineVisible: false }); l.setData(live); series.push(l);
    s.setMarkers([{ time: live[0].time, position: "aboveBar", color: "#f0499b", shape: "arrowDown", text: "Paper trading" }]);
  }
  $("#b-chart-note").textContent = `Rentabilidad acumulada (%) desde ${bot.summary.first} con ${nf(bot.capital, 0)} $ iniciales; ` +
    (bot.bot.kind === "universe" ? `hasta ${bot.config.max_positions} acciones a la vez, ${nf(bot.config.slot_pct, 0)}% del capital cada una, ` : `${bot.config.size_pct}% del capital por operación, `) +
    `costes ${nf(bot.config.slippage_bps / 100, 2)}% por lado.` + (live ? " En rosa, los resultados reales en paper trading desde su activación." : "");
  chart.timeScale().fitContent();
}
$("#b-hold").onchange = drawChart; $("#b-dd").onchange = drawChart;
const KEY = [["first_trade", "Primera operación", v => v], ["last_trade", "Última operación", v => v], ["sharpe", "Sharpe", v => nf(v)], ["sortino", "Sortino", v => nf(v)],
  ["calmar", "Calmar", v => nf(v)], ["longest_dd_days", "Caída más larga (días)", v => v], ["volatility", "Volatilidad anual", v => pct(v)],
  ["skew", "Asimetría", v => nf(v)], ["kurtosis", "Curtosis", v => nf(v)], ["expected_daily", "Esperado diario", v => pct(v, 2)],
  ["expected_monthly", "Esperado mensual", v => pct(v, 2)], ["expected_yearly", "Esperado anual", v => pct(v, 1)], ["kelly", "Criterio de Kelly", v => pct(v)],
  ["var_daily", "VaR diario (95%)", v => pct(v, 2)], ["cvar_daily", "Pérdida esperada (CVaR)", v => pct(v, 2)], ["max_consec_wins", "Máx. ganadoras seguidas", v => v],
  ["n_wins", "Operaciones ganadoras", v => v], ["max_consec_losses", "Máx. perdedoras seguidas", v => v], ["n_losses", "Operaciones perdedoras", v => v],
  ["gain_pain", "Ganancia / dolor", v => nf(v)], ["payoff", "Ganancia media / pérdida media", v => nf(v)], ["common_sense", "Common sense ratio", v => nf(v)],
  ["tail_ratio", "Ratio de colas", v => nf(v)], ["outlier_win", "Ratio de ganancias extremas", v => nf(v)], ["outlier_loss", "Ratio de pérdidas extremas", v => nf(v)],
  ["recovery_factor", "Factor de recuperación", v => nf(v)], ["ulcer", "Índice Ulcer", v => nf(v, 3)], ["serenity", "Índice de serenidad", v => nf(v)]];
function renderPerf() {
  const wd = bot.weekday, mx = Math.max(0.01, ...wd.map(x => x.value || 0));
  $("#b-weekday").innerHTML = wd.map(x => `<div class="b"><span>${nf(x.value, 2)}</span><i style="height:${(x.value || 0) / mx * 85}%"></i>${x.day}</div>`).join("");
  $("#b-exposure").textContent = bot.bot.kind === "universe" ? `De media tiene ${nf(bot.avg_positions, 1)} posiciones abiertas; ${pct(bot.summary.exposure, 0)} de los días con al menos una.`
    : `Media: ${pct(bot.summary.exposure, 0)} de los días con una posición abierta.`;
  const k = bot.key_metrics || {};
  $("#b-key").innerHTML = KEY.filter(([key]) => key in k).map(([key, label, f]) => `<div><span>•</span>${label}: <b>${k[key] === null ? "—" : f(k[key])}</b></div>`).join("") || '<p class="muted">Pocos datos.</p>';
}
function renderTrades() {
  const uni = bot.bot.kind === "universe";
  table($("#b-trades"), bot.trades, [...(uni ? [["Acción", t => `<b>${esc(t.symbol)}</b>`]] : []), ["Lado", t => t.side === "largo" ? '<span class="up">largo</span>' : '<span class="down">corto</span>'],
    ["Entrada", t => t.entry], ["Salida", t => t.exit], ["Precio entrada", t => usd(t.entry_price)], ["Precio salida", t => usd(t.exit_price)],
    ["Resultado", t => `<span class="${t.pnl >= 0 ? "up" : "down"}">${usd(t.pnl)} (${pct(t.pnl_pct, 2, true)})</span>`], ["Días", t => t.bars],
    ["Motivo", t => (REASON_ICON[t.reason] || "") + " " + esc(t.reason)]], "Sin operaciones");
}
function renderToday() {
  const nx = bot.next, pos = bot.positions;
  $("#b-today").innerHTML = `<div class="explain">Así funciona cada día: después del cierre mira todas las acciones del S&amp;P 500; en la apertura siguiente
    vende las que dan señal de salida y, si quedan huecos (máximo ${bot.config.max_positions}), compra las que dan señal de entrada, empezando por las mejor
    clasificadas (${esc(bot.bot.strategy.rank_rule)}).</div>` +
    `<h4>Posiciones abiertas ahora en el backtest</h4><table id="td-pos"></table>` +
    `<h4>Señales del cierre del ${esc(nx.day)}</h4><p class="small">${nx.n_signals} acciones dan señal de entrada · huecos libres en la próxima apertura: <b>${nx.free}</b>` +
    (nx.exits.length ? ` · se venderían: <b>${nx.exits.map(esc).join(", ")}</b>` : "") + `</p><table id="td-buys"></table>` +
    `<p class="muted small">En verde, las que compraría. El resto da señal pero no hay sitio para ellas.</p>`;
  table($("#td-pos"), pos, [["Acción", p => `<b>${esc(p.symbol)}</b>`], ["Lado", p => p.side], ["Desde", p => p.entry], ["Entrada", p => usd(p.entry_price)],
    ["Último cierre", p => usd(p.price)], ["Resultado", p => `<span class="${p.pnl_pct >= 0 ? "up" : "down"}">${pct(p.pnl_pct, 2, true)}</span>`],
    ["Stop", p => p.stop ? usd(p.stop) : "—"], ["Mañana", p => p.exit_next ? '<span class="bad">se vende</span>' : "sigue"]], "Ninguna");
  const el = $("#td-buys");
  if (!nx.buys.length) { el.innerHTML = '<tr><td class="muted">Ninguna acción da señal de entrada con este cierre.</td></tr>'; return; }
  el.innerHTML = `<tr><th>#</th><th>Acción</th><th>Señal</th><th>Cierre</th><th>Clasificación</th><th></th></tr>` +
    nx.buys.map((c, i) => `<tr class="${c.chosen ? "chosen" : ""}"><td>${i + 1}</td><td><b>${esc(c.symbol)}</b></td><td>${c.direction > 0 ? "compra" : "venta en corto"}</td>` +
      `<td>${usd(c.close)}</td><td>${nf(c.rank, c.rank > 1e5 ? 0 : 2)}</td><td>${c.chosen ? "✔ la compraría" : '<span class="muted">sin hueco</span>'}</td></tr>`).join("");
}
let breadthSort = "net";
function renderBreadth() {
  const b = bot.breadth, sm = b.summary;
  const rows = b.rows.slice().sort((x, y) => (y[breadthSort] ?? -1e9) - (x[breadthSort] ?? -1e9));
  $("#b-breadth").innerHTML = `<div class="explain">La estrategia probada en <b>cada acción por separado</b> (100% de una cuenta en esa acción, desde que entró en el índice).
    Sirve para ver si funciona <b>en general</b> o solo en unas pocas acciones con suerte.</div>` +
    (sm.n_used ? `<div class="metrics">${metricBox("Gana en", pct(sm.profitable, 0) + " de las acciones", sm.profitable >= 0.6 ? "up" : "down")}` +
      metricBox("Mejor que mantener en", pct(sm.beats_hold, 0)) + metricBox("Resultado mediano", pct(sm.median_net, 0, true), sm.median_net >= 0 ? "up" : "down") +
      metricBox("Factor de beneficio (todas juntas)", nf(sm.pooled_pf, 2)) + metricBox("Operaciones en total", nf(sm.trades, 0)) +
      metricBox("Ganadoras", pct(sm.win_rate)) + metricBox("Media por operación", pct(sm.avg_trade_pct, 2, true)) + `</div>` : '<p class="muted">Pocas operaciones por acción.</p>') +
    `<p class="muted small">${bot.universe.dated ? "Cada acción solo se opera desde la fecha en que entró en el S&amp;P 500." : "No se conocen las fechas de entrada al índice: se usa toda la historia de cada acción."}
     Ojo: la lista es la de hoy; las empresas que salieron del índice (o quebraron) no están, y eso hace que los resultados parezcan algo mejores de lo que serían.</p>` +
    `<p class="small">Ordenar por: <button class="link" data-bs="net">resultado</button> · <button class="link" data-bs="profit_factor">factor de beneficio</button> · ` +
    `<button class="link" data-bs="trades">operaciones</button> · <button class="link" data-bs="win_rate">ganadoras</button></p><div class="scroll"><table id="br-rows"></table></div>`;
  table($("#br-rows"), rows, [["Acción", r => `<b>${esc(r.symbol)}</b>`], ["Resultado", r => `<span class="${r.net >= 0 ? "up" : "down"}">${pct(r.net, 0, true)}</span>`],
    ["Mantener la acción", r => pct(r.hold, 0, true)], ["Factor de beneficio", r => nf(r.profit_factor, 2)], ["Ganadoras", r => pct(r.win_rate, 0)],
    ["Operaciones", r => r.trades], ["Caída máx.", r => pct(r.max_drawdown, 0)], ["Desde", r => r.since || "—"]], "Sin datos");
  $$("#b-breadth [data-bs]").forEach(x => x.onclick = () => { breadthSort = x.dataset.bs; renderBreadth(); });
}
const MONTHS = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"];
function heat(v) {
  if (v === null || v === undefined) return "";
  const a = Math.min(1, Math.abs(v) / 0.08);
  return `background:${v >= 0 ? `rgba(34,180,90,${0.1 + a * 0.5})` : `rgba(229,72,77,${0.1 + a * 0.5})`}`;
}
function renderMonthly() {
  const rows = bot.monthly.slice().reverse();
  $("#b-monthly").innerHTML = `<tr><th>Año</th>${MONTHS.map(m => `<th>${m}</th>`).join("")}<th>Año</th></tr>` +
    rows.map(r => `<tr><td class="y">${r.year}</td>${MONTHS.map((_, i) => { const v = r.months[i + 1]; return `<td style="${heat(v)}">${v === undefined || v === null ? "" : pct(v, 1)}</td>`; }).join("")}` +
      `<td style="${heat(r.total)}"><b>${pct(r.total, 1, true)}</b></td></tr>`).join("");
}
function renderMC() {
  const mc = bot.monte_carlo;
  if (!mc) { $("#b-mc-kpis").innerHTML = '<p class="muted">Hacen falta al menos 10 operaciones.</p>'; $("#b-mc-chart").style.display = "none"; return; }
  $("#b-mc-chart").style.display = "block";
  $("#b-mc-kpis").innerHTML = metricBox("Resultado mediano", pct(mc.final["50"], 1, true)) + metricBox("5% peor de los casos", pct(mc.final["5"], 1, true), mc.final["5"] < 0 ? "down" : "") +
    metricBox("5% mejor de los casos", pct(mc.final["95"], 1, true)) + metricBox("Probabilidad de perder", pct(mc.p_loss, 0)) +
    metricBox("Caída máx. mediana", pct(mc.max_drawdown["50"])) + metricBox("Caída máx. (5% peor)", pct(mc.max_drawdown["95"]));
  if (!mcChart) mcChart = LightweightCharts.createChart($("#b-mc-chart"), { ...chartOpts(), localization: { priceFormatter: v => nf(v, 0) + "%" },
    timeScale: { borderColor: "#ececf3", tickMarkFormatter: (t) => "op. " + t } });
  mcSeries.forEach(s => mcChart.removeSeries(s)); mcSeries = [];
  [["5", "#e5484d", "5% peor"], ["50", "#2d8cff", "mediana"], ["95", "#22b45a", "5% mejor"]].forEach(([k, c, t]) => {
    const s = mcChart.addLineSeries({ color: c, lineWidth: 2, title: t, priceLineVisible: false });
    s.setData(mc.band_trades.map((n, i) => ({ time: n, value: mc.bands[k][i] * 100 }))); mcSeries.push(s);
  });
  mcChart.timeScale().fitContent();
}
let auditLoaded = false;
async function loadAudit() {
  if (auditLoaded) return; auditLoaded = true;
  if (!/spin/.test($("#b-audit").innerHTML)) $("#b-audit").innerHTML = "Calculando (prueba la estrategia con otros ajustes, otros costes y otros periodos)…";
  let a; try { a = await api(`/api/bots/${encodeURIComponent(bot.bot.id)}/audit`); } catch (e) { $("#b-audit").textContent = e.message; return; }
  if (a.computing !== undefined) {
    const c = a.computing || {};
    $("#b-audit").innerHTML = `<p><span class="spin"></span>${esc(c.msg || "en cola")}…</p><p class="muted small">La auditoría de una cartera repite la simulación muchas veces
      (con otros costes, otras mitades, 100 «monos» que compran al azar y otros ajustes): tarda unos minutos la primera vez. Puedes seguir mirando el resto.</p>`;
    const id = bot.bot.id;
    setTimeout(() => { if (bot && bot.bot.id === id && $("#page-bot").classList.contains("on")) { auditLoaded = false; loadAudit(); } }, 3000);
    return;
  }
  const V = { "sólida": "✅ Parece sólida", "dudosa": "⚠️ Dudosa", "frágil": "❌ Frágil" };
  $("#b-audit").innerHTML = `<div class="verdict">${V[a.verdict] || a.verdict}: pasa ${a.passed} de ${a.total} comprobaciones</div>` +
    `<p class="muted small">Ninguna comprobación garantiza el futuro: la prueba de verdad es el paper trading con días nuevos.</p><ul class="checks">` +
    a.checks.map(c => `<li><span class="ic">${c.ok === null ? "➖" : c.ok ? "✅" : "❌"}</span><div><b>${esc(c.label)}</b><span class="muted small">${esc(c.detail)}</span>` +
      (c.symbols ? `<details><summary class="small">ver acciones</summary><table class="small">${c.symbols.map(s => `<tr><td>${esc(s.symbol)}</td><td class="${s.net >= 0 ? "up" : "down"}">${pct(s.net, 0, true)}</td><td>FB ${nf(s.profit_factor, 2)}</td><td class="muted">mantener ${pct(s.hold, 0, true)}</td></tr>`).join("")}</table></details>` : "") +
      (c.monkeys ? `<details><summary class="small">ver los monos</summary><p class="small">Resultado de cada cartera al azar (de peor a mejor): ${c.monkeys.map(v => pct(v, 0, true)).join(" · ")}</p></details>` : "") +
      (c.variants ? `<details><summary class="small">ver variantes</summary><table class="small">${c.variants.map(v => `<tr><td>${esc(v.param)} = ${esc(v.value)}</td><td class="${v.net >= 0 ? "up" : "down"}">${pct(v.net, 0, true)}</td><td>Sharpe ${nf(v.sharpe, 2)}</td></tr>`).join("")}</table></details>` : "") +
      `</div></li>`).join("") + "</ul>";
}
$$("#b-tabs button").forEach(t => t.onclick = () => {
  $$("#b-tabs button").forEach(x => x.classList.toggle("on", x === t));
  $$(".tabbody").forEach(b => b.style.display = b.id === "tab-" + t.dataset.tab ? "block" : "none");
  if (t.dataset.tab === "audit") loadAudit();
  if (t.dataset.tab === "mc" && mcChart) setTimeout(() => mcChart.timeScale().fitContent(), 80);  // after it gets its size
});
function rangeBox(r, label, color) {
  const vals = [r.max, r.min, r.current, 0].filter(v => v !== null);
  const hi = Math.max(...vals), lo = Math.min(...vals), span = (hi - lo) || 1, y = (v) => 190 - (v - lo) / span * 170;
  return `<div class="rng"><div class="zero" style="top:${y(0)}px"></div>` +
    `<div class="bar" style="top:${y(r.max)}px;height:${Math.max(4, y(r.min) - y(r.max))}px;background:${color}22;border:1px solid ${color}"></div>` +
    `<div class="lab" style="top:${y(r.max) - 12}px;left:2%;background:${color}">Máx ${pct(r.max, 1, true)}</div>` +
    `<div class="lab" style="top:${y(r.current) - 12}px;left:72%;background:${color}">Actual ${pct(r.current, 1, true)}</div>` +
    `<div class="lab" style="top:${y(r.min) - 12}px;left:2%;background:${color}">Mín ${pct(r.min, 1, true)}</div>` +
    `<div class="t">${label}</div></div>`;
}
function renderBench() {
  $("#b-bench").innerHTML = rangeBox(bot.pnl_range.hold, bot.bot.kind === "universe" ? "Comprar y mantener el S&P 500 (SPY)" : `Comprar y mantener ${esc(bot.bot.symbol)}`, "#f5a623") + rangeBox(bot.pnl_range.strategy, "La estrategia", "#2d8cff");
}
const REP = [["net_profit", "Beneficio neto", "usd"], ["gross_profit", "Beneficio bruto", "usd"], ["gross_loss", "Pérdida bruta", "usd"],
  ["profit_factor", "Factor de beneficio", "num"], ["n_trades", "Operaciones", "int"], ["win_rate", "Ganadoras", "pct"],
  ["avg_trade", "Media por operación", "usd"], ["avg_win", "Ganancia media", "usd"], ["avg_loss", "Pérdida media", "usd"],
  ["payoff", "Ganancia media / pérdida media", "num"], ["largest_win", "Mayor ganancia", "usd"], ["largest_loss", "Mayor pérdida", "usd"],
  ["avg_bars", "Días medios por operación", "num"], ["max_consec_wins", "Máx. ganadoras seguidas", "int"], ["max_consec_losses", "Máx. perdedoras seguidas", "int"]];
function renderReport() {
  const s = bot.by_side, f = (k, t, side) => { const v = s[side][k]; return v === null || v === undefined ? "—" : t === "usd" ? usd(v) +
    (k.endsWith("profit") || k === "gross_loss" ? `<br><span class="muted small">${pct(s[side][k + "_pct"] ?? (v / bot.capital), 2, true)}</span>` : "") : t === "pct" ? pct(v) : t === "int" ? v : nf(v, 2); };
  $("#b-report").innerHTML = `<tr><th>Métrica</th><th>Todas</th><th>Largos</th><th>Cortos</th></tr>` +
    REP.map(([k, l, t]) => `<tr><td>${l}</td><td>${f(k, t, "all")}</td><td>${f(k, t, "long")}</td><td>${f(k, t, "short")}</td></tr>`).join("");
}
function renderPaperLog() {
  const p = bot.paper;
  if (!p) { $("#b-paperlog").innerHTML = "Este bot no está en paper trading. Pulsa <b>Activar en paper</b> para que opere solo en tu cuenta paper de Alpaca."; return; }
  $("#b-paperlog").classList.remove("muted");
  $("#b-paperlog").innerHTML = `<p>Capital asignado: <b>${usd(p.capital)}</b> (${nf(p.allocation_pct, 0)}% de la cuenta) · desde ${when(p.activated_at)}</p>` +
    (p.status === "active" ? `<p class="small"><button id="pl-recheck" class="btn ghost">Volver a revisar el último cierre</button>
      <span class="muted">Solo con la bolsa cerrada. Manda lo que falte (por ejemplo, entradas que se saltaron); lo ya enviado no se repite.</span>
      <span id="pl-msg"></span></p>` : "") +
    `<h4>Posiciones abiertas en Alpaca</h4><table id="pl-pos"></table>` +
    (p.pending_entries.length ? `<p class="small"><span class="badge warn">Entradas pendientes para la próxima apertura</span> ${p.pending_entries.map(x => `<b>${esc(x.symbol)}</b> (${esc(x.reason)})`).join(", ")}</p>` : "") +
    `<h4>Operaciones cerradas</h4><table id="pl-trades"></table><h4>Órdenes enviadas a Alpaca</h4><table id="pl-orders"></table><h4>Registro</h4><table id="pl-events"></table>`;
  const rc = $("#pl-recheck");
  if (rc) rc.onclick = async () => { $("#pl-msg").textContent = " Revisando…";
    try { const r = await post(`/api/bots/${encodeURIComponent(bot.bot.id)}/recheck`); $("#pl-msg").textContent = " " + r.state; setTimeout(() => loadBot(bot.bot.id), 1500); }
    catch (e) { $("#pl-msg").innerHTML = ` <span class="bad">${esc(e.message)}</span>`; } };
  table($("#pl-pos"), p.positions, [["Acción", x => `<b>${esc(x.symbol)}</b>`], ["Acciones", x => nf(x.qty, 0)], ["Precio medio", x => usd(x.avg_price)],
    ["Stop", x => x.stop ? usd(x.stop) : "—"], ["Objetivo", x => x.target ? usd(x.target) : "—"]], "Ninguna");
  table($("#pl-trades"), p.trades, [["Acción", t => `<b>${esc(t.symbol || "")}</b>`], ["Lado", t => t.side], ["Entrada", t => t.entry], ["Salida", t => t.exit], ["Acciones", t => nf(t.qty, 0)],
    ["Resultado", t => `<span class="${t.pnl >= 0 ? "up" : "down"}">${usd(t.pnl)} (${pct(t.pnl_pct, 2, true)})</span>`], ["Motivo", t => esc(t.reason)]], "Aún ninguna");
  const PURP = { entry: "entrada", exit: "salida", stop: "stop", target: "objetivo", protect: "protección" };
  const STATUS = { filled: "ejecutada", canceled: "cancelada", expired: "caducada", rejected: "rechazada", new: "pendiente",
    accepted: "pendiente", pending_new: "pendiente", held: "en espera", partially_filled: "ejecutada en parte", submitted: "enviada",
    done_for_day: "terminada por hoy", replaced: "sustituida" };
  table($("#pl-orders"), p.orders, [["Enviada", o => when(o.submitted)], ["Para", o => PURP[o.purpose] || o.purpose], ["Orden", o => `${o.side === "buy" ? "compra" : "venta"} ${nf(o.qty, 0)} ${esc(o.symbol || "")} (${esc(o.type)})`],
    ["Estado", o => esc(STATUS[o.status] || o.status) + (o.error ? ` <span class="bad small">${esc(o.error)}</span>` : "")], ["Ejecutada", o => o.filled_price ? `${usd(o.filled_price)} · ${when(o.filled)}` : "—"]], "Aún ninguna");
  table($("#pl-events"), p.events, [["Cuándo", e => when(e.at)], ["", e => esc(e.text)]], "Sin actividad");
}
$("#b-fav").onclick = async () => { await post(`/api/bots/${encodeURIComponent(bot.bot.id)}/update`, { favorite: !bot.bot.favorite }); loadBot(bot.bot.id); };
$("#b-hide").onclick = async () => {
  if (!confirm("¿Quitar este bot de la biblioteca? (se puede volver a crear)")) return;
  try { await post(`/api/bots/${encodeURIComponent(bot.bot.id)}/update`, { hidden: true }); location.hash = "#/"; } catch (e) { alert(e.message); }
};
$("#b-other").onclick = () => { const uni = bot.bot.kind === "universe"; location.hash = "#/"; setTimeout(() => { $("#add-bot").style.display = "flex"; $("#ab-strategy").value = bot.bot.strategy.key; showStrategyInfo();
  $$('input[name="ab-kind"]').forEach(x => x.checked = x.value === (uni ? "stock" : "universe")); if (uni) $("#ab-symbol").focus(); }, 300); };
$("#b-paper").onclick = async () => {
  if (bot.bot.paper_status === "active") {
    const close = confirm("¿Detener el paper trading de este bot?\n\nAceptar: también se cierra su posición en la próxima apertura.\nCancelar: no hacer nada.");
    if (!close) return;
    try { await post(`/api/bots/${encodeURIComponent(bot.bot.id)}/deactivate`, { close: true }); } catch (e) { alert(e.message); }
    loadBot(bot.bot.id); return;
  }
  $("#b-activate").style.display = "flex";
};
$("#act-cancel").onclick = () => $("#b-activate").style.display = "none";
$("#act-go").onclick = async () => {
  $("#act-msg").textContent = "Conectando con Alpaca…";
  try { const r = await post(`/api/bots/${encodeURIComponent(bot.bot.id)}/activate`, { allocation_pct: +$("#act-pct").value, follow_open: $("#act-follow").checked });
    $("#act-msg").textContent = `Activado con ${usd(r.capital)}.` + (r.follow ? ` En la próxima apertura comprará ${r.symbols.length > 1 || bot.bot.kind === "universe" ? r.symbols.join(", ") : ""} como el backtest.` : "");
    setTimeout(() => loadBot(bot.bot.id), 800);
  } catch (e) { $("#act-msg").innerHTML = `<span class="bad">${esc(e.message)}</span>` + (/Alpaca/.test(e.message) ? ' <a href="#/settings">Ir a Ajustes</a>' : ""); }
};

// ---------------------------------------------------------------- paper trading overview
function openPaper() { loadPaper(); timer = setInterval(loadPaper, 15000); }
async function loadPaper() {
  try {
    const [p, a] = await Promise.all([api("/api/paper"), api("/api/alpaca")]);
    if (!a.configured) $("#pp-account").innerHTML = 'No conectada. <a href="#/settings">Pon tus claves de paper trading en Ajustes</a>.';
    else if (a.error) $("#pp-account").innerHTML = `<span class="bad">No se puede conectar: ${esc(a.error)}</span>`;
    else { const ac = a.account, ck = a.clock;
      $("#pp-account").classList.remove("muted");
      $("#pp-account").innerHTML = `Cuenta paper <b>${esc(ac.account_number)}</b> · valor <b>${usd(ac.equity)}</b> · efectivo ${usd(ac.cash)}<br>` +
        `Bolsa: ${ck.is_open ? '<span class="good">abierta</span>' : "cerrada"} · próxima apertura ${when(ck.next_open)} · próximo cierre ${when(ck.next_close)}` +
        (ac.trading_blocked ? '<br><span class="bad">Alpaca indica que el trading está bloqueado en esta cuenta.</span>' : ""); }
    $("#pp-state").classList.remove("muted");
    $("#pp-state").innerHTML = `${esc(p.state)}<pre class="log">${esc(p.log.slice().reverse().join("\n")) || "—"}</pre>`;
    table($("#pp-bots"), p.bots, [["Bot", b => `<a href="#/bot/${encodeURIComponent(b.id)}">${esc(b.name)}</a>`],
      ["Dónde", b => b.kind === "universe" ? '<b>S&amp;P 500</b><span class="kind">cartera</span>' : `<b>${esc(b.symbol)}</b>`],
      ["Estado", b => b.status === "active" ? '<span class="badge live">en paper</span>' : '<span class="badge">detenido</span>'],
      ["Capital", b => `${usd(b.capital)} (${nf(b.allocation_pct, 0)}%)`], ["Posición", b => b.kind === "universe" ? (b.held.length ? b.held.map(esc).join(", ") : "—") : (b.position ? nf(b.position, 0) + " acciones" : "—")],
      ["Resultado cerrado", b => `<span class="${b.realized >= 0 ? "up" : "down"}">${usd(b.realized)}</span>`], ["Operaciones", b => b.trades],
      ["Desde", b => when(b.activated_at)]], "Ningún bot en paper todavía: abre uno en la Biblioteca y pulsa «Activar en paper».");
    table($("#pp-events"), p.events, [["Cuándo", e => when(e.at)], ["Bot", e => e.bot ? `<a href="#/bot/${encodeURIComponent(e.bot)}">${esc(e.bot)}</a>` : ""], ["", e => esc(e.text)]], "Sin actividad");
  } catch (e) { $("#pp-state").textContent = e.message; }
}
$("#pp-run").onclick = async () => { $("#pp-state").textContent = "Comprobando…"; try { await post("/api/paper/run"); } catch (e) { alert(e.message); } loadPaper(); };
$("#stop-all").onclick = async () => {
  if (!confirm("PARAR TODO el paper trading: todos los bots dejan de operar y se cancelan sus órdenes pendientes.\n\n¿Continuar?")) return;
  const close = confirm("¿Cerrar también sus posiciones abiertas en la próxima apertura?\n\nAceptar: sí, cerrarlas.\nCancelar: dejarlas abiertas en Alpaca.");
  try { const r = await post("/api/stop-all", { close }); alert(`Parados ${r.stopped.length} bots.`); } catch (e) { alert(e.message); }
  route();
};

// ---------------------------------------------------------------- data
let dataRows = [], jobWas = false;
function openData() { loadData(); timer = setInterval(loadJob, 2000); }
async function loadData() {
  try { const d = await api("/api/data/summary");
    $("#d-summary").classList.remove("muted");
    $("#d-summary").innerHTML = d.count ? `<b>${d.count}</b> acciones, del <b>${d.first}</b> al <b>${d.last}</b>.` +
      (d.has_benchmark ? "" : ` <span class="bad">Falta ${d.benchmark} (el mercado).</span>`) : '<span class="bad">No hay datos todavía.</span>';
    dataRows = d.symbols; renderData();
  } catch (e) { $("#d-summary").textContent = e.message; }
  loadJob();
}
function renderData() {
  const q = ($("#d-filter").value || "").toUpperCase();
  table($("#d-table"), dataRows.filter(r => !q || r.symbol.includes(q) || (r.name || "").toUpperCase().includes(q)).slice(0, 600),
    [["Símbolo", r => `<b>${esc(r.symbol)}</b>`], ["Empresa", r => esc(r.name || "—")], ["Sector", r => esc(r.sector || "—")], ["Desde", r => r.first], ["Hasta", r => r.last], ["Días", r => r.bars]]);
}
$("#d-filter").oninput = renderData;
async function loadJob() {
  let j; try { j = await api("/api/data/job"); } catch (e) { return; }
  const p = j.total ? Math.round(100 * j.done / j.total) : 0;
  $("#d-bar").style.width = (j.running ? p : (j.total ? 100 : 0)) + "%";
  ["#d-update", "#d-add", "#d-sp"].forEach(id => $(id).disabled = j.running);
  $("#d-job").innerHTML = j.running ? `Descargando <b>${esc(j.current || "")}</b> · ${j.done}/${j.total} (${p}%)` : (j.message ? "Última descarga: " + esc(j.message) : "");
  $("#d-log").textContent = (j.log || []).slice().reverse().join("\n");
  if (jobWas && !j.running) loadData();
  jobWas = j.running;
}
const ingest = async (body) => { try { const r = await post("/api/data/ingest", body); $("#d-job").textContent = `Empezando: ${r.symbols} acciones…`; } catch (e) { alert(e.message); } loadJob(); };
$("#d-update").onclick = () => ingest({ mode: "update" });
$("#d-add").onclick = () => ingest({ mode: "symbols", symbols: $("#d-symbols").value.split(/[,\s]+/) });
$("#d-sp").onclick = () => { if (confirm("Descargar ~500 acciones del S&P 500 desde 2010 (unos minutos). ¿Continuar?")) ingest({ mode: "sp500" }); };

// ---------------------------------------------------------------- settings
async function loadSettings() { loadAlpaca(); loadTelegram(); loadSync(); loadServer(); }
async function loadServer() {
  let s; try { s = await api("/api/status"); } catch (e) { $("#sv-msg").textContent = e.message; return; }
  $("#sv-where").innerHTML = s.server_mode ? '<span class="good">✔ Estás en la app del <b>servidor</b> (funciona 24/7).</span>'
    : "Estás en la app de <b>tu ordenador</b>. La guía para montar el servidor gratis está en <code>docs/SERVIDOR.md</code>.";
  $("#sv-paper").checked = !!s.paper_here; $("#sv-auto").checked = !!s.auto_update;
  $("#sv-pc").style.display = s.server_mode || !s.paper_here ? "none" : "block";
  $("#sv-srv").style.display = s.server_mode ? "block" : "none";
  $("#sync-card").style.display = s.server_mode ? "none" : "";
}
$("#sv-paper").onchange = async () => {
  const on = $("#sv-paper").checked;
  if (on && !confirm("¿Seguro? Si el servidor también hace paper trading, las órdenes se duplicarían.")) { $("#sv-paper").checked = false; return; }
  try { const r = await post("/api/settings/paper_here", { enabled: on }); $("#sv-msg").textContent = "Paper trading en este equipo: " + (on ? "activado." : "desactivado.") + " " + r.state; }
  catch (e) { $("#sv-msg").textContent = e.message; } loadServer();
};
$("#sv-auto").onchange = async () => { try { await post("/api/settings/auto_update", { enabled: $("#sv-auto").checked }); $("#sv-msg").textContent = "Guardado."; } catch (e) { $("#sv-msg").textContent = e.message; } };
$("#sv-handover").onclick = async () => {
  if (!confirm("Se guardará una copia de todo en tu carpeta de Descargas y ESTE ordenador dejará de mandar órdenes a Alpaca (lo hará el servidor). ¿Continuar?")) return;
  $("#sv-msg").textContent = "Guardando copia…";
  try { const r = await post("/api/sync/handover"); $("#sv-msg").innerHTML = `<span class="good">Listo.</span> Copia guardada en <b>${esc(r.path)}</b>. Ahora abre la app del servidor → Ajustes → «Cargar estos datos» y elige ese archivo.`; }
  catch (e) { $("#sv-msg").textContent = e.message; } loadServer();
};
async function waitBack() {
  for (let i = 0; i < 90; i++) { await new Promise(r => setTimeout(r, 3000)); try { await api("/api/ping"); location.reload(); return; } catch (e) { /* still restarting */ } }
  $("#sv-msg").textContent = "El servidor tarda en volver; recarga la página en un rato.";
}
$("#sv-upload").onclick = async () => {
  const f = $("#sv-file").files[0]; if (!f) { $("#sv-msg").textContent = "Elige primero el archivo qsts-datos.db.gz"; return; }
  if (!confirm("Los datos del servidor se sustituirán por los de ese archivo (se guarda una copia de seguridad). ¿Continuar?")) return;
  $("#sv-msg").textContent = `Subiendo ${nf(f.size / 1e6, 0)} MB…`;
  const send = (force) => fetch("/api/sync/upload" + (force ? "?force=true" : ""), { method: "POST", body: f, headers: { "Content-Type": "application/octet-stream" } });
  try {
    let r = await send(false);
    if (r.status === 409) { const d = await r.json(); if (!/MENOS datos/.test(d.detail) || !confirm(d.detail + "\n\n¿Cargarlo igualmente?")) throw new Error(d.detail); r = await send(true); }
    if (!r.ok) throw new Error((await r.json()).detail);
    $("#sv-msg").textContent = "Subido. El servidor se está reiniciando con tus datos…"; waitBack();
  } catch (e) { $("#sv-msg").textContent = e.message; }
};
$("#sv-update").onclick = async () => {
  $("#sv-msg").textContent = "Descargando la última versión (puede tardar unos minutos)…";
  try { await post("/api/server/update"); $("#sv-msg").textContent = "Actualizado. Reiniciando…"; waitBack(); } catch (e) { $("#sv-msg").textContent = e.message; }
};
async function loadAlpaca() {
  let a; try { a = await api("/api/alpaca"); } catch (e) { $("#al-status").textContent = e.message; return; }
  $("#al-status").classList.remove("muted");
  $("#al-status").innerHTML = !a.configured ? '<span class="bad">No conectada.</span> Sigue estos pasos (una sola vez):'
    : a.error ? `<span class="bad">Claves guardadas, pero Alpaca no responde: ${esc(a.error)}</span>`
    : `<span class="good">✔ Conectada</span> a la cuenta paper ${esc(a.account.account_number)} (valor ${usd(a.account.equity)}).`;
  $("#al-setup").style.display = a.configured && !a.error ? "none" : "block";
  if (a.configured && !a.error) $("#al-status").innerHTML += ' <button class="link" id="al-change">Cambiar claves</button>';
  const ch = $("#al-change"); if (ch) ch.onclick = () => { $("#al-setup").style.display = "block"; };
}
$("#al-save").onclick = async () => {
  $("#al-msg").textContent = "Comprobando con Alpaca…";
  try { const r = await post("/api/alpaca/keys", { key: $("#al-key").value, secret: $("#al-secret").value });
    $("#al-key").value = $("#al-secret").value = "";
    $("#al-msg").innerHTML = `<span class="good">Guardadas. Cuenta paper con ${usd(r.account.equity)}.</span>`; loadAlpaca();
  } catch (e) { $("#al-msg").innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
};
async function loadTelegram() {
  let t; try { t = await api("/api/telegram"); } catch (e) { $("#tg-status").textContent = e.message; return; }
  $("#tg-status").innerHTML = t.configured ? `<span class="good">✔ Conectado</span> (chat ${esc(t.chat_id)}). Recibirás cada orden y cada operación ejecutada.`
    : t.token_set ? '<span class="bad">Falta el paso 3–4</span>: abre tu bot, pulsa Iniciar y luego «Detectar mi chat».' : '<span class="bad">No configurado.</span> Pasos (una sola vez):';
  $("#tg-setup").style.display = t.configured ? "none" : "block"; $("#tg-test").disabled = !t.configured;
}
const tgDo = async (path, body, ok) => { $("#tg-msg").textContent = "…"; try { $("#tg-msg").textContent = ok(await post(path, body)); } catch (e) { $("#tg-msg").textContent = e.message; } loadTelegram(); };
$("#tg-save").onclick = () => tgDo("/api/telegram/token", { token: $("#tg-token").value }, r => { $("#tg-token").value = ""; return `Token guardado (bot @${r.bot}). Ahora el paso 3.`; });
$("#tg-detect").onclick = () => tgDo("/api/telegram/detect", {}, r => `Chat detectado: ${r.name}. Pulsa «Enviar mensaje de prueba».`);
$("#tg-test").onclick = () => tgDo("/api/telegram/test", {}, () => "Enviado: mira Telegram.");
async function loadSync() {
  let s; try { s = await api("/api/sync"); } catch (e) { $("#sync-body").textContent = e.message; return; }
  const d = s.downloaded;
  $("#sync-file-info").innerHTML = d ? `En Descargas hay: <b>${esc(d.name)}</b> (${nf(d.size / 1e6, 0)} MB, ${when(d.modified)}).` : `No hay ninguna copia descargada en ${esc(s.downloads_dir)}.`;
  $("#sync-load-file").disabled = !d;
  $("#sync-setup").style.display = s.enabled ? "none" : "block"; $("#sync-actions").style.display = s.enabled ? "flex" : "none";
  if (!s.enabled) {
    $("#sync-body").innerHTML = "Desactivado: los datos solo están en este ordenador." + (s.suggested_dir ? "" : ' <span class="bad">No encuentro OneDrive en este ordenador.</span>');
    if (!$("#sync-dir").value && s.suggested_dir) $("#sync-dir").value = s.suggested_dir; return;
  }
  const r = s.remote, sm = (r && r.summary) || {};
  let h = `Carpeta: <b>${esc(s.dir)}</b> · este ordenador: <b>${esc(s.machine)}</b><br>` +
    (r ? `Copia en la carpeta: de <b>${esc(r.machine)}</b>, ${when(r.saved_at)} (${nf(r.size / 1e6, 0)} MB, ${sm.bots ?? "?"} bots)` : "Aún no hay ninguna copia en la carpeta.");
  if (s.remote_smaller && s.remote_newer) h += `<p class="bad">La copia de la carpeta tiene MENOS datos que este ordenador: no la cargues salvo que sepas que es la buena.</p>`;
  if (s.loaded_at_start) h += `<p class="good">✔ Al abrir se cargaron los datos de ${esc(s.loaded_at_start.machine)} (${when(s.loaded_at_start.saved_at)}).</p>`;
  if (s.remote_newer) h += `<p class="bad">Hay una copia más reciente de ${esc(r.machine)}. Cárgala antes de usar la app aquí.</p>`;
  else h += `<p class="muted small">${s.local_changed ? "Este ordenador tiene cambios que se guardarán al cerrar la app." : "Todo guardado."}</p>`;
  $("#sync-body").innerHTML = h; $("#sync-body").classList.remove("muted");
  $("#sync-load").style.display = s.remote_newer ? "" : "none";
}
const syncDo = async (path, body, ok) => { $("#sync-msg").textContent = "Un momento…"; try { $("#sync-msg").textContent = ok(await post(path, body)); } catch (e) { $("#sync-msg").textContent = e.message; } loadSync(); };
$("#sync-enable").onclick = () => syncDo("/api/sync/enable", { dir: $("#sync-dir").value }, (s) => s.remote ? `Activado. Hay una copia de ${s.remote.machine}.` : "Activado.");
$("#sync-off").onclick = () => { if (confirm("¿Desactivar la copia entre ordenadores? (no se borra nada)")) syncDo("/api/sync/disable", {}, () => "Desactivado."); };
$("#sync-save").onclick = async () => {
  $("#sync-msg").textContent = "Guardando copia…";
  try { const r = await post("/api/sync/save", {}); $("#sync-msg").textContent = `Copia guardada (${nf(r.size / 1e6, 0)} MB).`; }
  catch (e) { if (confirm(e.message + "\n\n¿Guardar igualmente y SUSTITUIR esa copia?")) { try { await post("/api/sync/save", { force: true }); $("#sync-msg").textContent = "Copia guardada."; } catch (e2) { $("#sync-msg").textContent = e2.message; } } else $("#sync-msg").textContent = ""; }
  loadSync();
};
$("#sync-save-file").onclick = () => syncDo("/api/sync/save_file", {}, r => `Guardada: ${r.path} (${nf(r.size / 1e6, 0)} MB).`);
const loadCopy = async (path) => {
  if (!confirm("Se cargarán esos datos y SUSTITUIRÁN los de este ordenador (antes se guarda una copia de seguridad). QSTS se reiniciará sola. ¿Continuar?")) return;
  $("#sync-msg").textContent = "Preparando la copia (puede tardar un minuto)…";
  try { const r = await post(path, {}).catch(async (e) => { if (!/MENOS datos/.test(e.message) || !confirm(e.message + "\n\n¿Cargarla igualmente?")) throw e; return post(path, { force: true }); });
    $("#sync-msg").textContent = r.restarting ? "Listo: QSTS se está reiniciando con esos datos…" : "Listo: cierra QSTS y vuelve a abrirla.";
  } catch (e) { $("#sync-msg").textContent = e.message; }
};
$("#sync-load").onclick = () => loadCopy("/api/sync/load");
$("#sync-load-file").onclick = () => loadCopy("/api/sync/load_file");

// ---------------------------------------------------------------- start
api("/api/status").then(s => { $("#foot").innerHTML = `Versión ${esc((s.code_version || "").slice(0, 7))}${s.server_mode ? " · <b>servidor 24/7</b>" : ""} · paper trading: ${esc(s.trader)} · ` +
  (s.auto_update_state ? `precios: ${esc(s.auto_update_state)} · ` : "") +
  `Herramienta de investigación: los backtests no garantizan resultados futuros. Solo dinero ficticio (cuenta paper de Alpaca).`; }).catch(() => {});
route();
