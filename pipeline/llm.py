"""Minimal LLM client for Groq's OpenAI-compatible API, built for a FREE tier.

Design goals (free tier = 8,000 tokens/minute per model, ~1,000 requests/day):
  * Disk cache: identical request => cached answer, zero tokens. Re-running or
    resuming the pipeline never pays twice for the same prompt.
  * Client-side rate limiter: a 60-second sliding window of tokens (prompt
    estimate + max_tokens) so we wait BEFORE Groq rejects us with 429.
  * Hard budget: LLM_MAX_TOKENS_PER_RUN across all calls (tracked in pipeline state).
    When exceeded we raise BudgetExhausted; the orchestrator saves state and exits
    cleanly so `python -m pipeline all` resumes later.
  * Daily-limit 429s also raise BudgetExhausted (no point hammering).
  * Every call (including cache hits) is logged to logs/llm_calls.jsonl, and the
    full prompt/response transcript to logs/llm_transcripts/<hash>.json.
  * The API key is read from the environment and never logged or printed.
"""
import hashlib
import json
import time
from collections import deque

import requests

from pipeline.config import settings

API = "https://api.groq.com/openai/v1/chat/completions"
TRANSCRIPTS = settings.logs / "llm_transcripts"
CALL_LOG = settings.logs / "llm_calls.jsonl"


class BudgetExhausted(RuntimeError):
    pass


class RequestTooLarge(ValueError):
    pass


def est_tokens(text: str) -> int:
    return int(len(text) / 3.4) + 16  # measured ~3.65 chars/token on our prompts; keep a margin


class LLM:
    def __init__(self, state):
        self.state = state
        self.windows = {}  # model -> deque[(timestamp, tokens)]; Groq limits are per model
        TRANSCRIPTS.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ rate limiting
    def _wait_for_capacity(self, need: int, model: str):
        window = self.windows.setdefault(model, deque())
        limit = int(settings.llm_tpm_limit * 0.92)
        if need > limit:
            raise RequestTooLarge(f"request needs ~{need} tokens but the per-minute limit is {settings.llm_tpm_limit}")
        while True:
            now = time.time()
            while window and now - window[0][0] > 61:
                window.popleft()
            used = sum(t for _, t in window)
            if used + need <= limit:
                return
            sleep = 61 - (now - window[0][0]) + 0.5
            print(f"    [rate limiter] {used}+{need} tokens > {limit}/min, sleeping {sleep:.0f}s")
            time.sleep(max(1, sleep))

    def _budget_check(self, need: int):
        used = self.state.data["llm"]["tokens"]
        if used + need > settings.llm_max_tokens_per_run:
            raise BudgetExhausted(f"LLM_MAX_TOKENS_PER_RUN={settings.llm_max_tokens_per_run} would be exceeded "
                                  f"(used {used}, next call needs ~{need}). Raise it in .env to continue.")

    # ------------------------------------------------------------------ main entry point
    def chat(self, messages, *, purpose, chunk=None, model=None, max_tokens=3000, reasoning="low", json_mode=False):
        model = model or settings.llm_model
        payload = {"model": model, "messages": messages, "max_completion_tokens": max_tokens, "temperature": 0.2}
        if model.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = reasoning
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
        tpath = TRANSCRIPTS / f"{key}.json"
        prompt_text = "".join(m["content"] for m in messages)
        summary = messages[-1]["content"][:160].replace("\n", " ")

        if tpath.exists():
            t = json.loads(tpath.read_text())
            self._log(purpose, chunk, model, key, summary, t["usage"], t["finish_reason"], cache_hit=True, seconds=0)
            return t["content"]

        need = est_tokens(prompt_text) + max_tokens
        self._budget_check(need)
        headers = {"Authorization": f"Bearer {settings.groq_api_key}", "Content-Type": "application/json"}
        for attempt in range(6):
            self._wait_for_capacity(need, model)
            window = self.windows[model]
            t0 = time.time()
            window.append((t0, need))
            try:
                r = requests.post(API, json=payload, headers=headers, timeout=180)
            except requests.RequestException as e:
                print(f"    [llm] network error {type(e).__name__}, retrying")
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429:
                msg = r.text[:400]
                if "per day" in msg or "TPD" in msg or "RPD" in msg:
                    raise BudgetExhausted(f"Groq daily limit reached for {model}: {msg}")
                wait = float(r.headers.get("retry-after", 20))
                print(f"    [llm] 429 rate limited, waiting {wait:.0f}s")
                time.sleep(wait + 1)
                continue
            if r.status_code == 413 or (r.status_code == 400 and "too large" in r.text.lower()):
                raise RequestTooLarge(r.text[:300])
            if r.status_code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"Groq API error {r.status_code}: {r.text[:500]}")
            d = r.json()
            choice = d["choices"][0]
            content = choice["message"].get("content") or ""
            usage = d.get("usage", {})
            # correct the window entry with the real number
            window[-1] = (t0, usage.get("total_tokens", need))
            self.state.data["llm"]["tokens"] += usage.get("total_tokens", 0)
            self.state.data["llm"]["calls"] += 1
            self.state.save()
            tpath.write_text(json.dumps({"model": model, "purpose": purpose, "chunk": chunk, "messages": messages,
                                         "content": content, "usage": usage, "finish_reason": choice.get("finish_reason")},
                                        indent=1))
            self._log(purpose, chunk, model, key, summary, usage, choice.get("finish_reason"), cache_hit=False,
                      seconds=round(time.time() - t0, 2))
            return content
        raise BudgetExhausted(f"Groq kept failing/rate-limiting after retries ({purpose})")

    def _log(self, purpose, chunk, model, key, summary, usage, finish, cache_hit, seconds):
        with open(CALL_LOG, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "purpose": purpose, "chunk": chunk,
                                "model": model, "transcript": key, "prompt_summary": summary,
                                "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                                "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                                "finish_reason": finish, "cache_hit": cache_hit, "seconds": seconds}) + "\n")


def extract_json(text: str):
    """Parse the first JSON object in a model reply (tolerates ```json fences)."""
    t = text.strip()
    if "```" in t:
        parts = t.split("```")
        for p in parts[1::2]:
            p = p[4:] if p.startswith("json") else p
            try:
                return json.loads(p)
            except ValueError:
                pass
    start = t.find("{")
    end = t.rfind("}")
    return json.loads(t[start:end + 1])
