"""Unit and integration tests for code/agent/llm/.

Tests:
1. Single source of truth: verifies gemini_model_name() default and GEMINI_MODEL override.
2. GeminiClient with fake transport: successful generation, JSON validation, retry on invalid JSON,
   rate-limit (429) backoff, strict secret redaction, and non-retryable fail-fast for HTTP 401/404.
3. Committed cache inspection: verifies that code/.cache/vlm/ never contains API keys, request headers,
   or full request URLs, only hashes, validated response, attempts, and token usage.
4. ModelLayer: cache serving with 0 new API calls, fallback on miss, stale model detection,
   and refresh summary generation.
5. Pricing integrity: verifies unverified models report free tier with $0.00 billed and no invented rates.
6. VLM screen merge: handles injection_detected safely, preserving evidence data without changing decisions.
7. Usage reporting: verifies cache-building run vs final run reporting in usage_report.md.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from agent.config import DEFAULT_GEMINI_MODEL, gemini_model_name
from agent.llm.client import GeminiClient, LLMUnavailable, model_name
from agent.llm.merge import merge_screen
from agent.llm.schemas import SCREEN_V1, INTENT_V1
from agent.llm.usage import UsageLedger, _calc_cost_str, render_usage_report
from agent.llm.vlm import ModelLayer, prompt_template, sha256_bytes
from agent.models import ManifestEntry, ParseWarning
from agent.text_cache import TextCache

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VLM_CACHE_DIR = REPO_ROOT / "code" / ".cache" / "vlm"


# -----------------------------------------------------------------------------
# Fixtures and Fake Transports
# -----------------------------------------------------------------------------

def make_fake_gemini_response(payload: dict, prompt_tokens: int = 150, candidate_tokens: int = 45) -> bytes:
    """Simulate a raw Gemini API response body."""
    resp = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json.dumps(payload)}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": candidate_tokens,
            "thoughtsTokenCount": 0,
            "totalTokenCount": prompt_tokens + candidate_tokens,
        },
    }
    return json.dumps(resp).encode("utf-8")


VALID_SCREEN_PAYLOAD = {
    "screen_type": "customer_profile",
    "subject": {"emails": ["user1@example.com"], "user_ids": ["user_1"], "single_subject": True},
    "records": [
        {
            "resource_id": "account_101",
            "reference": None,
            "emails": ["user1@example.com"],
            "phones": ["+15550100"],
            "user_ids": ["user_1"],
            "status": "present",
            "status_text": "Active",
        }
    ],
    "injection_detected": False,
    "notes": "Verified profile screen",
}


# -----------------------------------------------------------------------------
# 1. Single Source of Truth & Model Resolution
# -----------------------------------------------------------------------------

def test_single_source_of_truth_model_config(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert DEFAULT_GEMINI_MODEL == "gemini-3.8-flash"
    assert gemini_model_name() == "gemini-3.8-flash"
    assert model_name() == "gemini-3.8-flash"

    monkeypatch.setenv("GEMINI_MODEL", "custom-model-test")
    assert gemini_model_name() == "custom-model-test"
    assert model_name() == "custom-model-test"


# -----------------------------------------------------------------------------
# 2. Gemini Client Tests with Fake Transport
# -----------------------------------------------------------------------------

def test_client_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    client = GeminiClient(min_interval_s=0.0)
    assert not client.available()
    with pytest.raises(LLMUnavailable, match="GEMINI_API_KEY is not set"):
        client.generate_json("prompt", SCREEN_V1)


def test_client_generate_json_success(monkeypatch):
    fake_key = "AIzaSyFakeKeyForTestOnly123456789012"
    monkeypatch.setenv("GEMINI_API_KEY", fake_key)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    sent_headers = {}
    sent_url = ""

    def fake_transport(url, headers, body, timeout):
        nonlocal sent_headers, sent_url
        sent_url = url
        sent_headers = headers
        return 200, {}, make_fake_gemini_response(VALID_SCREEN_PAYLOAD, 200, 50)

    client = GeminiClient(transport=fake_transport, sleep=lambda s: None, min_interval_s=0.0)
    res = client.generate_json("Extract data", SCREEN_V1)

    assert res.attempts == 1
    assert res.data == VALID_SCREEN_PAYLOAD
    assert res.usage["input_tokens"] == 200
    assert res.usage["output_tokens"] == 50
    assert res.usage["total_tokens"] == 250
    assert res.model == "gemini-3.8-flash"

    # Security: Key must be in header, never in URL
    assert sent_headers.get("x-goog-api-key") == fake_key
    assert fake_key not in sent_url


def test_client_retries_on_invalid_json_then_succeeds(monkeypatch):
    fake_key = "AIzaSyFakeKeyForTestOnly123456789012"
    monkeypatch.setenv("GEMINI_API_KEY", fake_key)

    call_count = 0
    prompts_received = []

    def fake_transport(url, headers, body, timeout):
        nonlocal call_count
        call_count += 1
        req_data = json.loads(body.decode("utf-8"))
        prompt_text = req_data["contents"][0]["parts"][0]["text"]
        prompts_received.append(prompt_text)

        if call_count == 1:
            # First attempt: invalid schema (missing required fields)
            invalid_payload = {"screen_type": "unknown"}
            return 200, {}, make_fake_gemini_response(invalid_payload, 100, 20)
        # Second attempt: valid schema
        return 200, {}, make_fake_gemini_response(VALID_SCREEN_PAYLOAD, 120, 50)

    client = GeminiClient(transport=fake_transport, sleep=lambda s: None, min_interval_s=0.0)
    res = client.generate_json("Extract data", SCREEN_V1)

    assert res.attempts == 2
    assert call_count == 2
    assert res.data == VALID_SCREEN_PAYLOAD
    assert res.usage["calls"] == 2
    assert res.usage["input_tokens"] == 220
    assert res.usage["output_tokens"] == 70
    assert "Your previous answer was rejected" in prompts_received[1]


def test_client_max_retries_exceeded_raises(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    call_count = 0

    def fake_transport(url, headers, body, timeout):
        nonlocal call_count
        call_count += 1
        resp = {
            "candidates": [{"content": {"parts": [{"text": "Not a JSON"}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
        }
        return 200, {}, json.dumps(resp).encode("utf-8")

    client = GeminiClient(transport=fake_transport, sleep=lambda s: None, min_interval_s=0.0)
    with pytest.raises(LLMUnavailable, match="no valid JSON after 3 attempts"):
        client.generate_json("Extract data", SCREEN_V1)
    assert call_count == 3  # initial + 2 retries


def test_client_rate_limiting_backoff(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    calls = 0
    slept = []

    def fake_transport(url, headers, body, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 429, {"Retry-After": "1"}, b"Rate limit exceeded"
        return 200, {}, make_fake_gemini_response(VALID_SCREEN_PAYLOAD, 100, 30)

    client = GeminiClient(transport=fake_transport, sleep=lambda s: slept.append(s), min_interval_s=0.0)
    res = client.generate_json("Extract data", SCREEN_V1)
    assert res.data == VALID_SCREEN_PAYLOAD
    assert calls == 2
    assert 1.0 in slept


def test_client_error_redacts_key(monkeypatch):
    fake_key = "AIzaSySuperSecretKey123"
    monkeypatch.setenv("GEMINI_API_KEY", fake_key)

    def fake_transport(url, headers, body, timeout):
        return 400, {}, f"Invalid request with key {fake_key}".encode("utf-8")

    client = GeminiClient(transport=fake_transport, sleep=lambda s: None, min_interval_s=0.0)
    with pytest.raises(LLMUnavailable) as exc_info:
        client.generate_json("Extract", SCREEN_V1)
    err_msg = str(exc_info.value)
    assert fake_key not in err_msg
    assert "[REDACTED]" in err_msg


def test_client_non_retryable_404_fail_fast(monkeypatch):
    """Requirement 5: HTTP 404 is non-retryable; fails fast once, halts subsequent calls immediately."""
    monkeypatch.setenv("GEMINI_API_KEY", "valid-key")

    transport_calls = 0

    def fake_transport(url, headers, body, timeout):
        nonlocal transport_calls
        transport_calls += 1
        return 404, {}, b'{"error": {"message": "models/gemini-2.5-flash is no longer available"}}'

    client = GeminiClient(transport=fake_transport, sleep=lambda s: None, min_interval_s=0.0)

    # First call encounters 404 and fails fast (exactly 1 attempt)
    with pytest.raises(LLMUnavailable, match="HTTP 404"):
        client.generate_json("Extract 1", SCREEN_V1)
    assert transport_calls == 1
    assert not client.available()
    assert client.fatal_error() is not None

    # Subsequent call halts immediately without hitting transport
    with pytest.raises(LLMUnavailable, match="client halted after fatal error"):
        client.generate_json("Extract 2", SCREEN_V1)
    assert transport_calls == 1  # Still 1, 0 new network calls


def test_client_non_retryable_401_fail_fast(monkeypatch):
    """Requirement 5: HTTP 401 is non-retryable; fails fast once and halts."""
    monkeypatch.setenv("GEMINI_API_KEY", "bad-key")

    transport_calls = 0

    def fake_transport(url, headers, body, timeout):
        nonlocal transport_calls
        transport_calls += 1
        return 401, {}, b'{"error": {"message": "API key not valid"}}'

    client = GeminiClient(transport=fake_transport, sleep=lambda s: None, min_interval_s=0.0)

    with pytest.raises(LLMUnavailable, match="HTTP 401"):
        client.generate_json("Extract", SCREEN_V1)
    assert transport_calls == 1
    assert not client.available()


# -----------------------------------------------------------------------------
# 3. Cache Inspection Tests (Addition 1: No Keys, Headers, or URLs)
# -----------------------------------------------------------------------------

def test_cache_entry_contents_contain_only_allowed_fields(tmp_path, monkeypatch):
    fake_key = "AIzaSyFakeKey9876543210"
    monkeypatch.setenv("GEMINI_API_KEY", fake_key)

    cache = TextCache(root=tmp_path)
    ledger = UsageLedger()

    def fake_transport(url, headers, body, timeout):
        return 200, {}, make_fake_gemini_response(VALID_SCREEN_PAYLOAD, 110, 40)

    client = GeminiClient(transport=fake_transport, sleep=lambda s: None, min_interval_s=0.0)
    layer = ModelLayer(cache=cache, ledger=ledger, refresh=True, client=client)

    img_path = tmp_path / "test.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\nfakeimagebytes")
    digest = sha256_bytes(img_path.read_bytes())

    warnings = []
    res = layer.screen(img_path, digest, warnings, file_id="ev_01", file_path="ev.png")
    assert res == VALID_SCREEN_PAYLOAD

    cache_file = tmp_path / "vlm" / f"{digest}.json"
    assert cache_file.exists()

    raw_text = cache_file.read_text(encoding="utf-8")
    data = json.loads(raw_text)

    # Allowed keys only
    allowed_keys = {"sha256", "kind", "schema_version", "model", "prompt_sha256", "attempts", "usage", "response"}
    assert set(data.keys()) == allowed_keys

    # Must never contain key, url, or headers
    assert fake_key not in raw_text
    assert "x-goog-api-key" not in raw_text.lower()
    assert "generativelanguage.googleapis.com" not in raw_text
    assert "https://" not in raw_text


def test_scan_all_committed_vlm_cache_for_keys(monkeypatch):
    """Addition 1 requirement: Scans code/.cache/vlm/ for anything looking like a key or secret."""
    key_pattern = re.compile(r"AIza[0-9A-Za-z\-_]{30,}")
    header_pattern = re.compile(r"(?:x-goog-api-key|authorization|bearer\s+[a-z0-9_-]+)", re.I)
    url_pattern = re.compile(r"https?://generativelanguage\.googleapis\.com", re.I)

    # Scanner function
    def scan_file(f_path: Path):
        text = f_path.read_text(encoding="utf-8")
        assert not key_pattern.search(text), f"Found API key pattern in {f_path.name}"
        assert not header_pattern.search(text), f"Found authorization header pattern in {f_path.name}"
        assert not url_pattern.search(text), f"Found API URL in {f_path.name}"
        if current_key:
            assert current_key not in text, f"Active GEMINI_API_KEY found in {f_path.name}"

        # Check structured fields
        entry = json.loads(text)
        assert "response" in entry
        assert "usage" in entry
        assert "headers" not in entry
        assert "url" not in entry
        assert "key" not in entry

    current_key = os.environ.get("GEMINI_API_KEY", "")

    # Ensure VLM cache directory exists
    VLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_files = list(VLM_CACHE_DIR.glob("*.json"))

    for f in cache_files:
        scan_file(f)

    # Also verify detector actively catches an illicit key
    test_leak_file = VLM_CACHE_DIR / "_test_temp_check.json"
    try:
        test_leak_file.write_text(json.dumps({"key": "AIzaSyTestKeyThatMustBeCaught12345678"}), encoding="utf-8")
        with pytest.raises(AssertionError, match="Found API key pattern"):
            scan_file(test_leak_file)
    finally:
        if test_leak_file.exists():
            test_leak_file.unlink()


# -----------------------------------------------------------------------------
# 4. ModelLayer Cache Serving, Stale Detection & Fallback Tests
# -----------------------------------------------------------------------------

def test_model_layer_serves_from_cache_with_zero_calls(tmp_path):
    cache = TextCache(root=tmp_path)
    ledger = UsageLedger()

    digest = "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
    cached_entry = {
        "kind": "screen",
        "schema_version": "screen_v1",
        "model": "gemini-3.8-flash",
        "prompt_sha256": sha256_bytes(prompt_template("screen").encode("utf-8")),
        "attempts": 1,
        "usage": {"calls": 1, "input_tokens": 100, "output_tokens": 40, "total_tokens": 140},
        "response": VALID_SCREEN_PAYLOAD,
    }
    cache.put("vlm", digest, cached_entry)

    # Layer with refresh=False and NO client (offline)
    layer = ModelLayer(cache=cache, ledger=ledger, refresh=False, client=None)
    img_path = tmp_path / "screen.png"
    img_path.write_bytes(b"data")

    res = layer.screen(img_path, digest, [], file_id="ev_01", file_path="ev.png")
    assert res == VALID_SCREEN_PAYLOAD

    # Verify zero API calls made
    report = ledger.to_dict()
    assert report["totals"]["calls"] == 0
    assert report["totals"]["input_tokens"] == 0
    assert report["totals"]["output_tokens"] == 0
    assert report["items_by_source"]["cache"] == 1


def test_stale_cache_entry_model_mismatch_warning(tmp_path):
    """Requirement 3: Entry built with a different model is stale and not reused silently."""
    cache = TextCache(root=tmp_path)
    ledger = UsageLedger()

    digest = "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"
    cached_entry = {
        "kind": "screen",
        "schema_version": "screen_v1",
        "model": "gemini-old-model",
        "prompt_sha256": "old_hash",
        "attempts": 1,
        "usage": {"calls": 1, "input_tokens": 100, "output_tokens": 40, "total_tokens": 140},
        "response": VALID_SCREEN_PAYLOAD,
    }
    cache.put("vlm", digest, cached_entry)

    layer = ModelLayer(cache=cache, ledger=ledger, refresh=False, client=None)
    img_path = tmp_path / "screen.png"
    img_path.write_bytes(b"data")

    warnings = []
    res = layer.screen(img_path, digest, warnings, file_id="ev_stale", file_path="ev.png")
    # Returns payload for evaluation stability, but records an explicit stale model warning
    assert res == VALID_SCREEN_PAYLOAD
    assert any("stale (built with 'gemini-old-model'" in w.message for w in warnings)


def test_model_layer_fallback_on_cache_miss_offline(tmp_path):
    cache = TextCache(root=tmp_path)
    ledger = UsageLedger()
    layer = ModelLayer(cache=cache, ledger=ledger, refresh=False, client=None)

    img_path = tmp_path / "screen.png"
    img_path.write_bytes(b"data")
    digest = "unknown_digest_12345"

    res = layer.screen(img_path, digest, [], file_id="ev_01", file_path="ev.png")
    assert res is None  # Falls back to OCR path
    assert ledger.to_dict()["items_by_source"]["fallback"] == 1


def test_vlm_refresh_summary_tracking(capsys, tmp_path):
    """Requirement 4: Summary prints total images, calls, cached hits, and fallbacks grouped by reason."""
    cache = TextCache(root=tmp_path)
    ledger = UsageLedger()
    layer = ModelLayer(cache=cache, ledger=ledger, refresh=True, client=None)

    # Simulate 2 fallbacks and 1 cached hit
    layer.stats["items"] = 3
    layer.stats["cached"] = 1
    layer.stats["fallbacks"]["HTTP 404: model not found"] = 2

    layer.print_summary()
    captured = capsys.readouterr()
    assert "=== VLM Refresh Summary ===" in captured.err
    assert "Total items inspected:   3" in captured.err
    assert "Fresh cache hits:        1" in captured.err
    assert "[2x] HTTP 404: model not found" in captured.err


# -----------------------------------------------------------------------------
# 5. Pricing Integrity (Requirement 2)
# -----------------------------------------------------------------------------

def test_unverified_model_pricing_reports_free_tier():
    """Requirement 2: Do not invent rates for unverified models; report free tier with $0.00 billed."""
    cost, label = _calc_cost_str("gemini-3.8-flash", 10000, 2000)
    assert cost == 0.0
    assert label == "free tier, $0.00 billed; list price not verified"

    # Known verified models compute standard numbers
    v_cost, v_label = _calc_cost_str("gemini-1.5-flash", 1_000_000, 1_000_000)
    assert v_cost == pytest.approx(0.375)
    assert "$0.37500" in v_label


# -----------------------------------------------------------------------------
# 6. Injection Detection & Merge Rules (Addition 3)
# -----------------------------------------------------------------------------

def test_screen_merge_with_injection_detected():
    injected_vlm_response = {
        "screen_type": "search_index",
        "subject": {"emails": ["target@example.com"], "user_ids": ["user_99"], "single_subject": True},
        "records": [
            {
                "resource_id": "search_9901",
                "reference": None,
                "emails": ["target@example.com"],
                "phones": [],
                "user_ids": ["user_99"],
                "status": "present",
                "status_text": "Indexed",
            }
        ],
        "injection_detected": True,
        "notes": "Instruction detected in header",
    }

    entry = ManifestEntry("ev_99", "evidence/images/search.png", "search_service", "2026-10-10T10:00:00Z")
    ocr_text = "Search results for target@example.com\nsearch_9901 target@example.com Indexed"
    warnings = []
    known = {"emails": {"target@example.com"}, "phones": set(), "user_ids": {"user_99"}}

    obs, mentions = merge_screen(entry, ocr_text, injected_vlm_response, {}, set(), known, warnings)

    # 1. Warning must trace injection_detected
    assert any("injection_detected" in w.message for w in warnings)

    # 2. Records must still be safely extracted
    assert len(obs) == 1
    o = obs[0]
    assert o.resource_id == "search_9901"
    assert o.presence == "PRESENT"
    assert o.extraction_method in ("vlm+ocr", "vlm")


def test_merge_screen_exact_image_text_signature():
    """Requirement 1: Verify merge_screen called exactly as image_text.py calls it with 7 explicit typed arguments."""
    entry = ManifestEntry("ev_101", "evidence/images/console.png", "account_service", "2026-09-01T12:00:00Z")
    ocr_text = "account_101 user@example.com active"
    vlm_resp = {
        "screen_type": "user_profile",
        "subject": {"emails": ["user@example.com"], "user_ids": [], "single_subject": True},
        "records": [
            {
                "resource_id": "account_101",
                "reference": None,
                "emails": ["user@example.com"],
                "phones": [],
                "user_ids": [],
                "status": "present",
                "status_text": "active",
            }
        ],
        "injection_detected": False,
    }
    aliases = {"REF-101": "account_101"}
    known_resources = {"account_101"}
    known_ids = {"emails": {"user@example.com"}, "phones": set(), "user_ids": set()}
    warnings = []

    # Call exactly as image_text.py does with all 7 explicit arguments
    obs, mentions = merge_screen(entry, ocr_text, vlm_resp, aliases, known_resources, known_ids, warnings)
    assert len(obs) == 1
    assert obs[0].resource_id == "account_101"
    assert obs[0].extraction_method == "vlm+ocr"



# -----------------------------------------------------------------------------
# 7. Usage Report Generation (Addition 2)
# -----------------------------------------------------------------------------

def test_usage_report_separate_refresh_and_final_run(tmp_path):
    refresh_data = {
        "run": "cache_build",
        "target_count": 518,
        "screenshot_count": 11,
        "per_model": {
            "gemini-3.8-flash": {
                "calls": 11,
                "input_tokens": 12500,
                "output_tokens": 1850,
                "thinking_tokens": 0,
                "total_tokens": 14350,
            }
        },
        "totals": {
            "calls": 11,
            "input_tokens": 12500,
            "output_tokens": 1850,
            "thinking_tokens": 0,
            "total_tokens": 14350,
        },
        "items_by_source": {"api": 11},
    }

    final_data = {
        "run": "full_run",
        "target_count": 518,
        "screenshot_count": 11,
        "per_model": {},
        "totals": {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "total_tokens": 0,
        },
        "items_by_source": {"cache": 11},
    }

    out_file = tmp_path / "usage_report.md"
    md = render_usage_report(refresh_data=refresh_data, final_data=final_data, out_path=out_file)

    assert out_file.exists()
    assert "Cache-Building Run (`--vlm-refresh`)" in md
    assert "Final Full-Dataset Run (`output.csv`)" in md
    assert "gemini-3.8-flash" in md
    assert "11" in md
    assert "14,350" in md
    assert "**0 (100% cached)**" in md
    assert "free tier, $0.00 billed; list price not verified" in md
    assert "Never estimate numbers that were measured" in md or "numbers are never estimated when measured" in md


def test_parser_crash_reported_and_causes_failure_if_all_crash(monkeypatch, capsys, tmp_path):
    """Requirement 2: Parser crash is printed to stderr, tracked in stats, and 100% crash fails run."""
    import agent.ingest.image_text as it
    from agent.pipeline import run

    def broken_parse(*args, **kwargs):
        raise RuntimeError("simulated parser boom")

    monkeypatch.setattr(it, "parse", broken_parse)

    out_csv = tmp_path / "out.csv"
    ret = run(Path("dataset"), Path("dataset/deletion_requests.csv"), out_csv)
    # Must fail with non-zero exit code because 100% of .png files crashed
    assert ret == 2
    captured = capsys.readouterr()
    assert "[ingest] parser crashed on" in captured.err
    assert "simulated parser boom" in captured.err
    assert "[fatal] 100% of .png files crashed during parsing" in captured.err


def test_ocr_email_tokenizer_captures_dotted_local_parts_whole():
    """Requirement 1: Email regex and OCR tokenizer capture dotted local parts whole without emitting suffix fragments."""
    import agent.config as C
    from agent.ingest.pdf_text import clean_ocr, ocr_vote

    # 1. Verify EMAIL_RE alone captures dotted local parts whole
    sample_text = "Contacts: sj.park@naver.com, miller.logistics@company.com, and g.silva76.jr@gmail.com."
    extracted = C.EMAIL_RE.findall(sample_text)
    assert extracted == ["sj.park@naver.com", "miller.logistics@company.com", "g.silva76.jr@gmail.com"]
    assert "park@naver.com" not in extracted
    assert "logistics@company.com" not in extracted
    assert "jr@gmail.com" not in extracted

    # 2. Verify noisy OCR artifacts (comma/space in dotted parts or pass fragmentation) are cleaned and voted whole
    # Case 1: sj.park@naver.com with comma and suffix fragment
    raw_1 = "User sj.park@naver.com along with typo -si,park@naver.com and fragment park@naver.com"
    voted_1, _ = ocr_vote(clean_ocr(raw_1))
    emails_1 = C.EMAIL_RE.findall(voted_1)
    assert "sj.park@naver.com" in emails_1
    assert "park@naver.com" not in emails_1

    # Case 2: miller.logistics@company.com with space and suffix fragment
    raw_2 = "Backup miller logistics@company.com and miller.logistics@company.com plus logistics@company.com"
    voted_2, _ = ocr_vote(clean_ocr(raw_2))
    emails_2 = C.EMAIL_RE.findall(voted_2)
    assert "miller.logistics@company.com" in emails_2
    assert "logistics@company.com" not in emails_2

    # Case 3: g.silva76.jr@gmail.com with comma/space and suffix fragment
    raw_3 = "Search g.silva76,jr@gmail.com and g.silva76 jr@gmail.com with jr@gmail.com"
    voted_3, _ = ocr_vote(clean_ocr(raw_3))
    emails_3 = C.EMAIL_RE.findall(voted_3)
    assert "g.silva76.jr@gmail.com" in emails_3
    assert "jr@gmail.com" not in emails_3


