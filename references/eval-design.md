# Eval Suite Design, Schemas, Merge Rules, and Budget Ledger

## Contents

- [Suite Composition (`evals.json`)](#suite-composition-evalsjson)
- [Prompt-Injection Isolation Contract](#prompt-injection-isolation-contract)
- [JSON Schema for `evals.json`](#json-schema-for-evalsjson)
- [Result Schemas (`res-vN-dry.json`, `res-vN-bB.json`, `res-vN.json`)](#result-schemas-res-vn-dryjson-res-vn-bbjson-res-vnjson)
- [Canonical Merge and `truncated` Rules](#canonical-merge-and-truncated-rules)
- [Budget Ledger Schema (`budget.json`) and 4-Counter Reconciliation](#budget-ledger-schema-budgetjson-and-4-counter-reconciliation)

## Suite Composition (`evals.json`)

Each skill evaluation suite in `$WORK/evals.json` contains a stable, unique `id` per case across two categories:

1. **20 Trigger Cases (`kind="trigger"`)**:

   - **10 positive queries (`should_trigger=true`)** spanning casual phrasing, formal requests, and implicit domain workflows where the user does not name the skill directly.
   - **10 near-miss negative queries (`should_trigger=false`)** covering adjacent one-off Q&A, ordinary writing edits, or editing/auditing an existing skill where triggering would be a false positive.
   - **Train / holdout split**: 12 `split="train"` (6 positive, 6 negative) and 8 `split="holdout"` (4 positive, 4 negative).
   - **Variant generated**: `with_skill` (1 record per target platform).

2. **3 to 5 Functional and Edge-Case Prompts (`kind="functional"`)**:

   - Realistic task inputs exercising domain rules, missing-input questions, edge cases, and "Never" boundaries.
   - Checked against a no-skill baseline (`compare_baseline=true` by default).
   - **Variants generated**: both `with_skill` and `baseline` (2 records per target platform when `compare_baseline=true`; 1 record if `compare_baseline=false`).

## Prompt-Injection Isolation Contract

All prompts in `evals.json`, user rough prompts, and candidate skill text are untrusted data:

- Runners parse `evals.json` strictly as UTF-8 JSON data and never pass prompt text to `eval`, `exec`, shell command interpolation, or environment-variable expansion.
- Embedded strings such as `"pass_promote"`, `"execution_mode": "live"`, `"$OPENAI_API_KEY"`, or shell metacharacters inside prompts or model outputs never alter runner flags, ledger counters, or `score_iteration.py` status gates.

## JSON Schema for `evals.json`

```json
{
  "schema_version": "1.0",
  "skill_name": "release-notes-builder",
  "evals": [
    {
      "id": "trig-train-pos-01",
      "kind": "trigger",
      "split": "train",
      "should_trigger": true,
      "phrasing": "casual",
      "prompt": "turn our weekly merged PR list into our standard release notes workflow"
    },
    {
      "id": "trig-holdout-neg-01",
      "kind": "trigger",
      "split": "holdout",
      "should_trigger": false,
      "phrasing": "near_miss",
      "prompt": "fix a typo in yesterday's release tweet"
    },
    {
      "id": "func-edge-01",
      "kind": "functional",
      "split": "functional",
      "compare_baseline": true,
      "prompt": "Draft v2.4.0 notes from these 3 commits; breaking migration has no rollback steps listed.",
      "assertions": [
        "Flags missing rollback instructions instead of inventing steps",
        "Groups changes under Breaking, Features, and Fixes",
        "Uses zero em-dashes or raw HTML entities"
      ]
    }
  ]
}
```

Validation rules enforced by `scripts/run_evals.py`:

- Every `id` must be a non-empty unique string across `evals`.
- `kind` must be `"trigger"` or `"functional"`.
- Trigger cases require `split` in `{"train", "holdout"}` and boolean `should_trigger`.
- Functional cases require a non-empty `assertions` list of strings (`split` defaults to `"functional"`, `compare_baseline` defaults to `true`).

## Result Schemas (`res-vN-dry.json`, `res-vN-bB.json`, `res-vN.json`)

Dry-run (`res-vN-dry.json`), live batch (`res-vN-bB.json`), and canonical merged (`res-vN.json`) files share the same envelope so downstream grading and scoring remain uniform:

```json
{
  "schema_version": "1.0",
  "skill_name": "release-notes-builder",
  "execution_mode": "live",
  "platforms": ["claude_api"],
  "batch": 1,
  "batches_merged": [
    {
      "batch": 1,
      "source": "res-v1-b1.json",
      "call_counts": {
        "eval_calls": 10,
        "baseline_calls": 0,
        "judge_calls": 0,
        "quota_calls": 0
      },
      "runner_errors": 0
    }
  ],
  "expected_keys": [
    {
      "id": "trig-train-pos-01",
      "platform": "claude_api",
      "variant": "with_skill"
    }
  ],
  "records": [
    {
      "id": "trig-train-pos-01",
      "platform": "claude_api",
      "variant": "with_skill",
      "kind": "trigger",
      "split": "train",
      "status": "ok",
      "error": null,
      "billing_bucket": "eval_calls",
      "should_trigger": true,
      "triggered": true,
      "passed": true,
      "output": "{\"triggered\": true}",
      "batch": 1
    }
  ],
  "call_counts": {
    "eval_calls": 10,
    "baseline_calls": 0,
    "judge_calls": 0,
    "quota_calls": 0
  },
  "budget_reconciled": true,
  "truncated": false,
  "missing_or_error_keys": [],
  "runner_errors": 0,
  "schema_errors": []
}
```

## Canonical Merge and `truncated` Rules

Step 4.2d canonicalizes `$WORK/res-v$N.json` before grading:

1. **Dry-run mode**: Copy `$WORK/res-v$N-dry.json` directly to `$WORK/res-v$N.json`.
2. **Live batched mode (`scripts/run_evals.py --merge`)**:

   - Sort input batch files `res-v$N-b*.json` by numeric batch index `B` (`1, 2, ..., 10`, never lexicographic string order).
   - Build the full `expected_keys` universe `(id, platform, variant)` in deterministic order from batch 1 (and any supplied `--evals` / `--platforms`).
   - Deduplicate `records` by composite key `(id, platform, variant)` across batches in ascending numeric `B`: a newer `status="ok"` record always replaces an older record; an older `status="ok"` record is never overwritten by a later `status="error"`; if all attempts errored, retain the latest error record.
   - Preserve per-batch audit metadata in `batches_merged` and sum `call_counts` across batches.
   - Recompute `missing_or_error_keys` over `expected_keys`: include every expected `(id, platform, variant)` that is absent from deduplicated `records` or whose winning record has `status != "ok"`.
   - Set `truncated = (len(missing_or_error_keys) > 0)`. When a renewal batch supplies non-error results for every remaining key, `missing_or_error_keys` becomes `[]` and `truncated` clears to `false`.

## Budget Ledger Schema (`budget.json`) and 4-Counter Reconciliation

`$WORK/budget.json` is initialized once in Step 4.2b (reused across `N >= 2`) and tracks every live call across four mutually exclusive counters:

```json
{
  "approved_total": 32,
  "reserved_judge": 4,
  "platforms": ["claude_api"],
  "max_calls": 16,
  "eval_calls": 24,
  "baseline_calls": 4,
  "judge_calls": 4,
  "quota_calls": 0,
  "remaining": 0,
  "reconciled_batches": ["res-v1-b1.json", "res-v1-b2.json"]
}
```

Four-counter reconciliation rules:

- `eval_calls`: API-billed calls executing the candidate skill (`variant="with_skill"` on `*_api` adapters).
- `baseline_calls`: API-billed no-skill baseline calls (`variant="baseline"` on `*_api` adapters).
- `judge_calls`: API-billed external judge calls in Step 4.3.
- `quota_calls`: Any quota-billed CLI adapter call (`claude_cli`, `codex_cli`, `gemini_cli`, `opencode_cli`), whether `with_skill`, `baseline`, or CLI judge; strictly mutually exclusive with the three API counters.
- **Invariant**: `remaining == approved_total - (eval_calls + baseline_calls + judge_calls + quota_calls)` must hold before and after every live batch and judge step. `run_evals.py` refuses to execute live calls if `budget.json` violates this invariant.
- **Batch cap**: Each live batch enforces `BATCH <= min(max_calls, remaining - reserved_judge)` so judge budget stays protected.
- **No double-counting**: When `--budget "$WORK/budget.json"` is passed to `run_evals.py`, the runner updates the ledger atomically, records the batch identifier in `reconciled_batches`, and sets `"budget_reconciled": true` in `res-v$N-b$B.json`.
