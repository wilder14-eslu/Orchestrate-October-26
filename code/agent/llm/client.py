"""Gemini generateContent client (stdlib urllib) with validation, retries and backoff.

Security: the API key is read from GEMINI_API_KEY at call time, sent only in the
x-goog-api-key header (never in the URL), never stored on the client object, and
redacted from every error message. Nothing about the request (headers, URL, key) is
returned to callers or cached; only the validated JSON and token usage are.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from ..config import gemini_model_name
from .schemas import validate

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_JSON_RETRIES = 2          # at most 2 retries after the first invalid answer
MAX_RATE_LIMIT_ATTEMPTS = 5   # HTTP 429 / 503
BACKOFF_BASE_S = 2.0

# transport(url, headers, body, timeout) -> (status, response_headers, response_body)
Transport = Callable[[str, Dict[str, str], bytes, float], Tuple[int, Dict[str, str], bytes]]


class LLMUnavailable(Exception):
    """No key, no network, or the model never produced a valid answer: callers fall back."""


def model_name() -> str:
    return gemini_model_name()


def _urllib_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers or {}), err.read() or b""


def _redact(text: str) -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    return text.replace(key, "[REDACTED]") if key else text


@dataclass
class CallResult:
    data: dict
    attempts: int
    usage: Dict[str, int]
    model: str


@dataclass
class GeminiClient:
    transport: Transport = _urllib_transport
    sleep: Callable[[float], None] = time.sleep
    timeout_s: float = 90.0
    min_interval_s: float = float(os.environ.get("GEMINI_MIN_INTERVAL_S", "12"))  # free tier: about 5 requests/minute
    _last_call: float = field(default=0.0, repr=False)
    _fatal_error: Optional[str] = field(default=None, repr=False)

    def available(self) -> bool:
        return bool(os.environ.get("GEMINI_API_KEY")) and self._fatal_error is None

    def fatal_error(self) -> Optional[str]:
        return self._fatal_error

    def _post(self, model: str, payload: dict) -> dict:
        if self._fatal_error:
            raise LLMUnavailable(f"client halted after fatal error: {self._fatal_error}")
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMUnavailable("GEMINI_API_KEY is not set")
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "x-goog-api-key": key}
        for attempt in range(1, MAX_RATE_LIMIT_ATTEMPTS + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if self._last_call and wait > 0:
                self.sleep(wait)
            self._last_call = time.monotonic()
            try:
                status, resp_headers, raw = self.transport(ENDPOINT.format(model=model), headers, body, self.timeout_s)
            except OSError as exc:  # DNS, TLS, proxy refusal, timeout
                raise LLMUnavailable(_redact(f"network error: {type(exc).__name__}: {exc}")) from None

            # Non-retryable fatal errors (401 invalid key, 404 model not found)
            if status in (401, 404):
                err_msg = _redact(f"HTTP {status}: {raw[:300].decode('utf-8', 'replace')}")
                self._fatal_error = err_msg
                raise LLMUnavailable(err_msg)

            # Retryable rate-limits / server errors
            if status in (429, 503) and attempt < MAX_RATE_LIMIT_ATTEMPTS:
                retry_after = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else BACKOFF_BASE_S * 2 ** (attempt - 1)
                self.sleep(delay)
                continue

            if status != 200:
                raise LLMUnavailable(_redact(f"HTTP {status}: {raw[:300].decode('utf-8', 'replace')}"))
            return json.loads(raw.decode("utf-8"))
        raise LLMUnavailable("rate limited after retries")

    def generate_json(self, prompt: str, schema: dict, image: Optional[bytes] = None, mime: str = "image/png",
                      model: Optional[str] = None) -> CallResult:
        """Ask for JSON, validate it against `schema`, retry at most twice with the validation errors."""
        if self._fatal_error:
            raise LLMUnavailable(f"client halted after fatal error: {self._fatal_error}")
        model = model or model_name()
        usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "total_tokens": 0}
        feedback = ""
        for attempt in range(1, MAX_JSON_RETRIES + 2):
            parts: List[dict] = [{"text": prompt + feedback}]
            if image is not None:
                parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode("ascii")}})
            payload = {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            }
            response = self._post(model, payload)
            meta = response.get("usageMetadata", {})
            usage["calls"] += 1
            usage["input_tokens"] += int(meta.get("promptTokenCount", 0))
            usage["output_tokens"] += int(meta.get("candidatesTokenCount", 0))
            usage["thinking_tokens"] += int(meta.get("thoughtsTokenCount", 0))
            usage["total_tokens"] += int(meta.get("totalTokenCount", 0))
            text = "".join(p.get("text", "") for c in response.get("candidates", [])[:1]
                           for p in c.get("content", {}).get("parts", []))
            try:
                data = json.loads(text)
                errors = validate(data, schema)
            except json.JSONDecodeError as exc:
                errors = [f"invalid JSON: {exc.msg}"]
            if not errors:
                return CallResult(data, attempt, usage, model)
            feedback = ("\n\nYour previous answer was rejected: " + "; ".join(errors[:5]) +
                        "\nReturn only a JSON object that satisfies the schema.")
        raise LLMUnavailable(f"no valid JSON after {MAX_JSON_RETRIES + 1} attempts")
