"""Cached, schema-validated model extraction (screenshots and request intents).

Only --vlm-refresh runs call the API; every other run is cache-only, so the final
full run makes zero calls and a machine without a key reproduces the same output.
Cache entries (code/.cache/vlm/<sha256>.json) hold only hashes, the model name,
the validated response, attempt count and token usage: never the key, request
headers or the request URL.
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import gemini_model_name
from ..models import ParseWarning
from ..text_cache import TextCache
from .client import GeminiClient, LLMUnavailable
from .schema import validate
from .schemas import INTENT_V1, SCREEN_V1
from .usage import UsageLedger

PROMPTS = Path(__file__).resolve().parent / "prompts"
SCHEMAS = {
    "screen": ("screen_v1", SCREEN_V1, "screen_extraction.md"),
    "intent": ("intent_v1", INTENT_V1, "intent_fallback.md"),
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prompt_template(kind: str) -> str:
    version, schema, filename = SCHEMAS[kind]
    return (PROMPTS / filename).read_text(encoding="utf-8").replace("{schema}", json.dumps(schema, indent=1))


@dataclass
class ModelLayer:
    cache: TextCache
    ledger: UsageLedger
    refresh: bool = False
    client: Optional[GeminiClient] = None
    warnings: List[ParseWarning] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=lambda: {
        "items": 0, "calls": 0, "cached": 0, "fallbacks": defaultdict(int)
    })

    def __post_init__(self):
        if self.client is None and self.refresh:
            self.client = GeminiClient()

    def screen(self, image_path: Path, digest: str, warnings: List[ParseWarning],
               file_id: str = "", file_path: str = "") -> Optional[dict]:
        """Extract structured record observations from an evidence screenshot."""
        self.stats["items"] += 1
        image_bytes = image_path.read_bytes() if image_path.exists() else None
        res = self.extract("screen", digest, "", image=image_bytes, label=file_id or image_path.name)
        if self.warnings:
            warnings.extend(self.warnings)
            self.warnings.clear()
        return res

    def intent(self, case_id: str, request_text: str) -> Optional[dict]:
        """Extract classification for an unresolved customer request intent."""
        self.stats["items"] += 1
        digest = sha256_bytes(request_text.encode("utf-8"))
        return self.extract("intent", digest, request_text, label=case_id)

    def extract(self, kind: str, digest: str, payload_text: str, image: Optional[bytes] = None,
                label: str = "") -> Optional[dict]:
        """Return a validated response from cache, or (refresh runs only) from the API; None means fall back."""
        version, schema, _ = SCHEMAS[kind]
        template = prompt_template(kind)
        prompt = template.replace("{request_text}", payload_text) if kind == "intent" else template
        prompt_hash = sha256_bytes(template.encode("utf-8"))
        model = gemini_model_name()
        ns = "vlm" if kind == "screen" else kind

        cached = self.cache.get(ns, digest)
        cached_model = (cached or {}).get("model")
        fresh = (bool(cached)
                 and cached_model == model
                 and cached.get("prompt_sha256") == prompt_hash
                 and cached.get("schema_version") == version)

        if cached and validate(cached.get("response"), schema):
            self.warnings.append(ParseWarning(label, label, "-", f"{kind} cache entry fails schema; ignored"))
            cached, fresh = None, False

        # When not refreshing, serve cache if available (with stale warning if model/prompt changed)
        if cached and (fresh or not self.refresh):
            if not fresh:
                self.warnings.append(ParseWarning(
                    label, label, "-",
                    f"{kind} cache entry is stale (built with '{cached_model}', configured model is '{model}'); "
                    "used as is, run --vlm-refresh to rebuild"
                ))
            self.stats["cached"] += 1
            self.ledger.record(kind, digest, "cache", model=cached.get("model", ""), attempts=cached.get("attempts", 0))
            return cached["response"]

        # If refresh is not enabled or client is not available (e.g. fatal 401/404 error)
        if not (self.refresh and self.client and self.client.available()):
            fatal = self.client.fatal_error() if self.client else None
            reason = f"client fatal error: {fatal}" if fatal else "no cache entry; deterministic path used"
            self.stats["fallbacks"][reason] += 1
            self.ledger.record(kind, digest, "fallback", note=reason)
            return None

        try:
            result = self.client.generate_json(prompt, schema, image=image, model=model)
            self.stats["calls"] += 1
        except LLMUnavailable as exc:
            reason = str(exc)[:160]
            self.stats["fallbacks"][reason] += 1
            self.warnings.append(ParseWarning(label, label, "-", f"{kind} model unavailable, deterministic fallback: {exc}"))
            self.ledger.record(kind, digest, "fallback", model=model, note=reason)
            return None

        self.cache.put(ns, digest, {
            "kind": kind,
            "schema_version": version,
            "model": result.model,
            "prompt_sha256": prompt_hash,
            "attempts": result.attempts,
            "usage": result.usage,
            "response": result.data,
        })
        self.ledger.record(kind, digest, "api", model=result.model, usage=result.usage, attempts=result.attempts)
        return result.data

    def print_summary(self) -> None:
        """Print an explicit summary of the VLM refresh pass to stderr."""
        items = self.stats["items"]
        cached = self.stats["cached"]
        calls = self.stats["calls"]
        fallbacks = sum(self.stats["fallbacks"].values())
        print("\n=== VLM Refresh Summary ===", file=sys.stderr)
        print(f"Total items inspected:   {items}", file=sys.stderr)
        print(f"Fresh cache hits:        {cached}", file=sys.stderr)
        print(f"API calls successful:    {calls}", file=sys.stderr)
        print(f"Deterministic fallbacks: {fallbacks}", file=sys.stderr)
        if self.stats["fallbacks"]:
            print("Fallbacks grouped by reason:", file=sys.stderr)
            for reason, count in sorted(self.stats["fallbacks"].items()):
                print(f"  * [{count}x] {reason}", file=sys.stderr)
        if self.client and self.client.fatal_error():
            print(f"Client halted with fatal non-retryable error: {self.client.fatal_error()}", file=sys.stderr)
        print("===========================\n", file=sys.stderr)


# Backward-compatible alias
LLMContext = ModelLayer
