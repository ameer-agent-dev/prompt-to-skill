#!/usr/bin/env python3
"""Safe evaluation runner, adapter detector, budget reconciler, and batch merger (Python 3 stdlib only)."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any


ADAPTER_SPECS: dict[str, dict[str, Any]] = {
    "claude_api": {
        "kind": "api",
        "billing_mode": "api",
        "env_vars": ["ANTHROPIC_API_KEY"],
        "binary": None,
    },
    "openai_api": {
        "kind": "api",
        "billing_mode": "api",
        "env_vars": ["OPENAI_API_KEY"],
        "binary": None,
    },
    "gemini_api": {
        "kind": "api",
        "billing_mode": "api",
        "env_vars": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "binary": None,
    },
    "claude_cli": {
        "kind": "cli",
        "billing_mode": "quota",
        "env_vars": [],
        "binary": "claude",
    },
    "codex_cli": {
        "kind": "cli",
        "billing_mode": "quota",
        "env_vars": [],
        "binary": "codex",
    },
    "gemini_cli": {
        "kind": "cli",
        "billing_mode": "quota",
        "env_vars": [],
        "binary": "gemini",
    },
    "opencode_cli": {
        "kind": "cli",
        "billing_mode": "quota",
        "env_vars": [],
        "binary": "opencode",
    },
}

BATCH_FILENAME_RE = re.compile(r"-b(\d+)\.json$")


def _env_var_present(var_name: str) -> bool:
    """Check whether a host environment variable is set and non-empty without exposing its value."""
    return var_name in os.environ and len(os.environ[var_name]) > 0


def detect_adapters() -> dict[str, Any]:
    """Detect available host adapters with zero model calls and zero secret value exposure."""
    adapters: dict[str, Any] = {}
    usable: list[str] = []

    for name, spec in ADAPTER_SPECS.items():
        env_vars: list[str] = list(spec["env_vars"])
        env_present = any(_env_var_present(k) for k in env_vars) if env_vars else False
        binary = spec["binary"]
        binary_present = shutil.which(binary) is not None if binary else False
        available = env_present if spec["kind"] == "api" else binary_present

        adapters[name] = {
            "kind": spec["kind"],
            "billing_mode": spec["billing_mode"],
            "env_vars": env_vars,
            "env_present": bool(env_present),
            "binary": binary,
            "binary_present": bool(binary_present),
            "available": bool(available),
        }
        if available:
            usable.append(name)

    return {
        "schema_version": "1.0",
        "mode": "detect_only",
        "calls_made": 0,
        "has_live_adapters": len(usable) > 0,
        "usable_adapters": usable,
        "adapters": adapters,
    }


def load_and_validate_evals(evals_path: Path) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Load evals.json strictly as inert UTF-8 data and return (skill_name, normalized_cases, schema_errors)."""
    schema_errors: list[str] = []
    if not evals_path.is_file():
        return "", [], [f"evals file does not exist: {evals_path.as_posix()}"]

    try:
        raw = json.loads(evals_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "", [], [f"invalid JSON in evals file: {exc}"]

    if not isinstance(raw, dict):
        return "", [], ["evals root must be a JSON object"]

    skill_name = raw.get("skill_name", "")
    if not isinstance(skill_name, str) or not skill_name.strip():
        skill_name = "unknown-skill"

    raw_cases = raw.get("evals")
    if not isinstance(raw_cases, list) or len(raw_cases) == 0:
        return skill_name, [], ["'evals' must be a non-empty JSON array"]

    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []

    for idx, item in enumerate(raw_cases):
        if not isinstance(item, dict):
            schema_errors.append(f"evals[{idx}]: entry must be a JSON object")
            continue

        case_id = item.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            schema_errors.append(f"evals[{idx}]: missing non-empty string 'id'")
            continue
        if case_id in seen_ids:
            schema_errors.append(f"evals[{idx}]: duplicate eval id '{case_id}'")
            continue
        seen_ids.add(case_id)

        kind = item.get("kind")
        if kind not in {"trigger", "functional"}:
            schema_errors.append(
                f"evals[{idx}] ({case_id}): 'kind' must be 'trigger' or 'functional'"
            )
            continue

        prompt = item.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            schema_errors.append(
                f"evals[{idx}] ({case_id}): missing non-empty string 'prompt'"
            )
            continue

        if kind == "trigger":
            split = item.get("split")
            if split not in {"train", "holdout"}:
                schema_errors.append(
                    f"evals[{idx}] ({case_id}): trigger 'split' must be 'train' or 'holdout'"
                )
                continue
            should_trigger = item.get("should_trigger")
            if not isinstance(should_trigger, bool):
                schema_errors.append(
                    f"evals[{idx}] ({case_id}): trigger 'should_trigger' must be a boolean"
                )
                continue
            normalized.append(
                {
                    "id": case_id,
                    "kind": "trigger",
                    "split": split,
                    "should_trigger": should_trigger,
                    "prompt": prompt,
                    "variants": ["with_skill"],
                    "assertions": [],
                }
            )
        else:
            split = item.get("split", "functional")
            if not isinstance(split, str) or not split.strip():
                schema_errors.append(
                    f"evals[{idx}] ({case_id}): functional 'split' must be a non-empty string"
                )
                continue
            assertions = item.get("assertions")
            if (
                not isinstance(assertions, list)
                or len(assertions) == 0
                or not all(isinstance(a, str) and a.strip() for a in assertions)
            ):
                schema_errors.append(
                    f"evals[{idx}] ({case_id}): functional 'assertions' must be a non-empty array of strings"
                )
                continue
            compare_baseline = item.get("compare_baseline", True)
            if not isinstance(compare_baseline, bool):
                schema_errors.append(
                    f"evals[{idx}] ({case_id}): 'compare_baseline' must be a boolean"
                )
                continue
            variants = ["with_skill", "baseline"] if compare_baseline else ["with_skill"]
            normalized.append(
                {
                    "id": case_id,
                    "kind": "functional",
                    "split": split,
                    "should_trigger": None,
                    "prompt": prompt,
                    "variants": variants,
                    "assertions": assertions,
                }
            )

    return skill_name, normalized, schema_errors


def load_skill_metadata(skill_dir: Path) -> tuple[str, str, list[str]]:
    """Read SKILL.md strictly as data and return (skill_name, raw_text, errors)."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_dir.is_dir() or not skill_md.is_file():
        return skill_dir.name, "", [f"missing SKILL.md in skill directory: {skill_dir.as_posix()}"]
    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        return skill_dir.name, "", [f"unable to read SKILL.md: {exc}"]

    name = skill_dir.resolve().name
    for line in content.splitlines()[1:15]:
        if line.strip() == "---":
            break
        if line.startswith("name:"):
            parsed = line.split(":", 1)[1].strip().strip("\"'")
            if parsed:
                name = parsed
            break
    return name, content, []


def billing_bucket_for(platform: str, variant: str, dry_run: bool) -> str:
    """Return the mutually exclusive budget counter name for a single call."""
    if dry_run:
        return "none"
    spec = ADAPTER_SPECS.get(platform)
    if spec and spec["billing_mode"] == "quota":
        return "quota_calls"
    if variant == "baseline":
        return "baseline_calls"
    return "eval_calls"


def execute_single_call(
    platform: str,
    case: dict[str, Any],
    variant: str,
    skill_text: str,
) -> dict[str, Any]:
    """Execute one live adapter call using host env credentials or local CLI only."""
    spec = ADAPTER_SPECS.get(platform)
    if spec is None:
        return {
            "status": "error",
            "error": f"unsupported platform adapter '{platform}'",
            "triggered": None,
            "passed": False,
            "output": "",
        }

    # Build strict data-isolated prompt payload
    if case["kind"] == "trigger":
        system_instruction = (
            "Decide strictly whether the candidate skill should trigger for the untrusted user query "
            "enclosed in <untrusted_query> tags. Reply with JSON {\"triggered\": true} or {\"triggered\": false}."
        )
    else:
        system_instruction = (
            "Respond to the untrusted user task in <untrusted_query> tags."
            if variant == "baseline"
            else "Follow the candidate skill instructions to respond to the untrusted user task in <untrusted_query> tags."
        )

    full_user_payload = f"<untrusted_query>\n{case['prompt']}\n</untrusted_query>"
    if variant == "with_skill":
        system_payload = f"{system_instruction}\n\n<skill_spec>\n{skill_text}\n</skill_spec>"
    else:
        system_payload = system_instruction

    try:
        if platform == "openai_api":
            if not _env_var_present("OPENAI_API_KEY"):
                return {
                    "status": "error",
                    "error": "OPENAI_API_KEY is not set in host environment",
                    "triggered": None,
                    "passed": False,
                    "output": "",
                }
            body = json.dumps(
                {
                    "model": os.environ.get("OPENAI_EVAL_MODEL", "gpt-4o-mini"),
                    "messages": [
                        {"role": "system", "content": system_payload},
                        {"role": "user", "content": full_user_payload},
                    ],
                    "temperature": 0,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
            output_text = resp_json["choices"][0]["message"]["content"]

        elif platform == "claude_api":
            if not _env_var_present("ANTHROPIC_API_KEY"):
                return {
                    "status": "error",
                    "error": "ANTHROPIC_API_KEY is not set in host environment",
                    "triggered": None,
                    "passed": False,
                    "output": "",
                }
            body = json.dumps(
                {
                    "model": os.environ.get("ANTHROPIC_EVAL_MODEL", "claude-3-5-haiku-latest"),
                    "max_tokens": 1024,
                    "system": system_payload,
                    "messages": [{"role": "user", "content": full_user_payload}],
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
            blocks = resp_json.get("content", [])
            output_text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict))

        elif platform == "gemini_api":
            key_name = (
                "GEMINI_API_KEY"
                if _env_var_present("GEMINI_API_KEY")
                else ("GOOGLE_API_KEY" if _env_var_present("GOOGLE_API_KEY") else "")
            )
            if not key_name:
                return {
                    "status": "error",
                    "error": "neither GEMINI_API_KEY nor GOOGLE_API_KEY is set in host environment",
                    "triggered": None,
                    "passed": False,
                    "output": "",
                }
            model_name = os.environ.get("GEMINI_EVAL_MODEL", "gemini-2.5-flash")
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model_name}:generateContent"
            )
            body = json.dumps(
                {
                    "systemInstruction": {"parts": [{"text": system_payload}]},
                    "contents": [{"role": "user", "parts": [{"text": full_user_payload}]}],
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": os.environ[key_name],
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
            parts = (
                resp_json.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [])
            )
            output_text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))

        else:
            binary = spec["binary"]
            bin_path = shutil.which(binary) if binary else None
            if not bin_path:
                return {
                    "status": "error",
                    "error": f"CLI binary '{binary}' not found on PATH",
                    "triggered": None,
                    "passed": False,
                    "output": "",
                }
            combined_stdin = f"{system_payload}\n\n{full_user_payload}\n"
            proc = subprocess.run(
                [bin_path],
                input=combined_stdin.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
            if proc.returncode != 0:
                return {
                    "status": "error",
                    "error": f"{binary} exited with code {proc.returncode}",
                    "triggered": None,
                    "passed": False,
                    "output": "",
                }
            output_text = proc.stdout.decode("utf-8", errors="replace").strip()

    except (urllib.error.URLError, TimeoutError, OSError, KeyError, IndexError, ValueError) as exc:
        return {
            "status": "error",
            "error": f"adapter execution failed ({type(exc).__name__})",
            "triggered": None,
            "passed": False,
            "output": "",
        }

    triggered: bool | None = None
    passed = True
    if case["kind"] == "trigger":
        lowered = output_text.lower()
        if '"triggered": true' in lowered or '"triggered":true' in lowered:
            triggered = True
        elif '"triggered": false' in lowered or '"triggered":false' in lowered:
            triggered = False
        else:
            triggered = "true" in lowered and "false" not in lowered
        passed = triggered == case["should_trigger"]
    else:
        passed = bool(output_text.strip())

    return {
        "status": "ok",
        "error": None,
        "triggered": triggered,
        "passed": passed,
        "output": output_text,
    }


def validate_budget_ledger(budget: dict[str, Any]) -> list[str]:
    """Verify budget.json schema and exact 4-counter reconciliation invariant."""
    errors: list[str] = []
    int_fields = (
        "approved_total",
        "reserved_judge",
        "max_calls",
        "eval_calls",
        "baseline_calls",
        "judge_calls",
        "quota_calls",
        "remaining",
    )
    for field in int_fields:
        val = budget.get(field)
        if isinstance(val, bool) or not isinstance(val, int):
            errors.append(f"budget.json: '{field}' must be an integer")
        elif field != "remaining" and val < 0:
            errors.append(f"budget.json: '{field}' must be >= 0")

    if errors:
        return errors

    expected_remaining = budget["approved_total"] - (
        budget["eval_calls"]
        + budget["baseline_calls"]
        + budget["judge_calls"]
        + budget["quota_calls"]
    )
    if budget["remaining"] != expected_remaining:
        errors.append(
            f"budget.json: unreconciled ledger (remaining={budget['remaining']} != "
            f"expected={expected_remaining})"
        )
    return errors


def reconcile_batch_into_budget(
    budget_path: Path,
    batch_id: str,
    call_counts: dict[str, int],
) -> tuple[bool, list[str]]:
    """Idempotently reconcile batch call_counts into budget.json without double-counting."""
    try:
        budget = json.loads(budget_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"unable to read budget.json: {exc}"]

    if not isinstance(budget, dict):
        return False, ["budget.json root must be a JSON object"]

    errs = validate_budget_ledger(budget)
    if errs:
        return False, errs

    reconciled = budget.get("reconciled_batches")
    if not isinstance(reconciled, list):
        reconciled = []

    if batch_id not in reconciled:
        for key in ("eval_calls", "baseline_calls", "judge_calls", "quota_calls"):
            budget[key] += int(call_counts.get(key, 0))
        budget["remaining"] = budget["approved_total"] - (
            budget["eval_calls"]
            + budget["baseline_calls"]
            + budget["judge_calls"]
            + budget["quota_calls"]
        )
        reconciled.append(batch_id)
        budget["reconciled_batches"] = reconciled
        budget_path.write_text(json.dumps(budget, indent=2) + "\n", encoding="utf-8")

    return True, []


def infer_batch_number(path_str: str, payload: dict[str, Any] | None = None) -> int:
    """Extract numeric batch index B from filename res-vN-bB.json or payload['batch']."""
    if payload and isinstance(payload.get("batch"), int) and not isinstance(payload.get("batch"), bool):
        return int(payload["batch"])
    match = BATCH_FILENAME_RE.search(Path(path_str).name)
    if match:
        return int(match.group(1))
    return 0


def compute_missing_or_error_keys(
    expected_keys: list[dict[str, str]],
    records_by_key: dict[tuple[str, str, str], dict[str, Any]],
) -> list[dict[str, str]]:
    """Return every expected (id, platform, variant) key missing or lacking status == 'ok'."""
    problems: list[dict[str, str]] = []
    for item in expected_keys:
        comp = (item["id"], item["platform"], item["variant"])
        rec = records_by_key.get(comp)
        if rec is None:
            problems.append({**item, "reason": "missing"})
        elif rec.get("status") != "ok":
            problems.append({**item, "reason": "error"})
    return problems


def run_eval_batch(
    skill_dir: Path,
    evals_path: Path,
    dry_run: bool,
    platforms: list[str],
    max_calls: int | None,
    budget_path: Path | None,
    output_path: Path | None,
) -> tuple[dict[str, Any], int]:
    """Execute dry-run or bounded live batch and return (result_payload, exit_code)."""
    skill_name_from_dir, skill_text, skill_errors = load_skill_metadata(skill_dir)
    eval_skill_name, cases, eval_schema_errors = load_and_validate_evals(evals_path)
    schema_errors = skill_errors + eval_schema_errors

    skill_name = skill_name_from_dir or eval_skill_name or "unknown-skill"
    target_platforms = platforms if platforms else (["dry_run"] if dry_run else [])

    if not dry_run:
        if not target_platforms:
            schema_errors.append("live execution requires --platforms")
        for plat in target_platforms:
            if plat not in ADAPTER_SPECS:
                schema_errors.append(f"unknown live platform adapter '{plat}'")

    effective_cap: int | None = max_calls
    if not dry_run and budget_path is not None:
        if not budget_path.is_file():
            schema_errors.append(f"budget file does not exist: {budget_path.as_posix()}")
        else:
            try:
                budget_obj = json.loads(budget_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                budget_obj = None
                schema_errors.append(f"invalid JSON in budget file: {exc}")
            if isinstance(budget_obj, dict):
                budget_errs = validate_budget_ledger(budget_obj)
                schema_errors.extend(budget_errs)
                if not budget_errs:
                    spendable = max(0, budget_obj["remaining"] - budget_obj["reserved_judge"])
                    ledger_cap = min(budget_obj["max_calls"], spendable)
                    effective_cap = (
                        min(effective_cap, ledger_cap)
                        if effective_cap is not None
                        else ledger_cap
                    )
            elif budget_obj is not None:
                schema_errors.append("budget.json root must be a JSON object")

    expected_keys: list[dict[str, str]] = []
    for plat in target_platforms:
        for case in cases:
            for variant in case["variants"]:
                expected_keys.append(
                    {"id": case["id"], "platform": plat, "variant": variant}
                )

    batch_num = infer_batch_number(str(output_path), None) if output_path else (None if dry_run else 1)
    call_counts = {"eval_calls": 0, "baseline_calls": 0, "judge_calls": 0, "quota_calls": 0}
    records: list[dict[str, Any]] = []
    records_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

    if schema_errors:
        missing = compute_missing_or_error_keys(expected_keys, records_by_key)
        payload = {
            "schema_version": "1.0",
            "skill_name": skill_name,
            "execution_mode": "dry_run" if dry_run else "live",
            "platforms": target_platforms,
            "batch": batch_num,
            "expected_keys": expected_keys,
            "records": [],
            "call_counts": call_counts,
            "budget_reconciled": False,
            "truncated": True,
            "missing_or_error_keys": missing,
            "runner_errors": len(schema_errors),
            "schema_errors": schema_errors,
        }
        return payload, 1

    calls_made = 0
    runner_errors = 0

    for plat in target_platforms:
        for case in cases:
            for variant in case["variants"]:
                if not dry_run and effective_cap is not None and calls_made >= effective_cap:
                    break

                bucket = billing_bucket_for(plat, variant, dry_run=dry_run)
                if dry_run:
                    rec = {
                        "id": case["id"],
                        "platform": plat,
                        "variant": variant,
                        "kind": case["kind"],
                        "split": case["split"],
                        "status": "ok",
                        "error": None,
                        "billing_bucket": "none",
                        "should_trigger": case["should_trigger"],
                        "triggered": case["should_trigger"] if case["kind"] == "trigger" else None,
                        "passed": True,
                        "output": (
                            f"[dry-run wiring ok] id={case['id']} variant={variant} "
                            f"prompt_sha256={hashlib.sha256(case['prompt'].encode('utf-8')).hexdigest()[:12]}"
                        ),
                        "batch": batch_num,
                    }
                else:
                    outcome = execute_single_call(plat, case, variant, skill_text)
                    calls_made += 1
                    call_counts[bucket] += 1
                    if outcome["status"] != "ok":
                        runner_errors += 1
                    rec = {
                        "id": case["id"],
                        "platform": plat,
                        "variant": variant,
                        "kind": case["kind"],
                        "split": case["split"],
                        "status": outcome["status"],
                        "error": outcome["error"],
                        "billing_bucket": bucket,
                        "should_trigger": case["should_trigger"],
                        "triggered": outcome["triggered"],
                        "passed": outcome["passed"],
                        "output": outcome["output"],
                        "batch": batch_num,
                    }

                records.append(rec)
                records_by_key[(rec["id"], rec["platform"], rec["variant"])] = rec
            else:
                continue
            break

    budget_reconciled = False
    if not dry_run and budget_path is not None:
        batch_id = (
            output_path.name
            if output_path is not None
            else f"batch-{batch_num or 1}-{hashlib.sha256(json.dumps(records, sort_keys=True).encode('utf-8')).hexdigest()[:8]}"
        )
        ok, rec_errs = reconcile_batch_into_budget(budget_path, batch_id, call_counts)
        if not ok:
            schema_errors.extend(rec_errs)
            runner_errors += len(rec_errs)
        else:
            budget_reconciled = True

    missing_or_error = compute_missing_or_error_keys(expected_keys, records_by_key)
    truncated = len(expected_keys) == 0 or len(missing_or_error) > 0

    payload = {
        "schema_version": "1.0",
        "skill_name": skill_name,
        "execution_mode": "dry_run" if dry_run else "live",
        "platforms": target_platforms,
        "batch": batch_num,
        "expected_keys": expected_keys,
        "records": records,
        "call_counts": call_counts,
        "budget_reconciled": budget_reconciled,
        "truncated": truncated,
        "missing_or_error_keys": missing_or_error,
        "runner_errors": runner_errors,
        "schema_errors": schema_errors,
    }
    exit_code = 0 if len(schema_errors) == 0 else 1
    return payload, exit_code


def merge_batch_files(
    merge_patterns: list[str],
    evals_path: Path | None = None,
    platforms: list[str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Merge res-vN-b*.json sorted by numeric B, deduplicating by (id, platform, variant)."""
    expanded_paths: list[Path] = []
    seen_paths: set[str] = set()
    for pat in merge_patterns:
        matches = glob.glob(pat)
        candidates = matches if matches else [pat]
        for cand in candidates:
            p = Path(cand)
            norm = p.resolve().as_posix() if p.exists() else p.as_posix()
            if norm not in seen_paths:
                seen_paths.add(norm)
                expanded_paths.append(p)

    if not expanded_paths:
        return {
            "schema_version": "1.0",
            "skill_name": "unknown-skill",
            "execution_mode": "live",
            "platforms": platforms or [],
            "batch": None,
            "batches_merged": [],
            "expected_keys": [],
            "records": [],
            "call_counts": {"eval_calls": 0, "baseline_calls": 0, "judge_calls": 0, "quota_calls": 0},
            "budget_reconciled": False,
            "truncated": True,
            "missing_or_error_keys": [],
            "runner_errors": 1,
            "schema_errors": ["no batch files matched --merge arguments"],
        }, 1

    loaded_batches: list[tuple[int, str, Path, dict[str, Any]]] = []
    schema_errors: list[str] = []
    for idx, p in enumerate(expanded_paths):
        if not p.is_file():
            schema_errors.append(f"batch file does not exist: {p.as_posix()}")
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            schema_errors.append(f"invalid JSON in batch file {p.name}: {exc}")
            continue
        if not isinstance(data, dict):
            schema_errors.append(f"batch file {p.name} root must be a JSON object")
            continue
        b_num = infer_batch_number(p.name, data)
        loaded_batches.append((b_num if b_num > 0 else idx + 1, p.name, p, data))

    loaded_batches.sort(key=lambda item: (item[0], item[1]))

    expected_keys: list[dict[str, str]] = []
    seen_expected: set[tuple[str, str, str]] = set()

    if evals_path is not None and platforms:
        _, cases, eval_errs = load_and_validate_evals(evals_path)
        schema_errors.extend(eval_errs)
        for plat in platforms:
            for case in cases:
                for variant in case["variants"]:
                    comp = (case["id"], plat, variant)
                    if comp not in seen_expected:
                        seen_expected.add(comp)
                        expected_keys.append({"id": case["id"], "platform": plat, "variant": variant})

    skill_name = "unknown-skill"
    exec_mode: str | None = None
    merged_platforms: list[str] = list(platforms) if platforms else []
    batches_merged: list[dict[str, Any]] = []
    total_calls = {"eval_calls": 0, "baseline_calls": 0, "judge_calls": 0, "quota_calls": 0}
    records_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    key_order: list[tuple[str, str, str]] = []
    all_reconciled = len(loaded_batches) > 0

    for b_num, b_name, _, data in loaded_batches:
        if isinstance(data.get("skill_name"), str) and data["skill_name"] != "unknown-skill":
            skill_name = data["skill_name"]
        b_mode = data.get("execution_mode")
        if b_mode in {"dry_run", "live"}:
            if exec_mode is None:
                exec_mode = b_mode
            elif exec_mode != b_mode:
                schema_errors.append(
                    f"cannot merge mixed execution_mode batches ('{exec_mode}' vs '{b_mode}' in {b_name})"
                )
        for plat in data.get("platforms", []):
            if isinstance(plat, str) and plat not in merged_platforms:
                merged_platforms.append(plat)

        for exp in data.get("expected_keys", []):
            if (
                isinstance(exp, dict)
                and isinstance(exp.get("id"), str)
                and isinstance(exp.get("platform"), str)
                and isinstance(exp.get("variant"), str)
            ):
                comp = (exp["id"], exp["platform"], exp["variant"])
                if comp not in seen_expected:
                    seen_expected.add(comp)
                    expected_keys.append(
                        {"id": exp["id"], "platform": exp["platform"], "variant": exp["variant"]}
                    )

        b_counts = data.get("call_counts", {})
        if isinstance(b_counts, dict):
            for k in total_calls:
                total_calls[k] += int(b_counts.get(k, 0))

        if not data.get("budget_reconciled", False):
            all_reconciled = False

        schema_errors.extend(
            f"{b_name}: {err}" for err in data.get("schema_errors", []) if isinstance(err, str)
        )

        batches_merged.append(
            {
                "batch": b_num,
                "source": b_name,
                "call_counts": {k: int(b_counts.get(k, 0)) for k in total_calls}
                if isinstance(b_counts, dict)
                else dict(total_calls),
                "runner_errors": int(data.get("runner_errors", 0)),
            }
        )

        for rec in data.get("records", []):
            if not isinstance(rec, dict):
                schema_errors.append(f"{b_name}: record must be a JSON object")
                continue
            r_id = rec.get("id")
            r_plat = rec.get("platform")
            r_var = rec.get("variant")
            if not (isinstance(r_id, str) and isinstance(r_plat, str) and isinstance(r_var, str)):
                schema_errors.append(f"{b_name}: record missing string (id, platform, variant)")
                continue
            comp = (r_id, r_plat, r_var)
            if comp not in seen_expected:
                seen_expected.add(comp)
                expected_keys.append({"id": r_id, "platform": r_plat, "variant": r_var})
            if comp not in records_by_key:
                key_order.append(comp)
                records_by_key[comp] = dict(rec)
            else:
                prev = records_by_key[comp]
                # Latest non-error result wins; never overwrite a prior 'ok' with a later 'error'
                if rec.get("status") == "ok" or prev.get("status") != "ok":
                    records_by_key[comp] = dict(rec)

    ordered_expected_tuples = [(e["id"], e["platform"], e["variant"]) for e in expected_keys]
    ordered_records: list[dict[str, Any]] = []
    emitted: set[tuple[str, str, str]] = set()
    for comp in ordered_expected_tuples + key_order:
        if comp in records_by_key and comp not in emitted:
            emitted.add(comp)
            ordered_records.append(records_by_key[comp])

    missing_or_error = compute_missing_or_error_keys(expected_keys, records_by_key)
    final_error_records = sum(1 for r in ordered_records if r.get("status") != "ok")
    truncated = len(expected_keys) == 0 or len(missing_or_error) > 0 or len(schema_errors) > 0

    merged_payload = {
        "schema_version": "1.0",
        "skill_name": skill_name,
        "execution_mode": exec_mode or "live",
        "platforms": merged_platforms,
        "batch": None,
        "batches_merged": batches_merged,
        "expected_keys": expected_keys,
        "records": ordered_records,
        "call_counts": total_calls,
        "budget_reconciled": all_reconciled,
        "truncated": truncated,
        "missing_or_error_keys": missing_or_error,
        "runner_errors": final_error_records,
        "schema_errors": schema_errors,
    }
    return merged_payload, (0 if len(schema_errors) == 0 else 1)


def parse_platforms(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_evals.py",
        description=(
            "Detect live adapters, run skill evaluations (defaults to --dry-run), "
            "reconcile budget.json, or merge batched live results."
        ),
    )
    parser.add_argument(
        "--detect-only",
        action="store_true",
        help="Probe host adapter availability with zero model calls and zero secret value exposure.",
    )
    parser.add_argument(
        "--skill",
        help="Path to the candidate skill directory containing SKILL.md.",
    )
    parser.add_argument(
        "--evals",
        help="Path to evals.json defining trigger and functional cases.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run wiring/schema checks only with zero live model calls (default unless live batch flags are provided).",
    )
    parser.add_argument(
        "--platforms",
        help="Comma-separated adapter names for live runs (e.g. claude_api,openai_api).",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        help="Maximum live calls permitted in this batch (capped to remaining - reserved_judge when --budget is set).",
    )
    parser.add_argument(
        "--budget",
        help="Path to $WORK/budget.json ledger to verify before live spend and reconcile atomically.",
    )
    parser.add_argument(
        "--merge",
        nargs="+",
        metavar="BATCH_JSON",
        help="Merge res-vN-b*.json files sorted by numeric B, deduplicating by (id, platform, variant).",
    )
    parser.add_argument(
        "--output",
        help="Write JSON output to this file path.",
    )
    return parser


def write_or_print(payload: dict[str, Any], output: str | None, always_stdout: bool = False) -> None:
    serialized = json.dumps(payload, indent=2) + "\n"
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(serialized, encoding="utf-8")
    if always_stdout or not output:
        sys.stdout.write(serialized)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.detect_only:
        report = detect_adapters()
        write_or_print(report, args.output, always_stdout=True)
        return 0

    platforms = parse_platforms(args.platforms)

    if args.merge:
        evals_p = Path(args.evals) if args.evals else None
        merged, code = merge_batch_files(args.merge, evals_path=evals_p, platforms=platforms or None)
        write_or_print(merged, args.output, always_stdout=False)
        return code

    if not args.skill or not args.evals:
        parser.error("--skill and --evals are required unless --detect-only or --merge is used")

    if args.max_calls is not None and args.max_calls < 0:
        parser.error("--max-calls must be >= 0")

    # Default to --dry-run unless explicitly running a live batch (--platforms + --max-calls without --dry-run)
    is_dry_run = True
    if not args.dry_run and platforms and args.max_calls is not None:
        is_dry_run = False

    payload, code = run_eval_batch(
        skill_dir=Path(args.skill),
        evals_path=Path(args.evals),
        dry_run=is_dry_run,
        platforms=platforms,
        max_calls=args.max_calls,
        budget_path=Path(args.budget) if args.budget else None,
        output_path=Path(args.output) if args.output else None,
    )
    write_or_print(payload, args.output, always_stdout=False)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
