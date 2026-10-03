import io
import json

import pytest

from qsts.ai.providers import AIProviderError, GeminiProvider, MockAIProvider
from qsts.ai.service import AIBudgetExceeded, AIOutputRejected, AIResearchService

GOOD = {"strategies": [
    {"name": "pullback_trend", "family": "pullback", "hypothesis": "Buy dips in uptrends",
     "direction": "long",
     "entry_long": [{"left": {"feature": "close"}, "op": ">", "right": {"feature": "sma", "params": {"n": 200}}},
                    {"left": {"feature": "rsi", "params": {"n": 14}}, "op": "<", "right": {"value": "$lo"}}],
     "exit_long": [{"left": {"feature": "rsi", "params": {"n": 14}}, "op": ">", "right": {"value": 60}}],
     "stop": {"kind": "atr", "atr_n": 14, "mult": 2.0}, "take_profit": {"kind": "r_multiple", "value": 2.0},
     "max_holding_bars": 10, "params": {"lo": 35}},
    {"name": "bogus", "family": "x", "hypothesis": "uses an invented indicator", "direction": "long",
     "entry_long": [{"left": {"feature": "magic_alpha"}, "op": ">", "right": {"value": 0}}], "params": {}},
    {"name": "inject", "family": "x", "hypothesis": "h", "direction": "long", "expected_sharpe": 3.1,
     "entry_long": [{"left": {"feature": "rsi"}, "op": "<", "right": {"value": 30}}]},
]}


def test_strategy_proposals_validated_and_cached(sf):
    mock = MockAIProvider(lambda s, p: json.dumps(GOOD))
    svc = AIResearchService(mock, sf)
    out = svc.propose_strategies({"note": "no prior strategies"}, n=3, dataset_version="ds1")
    assert [s.name for s in out["accepted"]] == ["pullback_trend"]
    assert len(out["rejected"]) == 2  # invented feature + injected performance claim field
    sd = out["accepted"][0]
    assert sd.metadata["origin"] == "ai" and sd.metadata["provider"] == "mock" and sd.metadata["dataset_version"] == "ds1"
    svc.propose_strategies({"note": "no prior strategies"}, n=3)
    assert mock.calls == 1  # cached


def test_budget(sf):
    svc = AIResearchService(MockAIProvider(lambda s, p: json.dumps({"strategies": []})), sf, max_calls_per_day=2)
    svc.propose_strategies({"a": 1})
    svc.propose_strategies({"a": 2})
    with pytest.raises(AIBudgetExceeded):
        svc.propose_strategies({"a": 3})


def test_news_schema_enforced(sf):
    ok = AIResearchService(MockAIProvider(lambda s, p: json.dumps(
        {"event_type": "earnings", "sentiment": 0.6, "importance": 0.8, "relevance": 1.0, "rationale": "beat"})), sf)
    r = ok.analyze_news({"symbol": "AAA", "headline": "AAA beats", "source": "x", "available_at": "2024-01-01T21:00"})
    assert r["event_type"] == "earnings" and r["available_at"] == "2024-01-01T21:00"
    bad = AIResearchService(MockAIProvider(lambda s, p: json.dumps({"event_type": "earnings", "sentiment": 7})), sf)
    with pytest.raises(AIOutputRejected):
        bad.analyze_news({"headline": "z"})
    junk = AIResearchService(MockAIProvider(lambda s, p: "not json"), sf)
    with pytest.raises(AIOutputRejected):
        junk.analyze_news({"headline": "y"})


def test_gemini_request_shape(monkeypatch):
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout):
        seen["url"], seen["headers"], seen["body"] = req.full_url, dict(req.headers), json.loads(req.data)
        return Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": "{\"a\":1}"}]}}],
                                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    g = GeminiProvider("KEY", model="gemini-test")
    r = g.generate("sys", "hello")
    assert seen["url"].endswith("/models/gemini-test:generateContent")
    assert seen["headers"]["X-goog-api-key"] == "KEY" and "KEY" not in seen["url"]
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert r.text == '{"a":1}' and r.tokens_in == 5
    with pytest.raises(AIProviderError):
        GeminiProvider("")
