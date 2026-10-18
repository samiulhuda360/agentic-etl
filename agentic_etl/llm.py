"""A small client for any OpenAI-compatible chat endpoint, with a disk cache, call spacing and a call budget.

Configuration (environment variables; nothing is stored in files):
    AI_API_KEY        the key; without it only recorded replies (cache/llm) can be used
    AI_BASE_URL       default: Gemini's OpenAI-compatible endpoint
    AI_MODEL          default: gemini-flash-lite-latest
    AI_MIN_INTERVAL   minimum seconds between live calls (default 4.5; free quotas are small)
    AI_MAX_CALLS      stop after this many live calls in one process (default 150)

Every reply is cached by a hash of (model, messages, json mode), so evaluation and CI replay offline. A rate-limit
reply (HTTP 429) stops the run with `RateLimited` instead of retrying into a shared quota.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import cache_dir

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemini-flash-lite-latest"
# List price used to report cost per file (USD per million tokens, Gemini Flash-Lite paid tier).
PRICE_IN, PRICE_OUT = 0.10, 0.40


class NoModel(RuntimeError):
    """No key is configured and the cache has no reply for this prompt."""


class RateLimited(RuntimeError):
    """The endpoint answered 429; the caller stops and reports how far it got."""


class OverBudget(RuntimeError):
    """AI_MAX_CALLS live calls have been made in this process."""


@dataclass
class Reply:
    text: str
    cached: bool
    latency_s: float
    prompt_tokens: int
    completion_tokens: int

    @property
    def cost_usd(self) -> float:
        return (self.prompt_tokens * PRICE_IN + self.completion_tokens * PRICE_OUT) / 1e6


@dataclass
class LLM:
    model: str = field(default_factory=lambda: os.getenv("AI_MODEL", DEFAULT_MODEL))
    base_url: str = field(default_factory=lambda: os.getenv("AI_BASE_URL", DEFAULT_BASE_URL))
    cache: Path = field(default_factory=lambda: cache_dir() / "llm")
    offline: bool = False  # replay from the cache only
    min_interval: float = field(default_factory=lambda: float(os.getenv("AI_MIN_INTERVAL", "4.5")))
    max_calls: int = field(default_factory=lambda: int(os.getenv("AI_MAX_CALLS", "150")))
    live_calls: int = 0
    cached_calls: int = 0
    log: list[Reply] = field(default_factory=list)
    _last: float = 0.0

    @property
    def live(self) -> bool:
        return not self.offline and bool(os.getenv("AI_API_KEY"))

    def key(self, messages: list[dict[str, str]], json_mode: bool) -> str:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if json_mode:
            payload["json"] = True
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def has(self, messages: list[dict[str, str]], json_mode: bool = False) -> bool:
        return (self.cache / f"{self.key(messages, json_mode)}.json").exists()

    def chat(self, messages: list[dict[str, str]], json_mode: bool = False) -> Reply:
        path = self.cache / f"{self.key(messages, json_mode)}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.cached_calls += 1
            reply = Reply(data["text"], True, data["latency_s"], data["prompt_tokens"], data["completion_tokens"])
            self.log.append(reply)
            return reply
        if not self.live:
            raise NoModel("no AI_API_KEY and no recorded reply for this prompt")
        if self.live_calls >= self.max_calls:
            raise OverBudget(f"AI_MAX_CALLS={self.max_calls} live calls reached")
        reply, served = self._call(messages, json_mode)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "model": self.model,
            "served_by": served,
            "text": reply.text,
            "latency_s": reply.latency_s,
            "prompt_tokens": reply.prompt_tokens,
            "completion_tokens": reply.completion_tokens,
        }
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        self.log.append(reply)
        return reply

    def _call(self, messages: list[dict[str, str]], json_mode: bool) -> tuple[Reply, str]:
        gap = self.min_interval - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.0}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ['AI_API_KEY']}"},
        )
        data: dict[str, Any] = {}
        started = time.perf_counter()
        for attempt in range(4):
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    body = e.read().decode("utf-8", "replace")
                    # A per-minute limit clears by itself: back off for the delay the server asks for, at most
                    # three times. Any other quota (for example the daily one) stops the run.
                    if "PerMinute" in body and attempt < 3:
                        m = re.search(r'"retryDelay":\s*"(\d+)', body)
                        time.sleep(int(m.group(1)) + 5 if m else 30)
                        continue
                    raise RateLimited("the model endpoint returned 429 (quota or rate limit)") from e
                if e.code >= 500 and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt >= 2:
                    raise
                time.sleep(5 * (attempt + 1))
        latency = round(time.perf_counter() - started, 3)
        self.live_calls += 1
        usage = data.get("usage") or {}
        text = data["choices"][0]["message"].get("content") or ""
        reply = Reply(
            text.strip(), False, latency, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        )
        return reply, str(data.get("model", self.model))
