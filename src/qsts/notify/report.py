"""Telegram texts for the paper-trading session, built from `PaperTrading.view()` (plain function, no I/O).

Decisions are taken with each session's CLOSE and executed at the NEXT OPEN, so the evening message says what to do
at the next open. Stops and profit targets are hit DURING the day (only daily data is available): the message
reports them after the close and gives the prices to leave as stop / limit orders at the broker.
"""
from __future__ import annotations

from html import escape

import pandas as pd

TZ = "Europe/Madrid"
DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
REASON = {"stop": "stop de pérdidas", "stop_gap": "stop (abrió por debajo)", "target": "objetivo de beneficio alcanzado",
          "target_gap": "objetivo de beneficio (abrió por encima)", "signal_exit": "señal de salida",
          "time_stop": "tiempo máximo de la operación", "earnings_exit": "presenta resultados (salir antes)",
          "reversal": "señal contraria", "end_of_data": "fin de datos"}
PROFIT = {"target", "target_gap"}


def num(x, d: int = 2) -> str:
    """Spanish number format: 2.401,20"""
    if x is None or (isinstance(x, float) and x != x):
        return "—"
    s = f"{abs(float(x)):,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("−" if float(x) < 0 else "") + s


def money(x, cur: str) -> str:
    return f"{num(x)} {'€' if cur == 'EUR' else '$'}"


def usd(x) -> str:
    return f"{num(x)} $"


def signed(x, cur: str) -> str:
    return ("+" if (x or 0) >= 0 else "") + money(x, cur)


def pct(x, d: int = 1) -> str:
    return "—" if x is None else ("+" if x >= 0 else "") + num(100 * x, d) + " %"


def day_name(ts: pd.Timestamp, with_time: bool = False) -> str:
    t = ts.tz_convert(TZ) if ts.tz is not None else ts
    s = f"{DAYS[t.weekday()]} {t.day} {MONTHS[t.month - 1]}"
    return s + (f", {t.strftime('%H:%M')} h (hora de España)" if with_time else "")


def _qty(q) -> str:
    return num(q, 3)


def _sell_line(o: dict, cur: str) -> str:
    return (f"🔴 <b>VENDER {escape(o['symbol'])}</b>: todas ({_qty(o['qty'])} acciones, ≈ {money(o['approx_value'], cur)})"
            f" — {escape(REASON.get(o.get('reason'), str(o.get('reason'))))}")


def _buy_lines(o: dict, cur: str) -> list[str]:
    if o.get("likely") is False:
        return [f"⚪ {escape(o['symbol'])}: hay señal de compra, pero probablemente no quede efectivo (no hacer nada)"]
    head = (f"🟢 <b>COMPRAR {escape(o['symbol'])}</b>: ≈ {money(o['approx_value'], cur)} "
            f"(≈ {_qty(o['qty'])} acciones a ~{usd(o.get('last_close'))})")
    if o.get("partial"):
        head += " — parcial, se acaba el efectivo"
    det = [f"stop ≈ {usd(o['approx_stop'])}" if o.get("approx_stop") is not None else None,
           f"objetivo ≈ {usd(o['approx_target'])}" if o.get("approx_target") is not None else None,
           f"resultados en {o['earnings_in']:.0f} sesiones" if o.get("earnings_in") is not None else None]
    det = [x for x in det if x]
    return [head] + (["      " + " · ".join(det)] if det else [])


def closed_today(v: dict) -> list[dict]:
    return [t for t in v.get("closed") or [] if t.get("exit") == v.get("as_of")]


def daily_messages(v: dict) -> list[str]:
    """[alert (only when something must be sold or was closed today), daily summary]."""
    if not v.get("active"):
        return []
    cur = v.get("currency") or "USD"
    asof = pd.Timestamp(v["as_of"])
    nxt = pd.Timestamp(v["next_open"]) if v.get("next_open") else None
    when = day_name(nxt, with_time=True) if nxt is not None else "la próxima apertura"
    sells = [o for o in v.get("orders") or [] if o["action"] == "VENDER"]
    buys = [o for o in v.get("orders") or [] if o["action"] == "COMPRAR"]
    today = closed_today(v)
    out = []

    if sells or today:
        a = ["🔔 <b>QSTS · hay ventas</b>"]
        if sells:
            a.append(f"En la apertura del {when}:")
            a += [_sell_line(o, cur) for o in sells]
        if today:
            a.append(f"Cerradas hoy ({day_name(asof)}):")
            for t in today:
                icon = "💰" if t["reason"] in PROFIT else ("🛑" if str(t["reason"]).startswith("stop") else "⚪")
                a.append(f"{icon} {escape(t['symbol'])} {signed(t['pnl'], cur)} ({pct(t.get('pnl_pct'))}) — "
                         f"{escape(REASON.get(t['reason'], str(t['reason'])))} a ~{usd(t['exit_price'])}")
            a.append("Si tenías puestas en el bróker las órdenes de stop / objetivo, ya se habrán ejecutado; "
                     "si no, vende esas acciones en la apertura.")
        out.append("\n".join(a))

    s = [f"📊 <b>QSTS · cierre del {day_name(asof)}</b>",
         f"Valor: <b>{money(v['equity'], cur)}</b> ({signed(v['pnl'], cur)} · {pct(v['return'], 2)})"
         + (f" · SPY {pct(v['benchmark']['return'], 2)}" if v.get("benchmark") else "")]
    if v.get("stale_sessions"):
        s.append(f"⚠️ Faltan {v['stale_sessions']} día(s) de precios: lo de abajo puede estar atrasado.")
    s.append(f"\n<b>Para la apertura del {when}:</b>")
    if not sells and not buys:
        s.append("✅ Nada que comprar ni vender. Mantén lo que tienes.")
    s += [_sell_line(o, cur) for o in sells]
    for o in buys:
        s += _buy_lines(o, cur)
    pos = v.get("positions") or []
    if pos:
        s.append(f"\n📂 <b>Posiciones abiertas ({len(pos)})</b>:")
        for p in pos:
            extra = [f"stop {usd(p['stop'])}" if p.get("stop") is not None else None,
                     f"objetivo {usd(p['target'])}" if p.get("target") is not None else None,
                     f"resultados en {p['earnings_in']:.0f} ses." if p.get("earnings_in") is not None else None]
            s.append(f"• {escape(p['symbol'])} {pct(p.get('pnl_pct'))} ({signed(p.get('unrealized_pnl'), cur)}) · "
                     f"{p.get('bars_held', 0)} días · " + " · ".join(x for x in extra if x))
    else:
        s.append("\n📂 Sin posiciones abiertas.")
    if cur == "EUR" and v.get("fx"):
        s.append(f"\n<i>Cambio usado: 1 € = {num(v['fx'], 4)} $. Simulación con dinero ficticio: tú decides si operas.</i>")
    else:
        s.append("\n<i>Simulación con dinero ficticio: tú decides si operas.</i>")
    out.append("\n".join(s))
    return out


# ---------------------------------------------------------------------- intraday paper simulation
def intraday_day_message(view: dict, day: dict) -> str:
    """One recorded session of the intraday paper simulation (`day`: journal row as a dict with its trades)."""
    from qsts.data.bars import nyse_schedule
    cur = view.get("currency", "EUR")
    d = pd.Timestamp(day["day"])
    opens = nyse_schedule(d, d)["market_open"]
    open_t = opens.iloc[0] if len(opens) else None
    ds = {"5m": "velas de 5 min", "1h": "velas de 1 hora"}.get(view.get("dataset"), view.get("dataset"))
    lines = [f"📊 <b>Simulación intradía</b> · {escape(ds)} · {day_name(d.tz_localize(TZ) if d.tz is None else d)}",
             f"Regla: <i>{escape(view.get('rule', ''))}</i>"]
    trades = day.get("trades") or []
    if not trades:
        lines.append("Hoy la regla no ha encontrado ninguna operación.")
    else:
        lines.append(f"{len(trades)} operación(es):")
        for t in trades:
            at = (open_t + pd.Timedelta(minutes=t["entry_min"])).tz_convert(TZ).strftime("%H:%M") if open_t is not None else "?"
            icon = "🟢" if t["net"] >= 0 else "🔴"
            lines.append(f"{icon} {escape(t['symbol'])} {t['side']} a las {at} → {escape(t['exit'])} "
                         f"{pct(t['net'], 2)} ({signed(t['pnl'], cur)})")
    before = day["equity"] - day["pnl"]
    lines.append(f"<b>Resultado del día: {signed(day['pnl'], cur)}</b> ({pct(day['pnl'] / before if before else 0, 2)})"
                 + (f" · mantener las mismas acciones: {pct(day['passive'], 2)}" if day.get("passive") is not None else ""))
    lines.append(f"Cuenta simulada: {money(day['equity'], cur)} ({pct(view.get('return'), 2)} desde el "
                 f"{escape(str(view.get('start')))}, {view.get('n_days')} sesiones)")
    lines.append("<i>Calculado después del cierre con las velas de Yahoo: muestra lo que habría hecho la regla, "
                 "no son señales en directo. Nada de esto es una recomendación de inversión.</i>")
    return "\n".join(lines)
