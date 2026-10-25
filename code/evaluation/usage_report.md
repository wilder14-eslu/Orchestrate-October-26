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
| Model API Calls | **13** | **0 (100% cached)** |
| Input (Prompt) Tokens | 32,383 | 0 |
| Output (Candidate) Tokens | 4,559 | 0 |
| Total Tokens | 36,942 | 0 |
| Average Tokens / Request | 71.3 | 0.0 |
| Measured API Cost (Paid Tier List) | free tier, $0.00 billed; list price not verified | **$0.00000** |
| Measured API Cost (Free Tier) | $0.00 | $0.00 |

## 2. Per-Model Breakdown (Cache-Building Run)

| Model Provider | Model Name | Role | Calls | Input Tokens | Output Tokens | Thinking Tokens | Total Tokens | Estimated Cost |
|---|---|---|---|---|---|---|---|---|
| Google | `gemini-3.8-flash` | Vision / VLM extraction | 13 | 32,383 | 4,559 | 0 | 36,942 | free tier, $0.00 billed; list price not verified |

## 3. Final Full-Dataset Run Serving Breakdown

During the final full-dataset run:

- Total evaluation cases processed: **518**.
- Items served from cache (`code/.cache/vlm/`): **11**.
- New API calls initiated: **0**.
- New tokens consumed: **0**.
- Network requests made: **0** (fully offline and self-contained).

## 4. Cost and Pricing Structure

Pricing structure for measured model `gemini-3.8-flash`:
- **Google AI Studio Free Tier**: $0.00 (rate limited to 5-15 requests per minute, utilized for all challenge runs).
- **Standard List Price**: Not officially verified for this model; recorded as free tier with $0.00 billed.

Because cache entries are committed directly in the repository at `code/.cache/vlm/`, any evaluation or verification run executes deterministically at zero marginal cost.

## 5. Security & Secret Redaction Verification

- **No Secrets in Cache**: All cache entries under `code/.cache/vlm/` contain only content hashes (`sha256`, `prompt_sha256`), schema versions, attempt tallies, token metadata, and validated JSON response payloads. API keys, HTTP request headers, and full URLs are strictly omitted.
- **Automated Verification**: Verified by `code/tests/test_llm.py::test_scan_all_committed_vlm_cache_for_keys`, which inspects all cache files against regexes for API keys and auth headers.
- **Untrusted Data Handling**: Injected instructions inside evidence screenshots are treated as inert data (`injection_detected: true`) and never influence auditor decisions.

## 6. VLM Layer Contribution and Disagreement Analysis

Over the 13 cached items (11 evidence screenshots in `dataset/evidence/images/` and 2 evaluation fixtures in `code/evaluation/fixtures/`):

- **Corroborated Observations (`vlm+ocr`)**: **26** observations were corroborated between OCR and VLM.
- **Fields Filled by VLM (where OCR was empty)**: **0**. The deterministic OCR pipeline already extracted 100% of the direct identifiers, resources, flow states, and restore states across all screenshots.
- **Disagreements Logged**:
  1. `evidence_60` (`marketing_profile_console.png`, `case_63`, `marketing_001`): OCR extracted `['park@naver.com', 'sj.park@naver.com']` (split at dot boundary); VLM extracted `['sj.park@naver.com']`. Logged as an email disagreement warning while preserving OCR authoritative values.
  2. `evidence_32` (`backup_restore_console_20260429.png`, `case_55`, `backup_9288`): OCR extracted `['logistics@company.com', 'miller.logistics@company.com']`; VLM extracted `['miller.logistics@company.com']`. Logged as an email disagreement warning while preserving OCR authoritative values.
  3. `evidence_09` (`search_index_inspector_20260901.png`, `search_1874`): OCR truncated `['jr@gmail.com']`; VLM extracted `['g.silva76.jr@gmail.com']`. Because `g.silva76.jr@gmail.com` belongs to an unlinked relative not in known identity profiles, the hallucination guard filtered it.

**Decision Impact**:
`output.csv` is **identical to the OCR baseline** (0 changed rows). The VLM layer acts strictly as an independent corroborator and disagreement detector, not as a source of decisions.

