"""Application context: settings, database and market data, shared by the API, the launcher and the CLI."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from qsts.config import Settings, load_settings
from qsts.data.bars import Timeframe
from qsts.data.repository import MarketDataRepository
from qsts.db import models as m
from qsts.db.session import init_db, make_engine, session_factory


@dataclass
class AppContext:
    settings: Settings
    sf: object
    repo: MarketDataRepository
    extra: dict = field(default_factory=dict)

    def load_bars(self, symbol: str, timeframe: Timeframe = Timeframe.D1) -> pd.DataFrame:
        return self.repo.load_bars(symbol, timeframe)

    def research_frame(self, symbol: str, timeframe: Timeframe = Timeframe.D1) -> pd.DataFrame:
        """The ONE way backtests and the paper trader get bars: stored RAW data re-validated (ValidatedBars
        contract), then backward-adjusted for splits and dividends from the stored corporate actions (`raw_close`
        kept). Prepared frames are cached while the stored data is unchanged (`data_token`); callers get a copy."""
        cache = self.extra.setdefault("frame_cache", {})
        token = self.repo.data_token(symbol, timeframe)
        hit = cache.get((symbol, timeframe))
        if hit is not None and hit[0] == token:
            return hit[1].copy()
        df = self._research_frame(symbol, timeframe)
        cache[(symbol, timeframe)] = (token, df)
        return df.copy()

    def _research_frame(self, symbol: str, timeframe: Timeframe = Timeframe.D1) -> pd.DataFrame:
        from qsts.data.adjust import adjust
        from qsts.data.quality import QualityConfig, validate_and_clean
        raw = self.load_bars(symbol, timeframe)[["open", "high", "low", "close", "volume"]]
        if not len(raw):
            raise KeyError(symbol)
        # intraday history has gaps by nature (vendors keep only recent bars): kept as gaps, never filled
        cfg = QualityConfig(max_missing_fraction=1.0, max_missing_gap=10 ** 9) if timeframe.intraday else QualityConfig()
        vb = validate_and_clean(raw, symbol, timeframe, cfg)
        return adjust(vb.df, self.repo.load_corporate_actions(symbol), "total")

    def symbols(self) -> list[str]:
        with self.sf() as s:  # one index lookup per asset (a DISTINCT over millions of prices was slow)
            has_bars = select(m.Price.asset_id).where(m.Price.asset_id == m.Asset.id).exists()
            return list(s.scalars(select(m.Asset.symbol).where(has_bars).order_by(m.Asset.symbol)))


def build_context(settings: Settings | None = None) -> AppContext:
    st = settings or load_settings()
    from qsts.app.sync import apply_pending_import
    loaded = apply_pending_import(st.database_url, st.state_dir)  # data copied from another computer (sync)
    eng = make_engine(st.database_url)
    init_db(eng)
    sf = session_factory(eng)
    Path(st.state_dir).mkdir(parents=True, exist_ok=True)
    ctx = AppContext(st, sf, MarketDataRepository(sf))
    if loaded is not None:
        from qsts.app.sync import db_file, save_state, signature
        ctx.extra["sync_loaded"] = loaded
        save_state(st.state_dir, last_signature=signature(db_file(st.database_url)))  # after the schema update
    return ctx
