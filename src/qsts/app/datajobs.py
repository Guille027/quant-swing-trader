"""Price-history download jobs (used by the CLI and, in a background thread, by the UI's "Datos" tab).

Every symbol goes through the quality engine before it is stored; nothing is filled or invented. A failed
symbol is reported with its reason and the job continues with the next one.
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from qsts.core.power import keep_awake
from qsts.data.bars import Timeframe, nyse_schedule
from qsts.data.quality import DataQualityError, QualityConfig, validate_and_clean
from qsts.data.repository import MarketDataRepository


FX_SERIES, FX_PAIR = "EURUSD", "EURUSD=X"  # US dollars per euro


def ingest_one(repo: MarketDataRepository, prov, symbol: str, start, end, asset_fields: dict | None = None,
               timeframe: Timeframe = Timeframe.D1) -> dict:
    raw = prov.get_bars(symbol, timeframe, pd.Timestamp(start), pd.Timestamp(end))
    # a missing intraday day is kept as a gap (never filled) instead of rejecting the whole download
    cfg = QualityConfig(max_missing_fraction=1.0, max_missing_gap=10 ** 9) if timeframe.intraday else QualityConfig()
    vb = validate_and_clean(raw, symbol, timeframe, cfg)
    repo.upsert_asset(symbol, **(asset_fields or {}))
    n = repo.store_bars(vb, prov.name)
    if timeframe is Timeframe.D1:  # splits/dividends come with the daily download
        acts = prov.get_corporate_actions(symbol)
        if len(acts):
            repo.store_corporate_actions(symbol, acts, prov.name)
    return {"symbol": symbol, "bars": n, "warnings": sorted({i.code for i in vb.report.issues})}


def ingest_earnings(repo: MarketDataRepository, prov, symbol: str) -> int:
    """Quarterly results calendar (past + upcoming). Returns the number of events stored."""
    ev = prov.get_earnings(symbol)
    return repo.store_earnings(symbol, ev, prov.name) if len(ev) else 0


def last_completed_session(now: pd.Timestamp | None = None) -> pd.Timestamp:
    now = now or pd.Timestamp.now(tz="UTC")
    sched = nyse_schedule(now - pd.Timedelta(days=10), now)
    return sched.index[sched["market_close"] <= now][-1]


@dataclass
class JobState:
    running: bool = False
    kind: str | None = None
    total: int = 0
    done: int = 0
    current: str | None = None
    ok: int = 0
    up_to_date: int = 0
    failed: dict = field(default_factory=dict)
    earnings_ok: int = 0       # symbols with at least one earnings event downloaded
    earnings_missing: int = 0  # symbols where the provider returned none or failed (never fatal)
    started_at: str | None = None
    finished_at: str | None = None
    message: str | None = None


class DataJobRunner:
    """One download job at a time, in a daemon thread."""

    def __init__(self, repo: MarketDataRepository, provider_factory: Callable[[], object],
                 on_finish: Callable[[], None] | None = None, pause: float = 0.2, retry_wait: float = 3.0):
        self.repo, self.provider_factory, self.on_finish = repo, provider_factory, on_finish
        self.pause, self.retry_wait = pause, retry_wait
        self.state = JobState()
        self.logs: deque[str] = deque(maxlen=200)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        self.logs.append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")

    def start(self, kind: str, symbols: list[str], start: str = "2010-01-01", *, incremental: bool = False,
              asset_fields: dict[str, dict] | None = None, memberships: tuple | None = None,
              bars: bool = True, earnings: bool = True, timeframe: Timeframe = Timeframe.D1) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop = threading.Event()
            self.state = JobState(running=True, kind=kind, total=len(symbols),
                                  started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self._thread = threading.Thread(target=self._run, daemon=True, name="datajob",
                                            args=(symbols, start, incremental, asset_fields or {}, memberships,
                                                  bars, earnings, Timeframe(timeframe)))
            self._thread.start()
            return True

    def _earnings(self, prov, sym: str) -> None:
        try:
            n = ingest_earnings(self.repo, prov, sym)
        except Exception as e:  # noqa: BLE001 - earnings are optional data; prices must not fail because of them
            n = 0
            self.log(f"{sym}: sin resultados trimestrales ({e!r})"[:200])
        if n:
            self.state.earnings_ok += 1
        else:
            self.state.earnings_missing += 1

    def _run(self, symbols, start, incremental, asset_fields, memberships, bars=True, earnings=True,
             timeframe: Timeframe = Timeframe.D1) -> None:
        intraday = timeframe.intraday
        earnings = earnings and not intraday
        keep_awake(True)
        try:
            prov = self.provider_factory()
            end = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
            last_done = last_completed_session()
            for sym, fields in asset_fields.items():  # names/sectors first, so memberships can be recorded
                self.repo.upsert_asset(sym, **fields)
            for sym in symbols:
                if self._stop.is_set():
                    self.log("Detenido por el usuario")
                    break
                self.state.current = sym
                if not bars:  # earnings-only job
                    if self.repo.last_bar(sym) is not None:
                        self._earnings(prov, sym)
                    self.state.done += 1
                    time.sleep(self.pause)
                    continue
                s = start
                if incremental and intraday:  # intraday: re-read the last days (Yahoo keeps only recent history)
                    last = self.repo.last_bar(sym, timeframe)
                    if last is not None:
                        s = (last - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
                elif incremental:
                    last = self.repo.last_bar(sym)
                    if last is not None and last >= last_done:
                        self.state.up_to_date += 1
                        if earnings:  # upcoming result dates still matter (earnings blackout)
                            self._earnings(prov, sym)
                        self.state.done += 1
                        continue
                    if last is not None:
                        s = (last - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
                for attempt in (1, 2):
                    try:
                        r = ingest_one(self.repo, prov, sym, s, end, asset_fields.get(sym), timeframe)
                        self.state.ok += 1
                        self.log(f"{sym}: {r['bars']} barras" + (f" (avisos: {', '.join(r['warnings'])})" if r["warnings"] else ""))
                        if earnings:
                            self._earnings(prov, sym)
                        break
                    except DataQualityError as e:
                        self.state.failed[sym] = f"datos rechazados: {e}"[:200]
                        self.log(f"{sym}: RECHAZADO ({e})"[:200])
                        break
                    except Exception as e:  # noqa: BLE001 - network hiccups: one retry, then report
                        if attempt == 1:
                            time.sleep(self.retry_wait)
                            continue
                        self.state.failed[sym] = repr(e)[:200]
                        self.log(f"{sym}: FALLO ({e!r})"[:200])
                self.state.done += 1
                time.sleep(self.pause)
            if hasattr(prov, "get_fx") and not self._stop.is_set() and not intraday:
                try:  # euro/dollar rate, to show amounts of EUR accounts in euros
                    self.repo.store_fx(FX_SERIES, prov.get_fx(FX_PAIR), prov.name)
                except Exception as e:  # noqa: BLE001 - optional
                    self.log(f"Cambio euro/dólar no disponible ({e!r})"[:200])
            if memberships is not None:
                universe, rows, source = memberships
                n = self.repo.set_memberships(universe, rows, source)
                self.log(f"Pertenencia a {universe} registrada para {n} acciones (fuente: {source})")
            self.state.message = (f"{self.state.ok} descargadas, {self.state.up_to_date} ya al día, "
                                  f"{len(self.state.failed)} con problemas"
                                  + (f"; resultados trimestrales: {self.state.earnings_ok} con datos, "
                                     f"{self.state.earnings_missing} sin datos" if earnings else ""))
            self.log("Terminado: " + self.state.message)
        except Exception as e:  # noqa: BLE001
            self.state.message = f"error: {e!r}"[:300]
            self.log(f"ERROR: {e!r}")
        finally:
            keep_awake(False)
            self.state.running = False
            self.state.current = None
            self.state.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if self.on_finish:
                self.on_finish()

    def stop(self) -> None:
        self._stop.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def status(self) -> dict:
        return asdict(self.state) | {"log": list(self.logs)[-40:]}


def sample_symbols(symbols: list[str], n: int, seed: int = 0) -> list[str]:
    """Random (not hand-picked) subset: avoids choosing today's winners. Same seed -> same sample."""
    if n >= len(symbols):
        return sorted(symbols)
    return sorted(random.Random(seed).sample(sorted(symbols), n))
