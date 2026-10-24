"""Optional model layer (Phase 5): Gemini vision extraction for screenshots and an intent fallback.

Every model output is schema-validated, cached by content hash, merged under deterministic
rules, and can never override a rule-based decision. Without a cache entry or a key the
pipeline falls back to the deterministic path (OCR text, rule-based intent).
"""
from __future__ import annotations

from .client import GeminiClient, LLMUnavailable, model_name
from .merge import merge_screen
from .usage import UsageLedger
from .vlm import LLMContext, ModelLayer

__all__ = [
    "GeminiClient",
    "LLMUnavailable",
    "ModelLayer",
    "LLMContext",
    "UsageLedger",
    "merge_screen",
    "model_name",
]
