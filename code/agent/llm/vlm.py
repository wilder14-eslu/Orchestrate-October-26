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
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..models import ParseWarning
from ..text_cache import TextCache
from .client import GeminiClient, LLMUnavailable, model_name
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

    def __post_init__(self):
        if self.client is None and self.refresh:
            self.client = GeminiClient()

    def screen(self, image_path: Path, digest: str, warnings: List[ParseWarning],
               file_id: str = "", file_path: str = "") -> Optional[dict]:
        """Extract structured record observations from an evidence screenshot."""
        image_bytes = image_path.read_bytes() if image_path.exists() else None
        res = self.extract("screen", digest, "", image=image_bytes, label=file_id or image_path.name)
        if self.warnings:
            warnings.extend(self.warnings)
            self.warnings.clear()
        return res

    def intent(self, case_id: str, request_text: str) -> Optional[dict]:
        """Extract classification for an unresolved customer request intent."""
        digest = sha256_bytes(request_text.encode("utf-8"))
        return self.extract("intent", digest, request_text, label=case_id)

    def extract(self, kind: str, digest: str, payload_text: str, image: Optional[bytes] = None,
                label: str = "") -> Optional[dict]:
        """Return a validated response from cache, or (refresh runs only) from the API; None means fall back."""
        version, schema, _ = SCHEMAS[kind]
        template = prompt_template(kind)
        prompt = template.replace("{request_text}", payload_text) if kind == "intent" else template
        prompt_hash = sha256_bytes(template.encode("utf-8"))
        model = model_name()
        ns = "vlm" if kind == "screen" else kind

        cached = self.cache.get(ns, digest)
        fresh = (bool(cached)
                 and cached.get("model") == model
                 and cached.get("prompt_sha256") == prompt_hash
                 and cached.get("schema_version") == version)

        if cached and validate(cached.get("response"), schema):
            self.warnings.append(ParseWarning(label, label, "-", f"{kind} cache entry fails schema; ignored"))
            cached, fresh = None, False

        if cached and (fresh or not self.refresh):
            if not fresh:
                self.warnings.append(ParseWarning(label, label, "-", f"{kind} cache entry is stale (model/prompt changed); "
                                                                     "used as is, run --vlm-refresh to rebuild"))
            self.ledger.record(kind, digest, "cache", model=cached.get("model", ""), attempts=cached.get("attempts", 0))
            return cached["response"]

        if not (self.refresh and self.client and self.client.available()):
            self.ledger.record(kind, digest, "fallback", note="no cache entry; deterministic path used")
            return None

        try:
            result = self.client.generate_json(prompt, schema, image=image, model=model)
        except LLMUnavailable as exc:
            self.warnings.append(ParseWarning(label, label, "-", f"{kind} model unavailable, deterministic fallback: {exc}"))
            self.ledger.record(kind, digest, "fallback", model=model, note=str(exc)[:200])
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


# Backward-compatible alias
LLMContext = ModelLayer
