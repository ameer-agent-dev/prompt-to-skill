# Grading Rubric and Schema (`grade-vN.json`)

## Contents

- [Overview and Hard Gates](#overview-and-hard-gates)
- [Mode Separation Rules](#mode-separation-rules)
- [Scoring Dimensions (0 to 10)](#scoring-dimensions-0-to-10)
- [Holdout Isolation and Pass Rates](#holdout-isolation-and-pass-rates)
- [Judge Budget Accounting](#judge-budget-accounting)
- [JSON Schema for `grade-vN.json`](#json-schema-for-grade-vnjson)
- [Complete Example](#complete-example)

## Overview and Hard Gates

Every iteration `N` produces `$WORK/grade-v$N.json` in Step 4.3 by grading `$WORK/res-v$N.json` against three 0.0 to 10.0 dimensions:

- `accuracy`: Trigger precision/recall across train and holdout splits plus functional correctness over the no-skill baseline.
- `quality`: Output structure, domain rule adherence, progressive disclosure, deterministic script usage, and safety hygiene.
- `easy_for_llms`: Instruction clarity, low token overhead, unambiguous branching, verifiable completion checks, and clean formatting.

Promotion to a passing terminal status (`pass_promote`, `live_self_graded_only`, or `dry_run_wiring_passed`) requires all four conditions simultaneously:

1. Zero static validation errors in `$WORK/val-v$N.json`.
2. Zero runner and schema errors in `$WORK/res-v$N.json` and `$WORK/grade-v$N.json`.
3. Complete non-error coverage (`truncated=false`) across every expected `(id, platform, variant)` record in `$WORK/res-v$N.json`.
4. All three dimension scores (`accuracy`, `quality`, `easy_for_llms`) >= `9.5` out of `10.0`.

## Mode Separation Rules

`execution_mode` and `grading_mode` are tracked independently and cross-checked by `scripts/score_iteration.py`:

- `execution_mode` must match `$WORK/res-v$N.json` (`"dry_run"` or `"live"`). Any mismatch is recorded as a schema error and blocks all passing statuses.
- `grading_mode` must be `"external_judge"` or `"self_graded"`.
- When `execution_mode` is `"dry_run"`, `grading_mode` must be `"self_graded"`. Paid judges never run on dry-run results, and `"dry_run"` combined with `"external_judge"` is rejected as a schema error.
- Neither `"dry_run"` nor `"self_graded"` can ever reach `pass_promote`. Only untruncated `"live"` runs graded with `"external_judge"`, 0 errors, and all three scores >= `9.5` reach `pass_promote`.

## Scoring Dimensions (0 to 10)

### 1. Accuracy (`accuracy`)

Measures whether the skill triggers on the right requests and produces materially better task outcomes than the no-skill baseline without violating domain boundaries:

- **9.5 to 10.0**: 100% holdout trigger pass rate, >= 95% train trigger pass rate, 100% functional assertions passed, clear win or parity-with-guardrails against baseline, and zero boundary violations.
- **8.0 to 9.4**: Minor train trigger miss or partial edge-case assertion gap; zero "Never" boundary violations.
- **6.0 to 7.9**: Multiple false positives on near-miss negatives, false negatives on implicit phrasing, or weak lift over the no-skill baseline.
- **0.0 to 5.9**: Fails core functional assertions, regresses below baseline, leaks secrets, or violates explicit "Never" rules.

### 2. Quality (`quality`)

Measures engineering rigor, safety boundaries, and progressive disclosure of the skill package:

- **9.5 to 10.0**: Tight `SKILL.md` with Hard Rules first, deterministic checks delegated to stdlib scripts when numeric/rule-heavy, one-level references used cleanly, strict data-vs-instructions handling, secrets read from host env only, and overwrite protection intact.
- **8.0 to 9.4**: Sound structure and safety rules with minor verbosity or a concrete check left in prose instead of a script.
- **6.0 to 7.9**: Bloated `SKILL.md`, vague filler ("be thorough", "think step by step"), or missing edge-case coverage.
- **0.0 to 5.9**: Unsafe shell/file handling, silent overwrites, hardcoded credentials, or broken file references.

### 3. Easy for LLMs (`easy_for_llms`)

Measures how reliably an executing agent can parse, route, and follow the skill without wasted tokens or ambiguity:

- **9.5 to 10.0**: Scannable sections, explicit inputs/outputs, exact CLI contracts, deterministic failure routing, zero em/en-dashes or raw HTML entities, blank lines before every list, and compact token footprint.
- **8.0 to 9.4**: Clear workflow with minor ordering friction or slightly wordy explanations.
- **6.0 to 7.9**: Ambiguous branching, buried constraints, or conflicting flags between `SKILL.md` and `references/`.
- **0.0 to 5.9**: Contradictory steps, unresolved `<...>` placeholders presented as literal commands, or unreadable walls of text.

## Holdout Isolation and Pass Rates

- Keep holdout trigger cases (`split="holdout"`) strictly isolated during diagnosis and revision in Step 4.5.
- Edit `SKILL.md` frontmatter `description` only using lessons from `split="train"` failures; never copy holdout wording into `SKILL.md`.
- Record `train_pass_rate`, `holdout_pass_rate`, and `functional_pass_rate` as floats in `[0.0, 1.0]`. Step 5.1 (`Pick BEST`) uses `holdout_pass_rate` as tie-breaker #4 after 0 errors, live/judge mode rank, and minimum dimension score.

## Judge Budget Accounting

When `execution_mode="live"` and `reserved_judge > 0` was approved in `$WORK/budget.json`:

1. Cap total live judge calls to `min(reserved_judge, remaining)` before invoking any external judge.
2. Record API-billed judge calls in `judge_calls` and quota-billed CLI judge calls in `quota_calls` (mutually exclusive counters; one increment per call).
3. Recompute `remaining = approved_total - (eval_calls + baseline_calls + judge_calls + quota_calls)` in `$WORK/budget.json` immediately after Step 4.3 before Step 4.4 runs.
4. If `min(reserved_judge, remaining) <= 0` or live spend was not approved for judging, fall back cleanly to `grading_mode="self_graded"` with zero judge calls.

## JSON Schema for `grade-vN.json`

Required top-level fields in `$WORK/grade-v$N.json`:

- `schema_version` (string): `"1.0"`.
- `iteration` (integer): Iteration number `1..5`.
- `execution_mode` (string): `"dry_run"` or `"live"` (must equal `res-vN.json["execution_mode"]`).
- `grading_mode` (string): `"self_graded"` or `"external_judge"`.
- `scores` (object): Required numeric fields `accuracy`, `quality`, and `easy_for_llms`, each in `[0.0, 10.0]`.
- `holdout_pass_rate` (number): Float in `[0.0, 1.0]`.
- `train_pass_rate` (number): Float in `[0.0, 1.0]`.
- `functional_pass_rate` (number): Float in `[0.0, 1.0]`.
- `judge_calls_used` (integer, optional, default `0`): API judge calls made during this grading step.
- `quota_calls_used` (integer, optional, default `0`): Quota-billed CLI judge calls made during this grading step.
- `per_case` (array): Per-eval grading entries (`id`, `split`, `passed`, `notes`).
- `root_causes` (array of strings): Train/functional failure diagnoses used by Step 4.5 (must not leak holdout text).
- `summary` (string): Concise human-readable grading summary.

## Complete Example

```json
{
  "schema_version": "1.0",
  "iteration": 1,
  "execution_mode": "dry_run",
  "grading_mode": "self_graded",
  "scores": {
    "accuracy": 9.6,
    "quality": 9.7,
    "easy_for_llms": 9.8
  },
  "holdout_pass_rate": 1.0,
  "train_pass_rate": 1.0,
  "functional_pass_rate": 1.0,
  "judge_calls_used": 0,
  "quota_calls_used": 0,
  "per_case": [
    {
      "id": "trig-train-pos-01",
      "split": "train",
      "passed": true,
      "notes": "Dry-run wiring verified for positive train trigger."
    }
  ],
  "root_causes": [],
  "summary": "Dry-run wiring and static structure passed all rubric checks; live execution remains unverified."
}
```
