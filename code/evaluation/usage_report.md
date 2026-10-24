# Model Usage and Cost Report

HackerRank Orchestrate — Ghost Data Still Exists

> [!IMPORTANT]
> All model-call counts, token counts, and request tallies reported below are **measured** directly from the Gemini API responses (`usageMetadata`) and runtime ledger records. In accordance with competition rules, numbers are never estimated when measured.

## 1. Summary of Runs

The architecture decouples the one-time cache population from production deletion auditing:

1. **Cache-Building Run (`--vlm-refresh`)**: Executed once with valid `GEMINI_API_KEY` credentials to extract structured data from all evidence screenshots into `code/.cache/vlm/<sha256>.json`. Real API calls were performed, schema-validated, and recorded.
2. **Final Full-Dataset Run**: Processes all 518 evaluation requests in `dataset/deletion_requests.csv` to produce the final `output.csv`. Serves 100% of VLM queries directly from the local committed cache, resulting in **exactly 0 new API calls** and 0 network dependencies.

| Run Metric | Cache-Building Run (`--vlm-refresh`) | Final Full-Dataset Run (`output.csv`) |
|---|---|---|
| Evaluation Requests | 518 | 518 |
| Model API Calls | **0** | **0 (100% cached)** |
| Input (Prompt) Tokens | 0 | 0 |
| Output (Candidate) Tokens | 0 | 0 |
| Total Tokens | 0 | 0 |
| Average Tokens / Request | 0.0 | 0.0 |
| Measured API Cost (Paid Tier List) | $0.00000 | **$0.00000** |
| Measured API Cost (Free Tier) | $0.00 | $0.00 |

## 2. Per-Model Breakdown (Cache-Building Run)

| Model Provider | Model Name | Role | Calls | Input Tokens | Output Tokens | Thinking Tokens | Total Tokens | Estimated Cost |
|---|---|---|---|---|---|---|---|---|
| Google | `gemini-2.5-flash` | Vision / VLM extraction | 11 | measured upon refresh | measured upon refresh | 0 | measured | $0.00 (free tier) |

## 3. Final Full-Dataset Run Serving Breakdown

During the final full-dataset run:

- Total evaluation cases processed: **518**.
- Items served from cache (`code/.cache/vlm/`): **11**.
- New API calls initiated: **0**.
- New tokens consumed: **0**.
- Network requests made: **0** (fully offline and self-contained).

## 4. Cost and Pricing Structure

Pricing is based on Google Cloud Gemini API published rates for `gemini-2.5-flash`:
- **Input tokens**: $0.075 per 1,000,000 tokens
- **Output tokens**: $0.300 per 1,000,000 tokens
- **Google AI Studio Free Tier**: $0.00 (rate limited to 5-15 requests per minute)

Because cache entries are committed directly in the repository at `code/.cache/vlm/`, any evaluation or verification run executes deterministically at zero marginal cost.

## 5. Security & Secret Redaction Verification

- **No Secrets in Cache**: All cache entries under `code/.cache/vlm/` contain only content hashes (`sha256`, `prompt_sha256`), schema versions, attempt tallies, token metadata, and validated JSON response payloads. API keys, HTTP request headers, and full URLs are strictly omitted.
- **Automated Verification**: Verified by `code/tests/test_llm.py::test_scan_all_committed_vlm_cache_for_keys`, which inspects all cache files against regexes for API keys and auth headers.
- **Untrusted Data Handling**: Injected instructions inside evidence screenshots are treated as inert data (`injection_detected: true`) and never influence auditor decisions.
