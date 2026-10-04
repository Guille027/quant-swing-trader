"""Application context: wires settings, database, safety components and services together."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from sqlalchemy import func, select

from qsts.backtest.engine import CostModel
from qsts.config import Environment, Settings, load_settings
from qsts.core.kill_switch import KillSwitch
from qsts.core.modes import ModeController, SystemMode
from qsts.data.bars import Timeframe
from qsts.data.repository import MarketDataRepository
from qsts.db import models as m
from qsts.db.session import init_db, make_engine, session_factory
from qsts.execution.broker import PaperBroker
from qsts.execution.service import ExecutionService
from qsts.notify.service import LogChannel, NotificationService
from qsts.research.experiments import ExperimentTracker
from qsts.risk.engine import MICRO_LIVE, RiskEngine, RiskLimits
from qsts.strategy.lifecycle import StrategyRegistry


@dataclass
class AppContext:
    settings: Settings
    sf: object
    repo: MarketDataRepository
    kill_switch: KillSwitch
    modes: ModeController
    risk: RiskEngine
    notifier: NotificationService
    log_channel: LogChannel
    execution: ExecutionService
    tracker: ExperimentTracker
    registry: StrategyRegistry
    last_scan: object = None
    extra: dict = field(default_factory=dict)

    def load_bars(self, symbol: str, timeframe: Timeframe = Timeframe.D1) -> pd.DataFrame:
        return self.repo.load_bars(symbol, timeframe)

    def research_frame(self, symbol: str, timeframe: Timeframe = Timeframe.D1) -> pd.DataFrame:
        """The ONE way research code gets bars: stored RAW data re-validated (ValidatedBars contract), then
        backward-adjusted for splits and dividends from stored corporate actions (`raw_close` kept).
        Using the same loader everywhere keeps dataset hashes stable for reproduction."""
        from qsts.data.adjust import adjust
        from qsts.data.quality import validate_and_clean
        raw = self.load_bars(symbol, timeframe)[["open", "high", "low", "close", "volume"]]
        vb = validate_and_clean(raw, symbol, timeframe)
        return adjust(vb.df, self.repo.load_corporate_actions(symbol), "total")

    def adjusted_bars(self, symbol: str, timeframe: Timeframe = Timeframe.D1, asof=None) -> pd.DataFrame:
        """Stored RAW bars backward-adjusted with the corporate actions known at `asof` (ex_date <= asof),
        so a replay at a past time sees the price scale the system would have seen then. OHLCV only."""
        from qsts.data.adjust import adjust
        raw = self.load_bars(symbol, timeframe)[["open", "high", "low", "close", "volume"]]
        acts = self.repo.load_corporate_actions(symbol)
        if asof is not None:
            t = pd.Timestamp(asof)
            acts = acts[acts["ex_date"] <= (t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC"))]
        return adjust(raw, acts, "total")[["open", "high", "low", "close", "volume"]]

    def autoresearcher(self, cfg=None, log=None, stop_event=None):
        """AutoResearcher over every stored symbol except the benchmark, with Gemini if configured."""
        from qsts.data.quality import DataQualityError
        from qsts.research.autoresearch import AutoResearchConfig, AutoResearcher
        cfg = cfg or AutoResearchConfig(oos_start=self.settings.oos_start)
        starts = self.repo.membership_starts("SP500") if self.settings.pit_membership else {}
        data, cut = {}, 0
        for sym in self.symbols():
            if sym == self.settings.benchmark:
                continue
            try:
                df = self.research_frame(sym)
            except (DataQualityError, KeyError) as e:
                if log:
                    log(f"{sym} excluido: {e}")
                continue
            joined = starts.get(sym)
            if joined is not None and pd.Timestamp(joined, tz="UTC") > df.index[0]:
                df, cut = df[df.index >= pd.Timestamp(joined, tz="UTC")], cut + 1  # no history before joining
            if len(df):
                data[sym] = df
        if log and cut:
            log(f"{cut} acciones recortadas a su fecha de entrada en el S&P 500 (evita usar su pasado previo)")
        bench = None
        try:
            bench = self.research_frame(self.settings.benchmark)["close"]
        except (DataQualityError, KeyError):
            pass
        ai = None
        if cfg.use_ai and self.settings.gemini_api_key:
            from qsts.ai.providers import GeminiProvider
            from qsts.ai.service import AIResearchService
            ai = AIResearchService(GeminiProvider(self.settings.gemini_api_key.get_secret_value(),
                                                  model=self.settings.gemini_model),
                                   self.sf, max_calls_per_day=self.settings.ai_max_calls_per_day)
        return AutoResearcher(self.sf, data, cfg, ai=ai, log=log, stop_event=stop_event, benchmark=bench)

    def symbols(self) -> list[str]:
        with self.sf() as s:
            return list(s.scalars(select(m.Asset.symbol).join(m.Price, m.Price.asset_id == m.Asset.id)
                                  .distinct().order_by(m.Asset.symbol)))

    def strategy_counts(self) -> dict[str, int]:
        with self.sf() as s:
            rows = s.execute(select(m.Strategy.status, func.count()).group_by(m.Strategy.status)).all()
        return {k.lower(): v for k, v in rows}


def build_context(settings: Settings | None = None, initial_paper_cash: float = 10_000.0) -> AppContext:
    st = settings or load_settings()
    eng = make_engine(st.database_url)
    init_db(eng)
    sf = session_factory(eng)
    ks = KillSwitch(st.state_dir)
    modes = ModeController(SystemMode.OBSERVATION)  # always start in OBSERVATION
    limits: RiskLimits = MICRO_LIVE if st.env is Environment.LIVE else RiskLimits()
    risk = RiskEngine(limits, ks)
    log_ch = LogChannel()
    notifier = NotificationService([log_ch])
    # Only a PAPER broker exists. A live adapter (phase 17) must be added explicitly; never auto-selected.
    broker = PaperBroker(initial_paper_cash, CostModel())
    execu = ExecutionService(broker, risk, ks, modes, notifier)
    Path(st.state_dir).mkdir(parents=True, exist_ok=True)
    return AppContext(st, sf, MarketDataRepository(sf), ks, modes, risk, notifier, log_ch, execu,
                      ExperimentTracker(sf), StrategyRegistry(sf))
