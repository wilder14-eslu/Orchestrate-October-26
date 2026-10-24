"""Model fallback for request intents the rules cannot resolve (rule 'no_deletion_signal' only).

The model can only turn an unresolved request into a deletion when its answer is grounded:
the quote appears verbatim in the request, the wording is not negated and confidence >= 0.8.
Every other outcome keeps the deterministic default (not a deletion request).
"""
from __future__ import annotations

from ..intent import Intent
from .vlm import LLMContext, sha256_bytes

MIN_CONFIDENCE = 0.8


def resolve(request_text: str, rule_intent: Intent, llm: LLMContext) -> Intent:
    if rule_intent.rule != "no_deletion_signal":
        return rule_intent  # rules decided; the model is never consulted
    answer = llm.extract("intent", sha256_bytes(request_text.encode("utf-8")), request_text, label="intent")
    if not answer:
        return rule_intent
    quote = answer["quote"].strip()
    grounded = bool(quote) and quote in request_text
    if answer["is_deletion_request"] and not answer["negated"] and answer["confidence"] >= MIN_CONFIDENCE and grounded:
        return Intent(True, "llm_fallback", quote)
    return Intent(False, "llm_fallback_rejected" if answer["is_deletion_request"] else "llm_fallback_not_deletion", quote)
