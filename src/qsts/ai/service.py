"""AI Research Engine.

Role separation (mandatory): the AI proposes hypotheses/strategies and interprets text. It never
computes or supplies numbers the system relies on (prices, indicators, metrics, backtests). Every
AI output is parsed against a strict schema; anything outside it is rejected, not repaired.

Cost control: responses are cached by (provider, model, prompt_version, input hash) in the
database; a daily call budget caps spend; inputs are compact structured JSON.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from qsts.ai.providers import AIProvider, AIProviderError
from qsts.core.hashing import hash_obj, stable_json
from qsts.db import models as m
from qsts.features.registry import REGISTRY
from qsts.strategy.definition import StrategyDefinition, definition_from_dict

PROMPT_VERSIONS = {"strategy_proposal": "sp-1", "news_analysis": "na-1", "result_review": "rr-1"}

SYSTEM_RESEARCHER = (
    "You are a quantitative research assistant. You only reason over the structured data you are given. "
    "Never invent prices, statistics, news, metrics or backtest results. If the data is insufficient, say so. "
    "Respond with JSON only, exactly in the requested schema."
)

NEWS_EVENT_TYPES = {"earnings", "guidance", "upgrade", "downgrade", "sec_filing", "insider", "m&a", "lawsuit",
                    "regulatory", "product", "macro", "geopolitical", "sector", "other"}
OPS = {"<", ">", "<=", ">=", "cross_above", "cross_below"}


class AIBudgetExceeded(RuntimeError):
    pass


class AIOutputRejected(ValueError):
    pass


class AIResearchService:
    def __init__(self, provider: AIProvider, sf: sessionmaker[Session], max_calls_per_day: int = 50):
        self.p = provider
        self.sf = sf
        self.max_calls_per_day = max_calls_per_day

    # ------------------------------------------------------------------ core call with cache
    def _call(self, task: str, payload: dict, instructions: str) -> tuple[dict, int]:
        pv = PROMPT_VERSIONS[task]
        key = hash_obj({"provider": self.p.name, "model": self.p.model, "pv": pv, "payload": payload}, 32)
        with self.sf() as s:
            hit = s.execute(select(m.AIResponse.content, m.AIResponse.id).join(m.AIRequest)
                            .where(m.AIRequest.input_hash == key).order_by(m.AIResponse.id.desc()).limit(1)).first()
            if hit:
                return hit[0], hit[1]
            since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
            n = s.scalar(select(func.count()).select_from(m.AIRequest).where(m.AIRequest.ts >= since,
                                                                            m.AIRequest.provider == self.p.name))
        if n >= self.max_calls_per_day:
            raise AIBudgetExceeded(f"{n} calls in last 24h >= {self.max_calls_per_day}")
        prompt = f"{instructions}\n\nDATA (JSON):\n{stable_json(payload)}"
        with self.sf() as s, s.begin():
            req = m.AIRequest(provider=self.p.name, model=self.p.model, prompt_version=pv, input_hash=key,
                              payload={"task": task, "payload": payload})
            s.add(req)
            s.flush()
            req_id = req.id
        res = self.p.generate(SYSTEM_RESEARCHER, prompt, json_mode=True)
        try:
            content = json.loads(res.text)
        except json.JSONDecodeError as e:
            content = {"_invalid_json": res.text[:2000]}
            self._store(req_id, content, res)
            raise AIOutputRejected("AI returned non-JSON output") from e
        resp_id = self._store(req_id, content, res)
        return content, resp_id

    def _store(self, req_id, content, res) -> int:
        with self.sf() as s, s.begin():
            r = m.AIResponse(request_id=req_id, content=content, tokens_in=res.tokens_in, tokens_out=res.tokens_out)
            s.add(r)
            s.flush()
            return r.id

    # ------------------------------------------------------------------ strategy proposals
    def propose_strategies(self, context: dict, n: int = 3, dataset_version: str | None = None) -> dict:
        """context: structured research facts (existing strategies, their scores, failure reasons, regime stats).
        Returns {"accepted": [StrategyDefinition], "rejected": [(raw, reason)]}. Accepted ones start at RESEARCH."""
        catalogue = {k: {"category": d.category, "params": d.defaults} for k, d in REGISTRY.items()
                     if not k.startswith("_")}
        instructions = (
            f"Propose up to {n} NEW swing-trading strategy hypotheses as JSON: "
            '{"strategies": [{"name": str, "family": str, "hypothesis": str, "direction": "long"|"short"|"both", '
            '"entry_long": [cond], "entry_short": [cond], "exit_long": [cond], "exit_short": [cond], '
            '"stop": {"kind": "atr"|"percent"|"structure", "atr_n": int, "mult": float}, '
            '"take_profit": {"kind": "none"|"r_multiple"|"atr", "value": float}, "max_holding_bars": int|null, '
            '"params": {name: number}}]} where cond = {"left": operand, "op": one of '
            f"{sorted(OPS)}, \"right\": operand}} and operand = {{\"feature\": name, \"params\": {{...}}}} or "
            '{"value": number or "$param"}. Use ONLY features from the catalogue (or open/high/low/close/volume). '
            "Prefer few rules and few parameters. Do not claim any performance."
        )
        content, resp_id = self._call("strategy_proposal", {"catalogue": catalogue, "context": context}, instructions)
        accepted, rejected = [], []
        for raw in (content.get("strategies") or [])[:n]:
            try:
                sd = self._parse_strategy(raw, resp_id, dataset_version)
                accepted.append(sd)
            except (AIOutputRejected, KeyError, TypeError, ValueError) as e:
                rejected.append((raw, str(e)))
        return {"accepted": accepted, "rejected": rejected, "response_id": resp_id}

    def _parse_strategy(self, raw: dict, resp_id: int, dataset_version: str | None) -> StrategyDefinition:
        allowed = {"name", "family", "hypothesis", "direction", "entry_long", "entry_short", "exit_long",
                   "exit_short", "stop", "take_profit", "max_holding_bars", "params"}
        extra = set(raw) - allowed
        if extra:
            raise AIOutputRejected(f"unexpected fields {extra}")
        for k in ("entry_long", "entry_short", "exit_long", "exit_short"):
            for c in raw.get(k, []) or []:
                if c.get("op") not in OPS:
                    raise AIOutputRejected(f"bad op {c.get('op')}")
                for side in ("left", "right"):
                    o = c[side]
                    if set(o) - {"feature", "params", "value"}:
                        raise AIOutputRejected("bad operand")
        d = {k: v for k, v in raw.items() if v is not None or k == "max_holding_bars"}
        for k in ("entry_long", "entry_short", "exit_long", "exit_short"):
            d[k] = d.get(k) or []
        d["params"] = {k: float(v) for k, v in (d.get("params") or {}).items()}
        d["metadata"] = {"origin": "ai", "provider": self.p.name, "model": self.p.model,
                         "prompt_version": PROMPT_VERSIONS["strategy_proposal"], "ai_response_id": resp_id,
                         "dataset_version": dataset_version,
                         "generated_at": datetime.now(timezone.utc).isoformat()}
        sd = definition_from_dict(d)
        sd.validate()  # unknown features / undefined params -> rejected
        if sd.complexity()["score"] > 15:
            raise AIOutputRejected("too complex")
        return sd

    # ------------------------------------------------------------------ news
    def analyze_news(self, item: dict) -> dict:
        """item: {symbol, headline, source, published_at, available_at}. Returns validated labels only."""
        instructions = ('Classify this news item. JSON: {"event_type": one of ' + str(sorted(NEWS_EVENT_TYPES)) +
                        ', "sentiment": number in [-1,1], "importance": number in [0,1], "relevance": number in [0,1], '
                        '"rationale": short string}. Use only the headline/source given.')
        content, resp_id = self._call("news_analysis", item, instructions)
        et = content.get("event_type")
        out = {"event_type": et if et in NEWS_EVENT_TYPES else "other", "response_id": resp_id}
        for k, lo, hi in (("sentiment", -1, 1), ("importance", 0, 1), ("relevance", 0, 1)):
            v = content.get(k)
            if not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise AIOutputRejected(f"{k} invalid: {v!r}")
            out[k] = float(v)
        out["rationale"] = str(content.get("rationale", ""))[:500]
        out["available_at"] = item.get("available_at")  # timing comes from data, never from the AI
        return out

    # ------------------------------------------------------------------ result review
    def review_results(self, report: dict) -> dict:
        instructions = ('Review this strategy validation report. JSON: {"assessment": str, "failure_modes": [str], '
                        '"suggested_variations": [str]}. Reason only from the numbers provided; do not add numbers.')
        content, resp_id = self._call("result_review", report, instructions)
        return {"assessment": str(content.get("assessment", "")), "failure_modes": list(content.get("failure_modes", [])),
                "suggested_variations": list(content.get("suggested_variations", [])), "response_id": resp_id}
