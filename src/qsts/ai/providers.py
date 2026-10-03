"""AIProvider abstraction. The rest of the system talks only to `AIProvider`.

GeminiProvider: implemented against the Gemini REST `models/{model}:generateContent` endpoint
(v1beta). Status UNVERIFIED: the official docs were unreachable from the build environment, so
re-check request/response fields against https://ai.google.dev/api before relying on it.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from qsts.core import net


class AIProviderError(RuntimeError):
    pass


@dataclass
class AIResult:
    text: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    raw: dict | None = None


class AIProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def generate(self, system: str, prompt: str, *, json_mode: bool = True, temperature: float = 0.2) -> AIResult: ...


class GeminiProvider(AIProvider):
    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", timeout: float = 60.0):
        if not api_key:
            raise AIProviderError("missing Gemini API key (QSTS_GEMINI_API_KEY)")
        self._key = api_key
        self.model = model
        self.timeout = timeout

    def generate(self, system, prompt, *, json_mode=True, temperature=0.2):
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature,
                                 **({"responseMimeType": "application/json"} if json_mode else {})},
        }
        req = urllib.request.Request(f"{self.BASE}/models/{self.model}:generateContent",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": self._key})
        try:
            with net.urlopen(req, self.timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise AIProviderError(f"Gemini HTTP {e.code}: {e.read()[:300]!r}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise AIProviderError(f"Gemini unreachable: {e}") from e
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError) as e:
            raise AIProviderError(f"unexpected Gemini response: {str(data)[:300]}") from e
        u = data.get("usageMetadata", {})
        return AIResult(text, u.get("promptTokenCount"), u.get("candidatesTokenCount"), data)


class MockAIProvider(AIProvider):
    """MOCKED provider for tests/offline use. Responses come from a user-supplied function;
    nothing it returns is ever presented as real AI analysis."""
    name = "mock"

    def __init__(self, responder: Callable[[str, str], str], model: str = "mock-1"):
        self.responder = responder
        self.model = model
        self.calls = 0

    def generate(self, system, prompt, *, json_mode=True, temperature=0.2):
        self.calls += 1
        return AIResult(self.responder(system, prompt), len(prompt) // 4, None)
