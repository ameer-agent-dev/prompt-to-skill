#!/usr/bin/env python3
"""Iteration scorer and best-version selector enforcing strict mode and status precedence (Python 3 stdlib only)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


PASS_SCORE_THRESHOLD = 9.5
SCORECARD_FILENAME_RE = re.compile(r"^scorecard-v(\d+)\.json$")


def _is_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def load_json_file(path: Path, label: str) -> tuple[dict[str, Any] | None, list[str]]:
    if not path.is_file():
        return None, [f"{label}: file does not exist ({path.as_posix()})"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"{label}: invalid JSON ({exc})"]
    if not isinstance(data, dict):
        return None, [f"{label}: root must be a JSON object"]
    return data, []


def verify_eval_coverage(res: dict[str, Any]) -> tuple[bool, int, list[str]]:
    """Verify complete non-error coverage across expected (id, platform, variant) keys."""
    schema_errors: list[str] = []
    expected = res.get("expected_keys")
    records = res.get("records")

    if not isinstance(expected, list):
        schema_errors.append("eval-results: 'expected_keys' must be a list")
        expected = []
    if not isinstance(records, list):
        schema_errors.append("eval-results: 'records' must be a list")
        records = []

    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    record_error_count = 0
    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            schema_errors.append(f"eval-results: records[{idx}] must be an object")
            continue
        r_id = rec.get("id")
        r_plat = rec.get("platform")
        r_var = rec.get("variant")
        if not (isinstance(r_id, str) and isinstance(r_plat, str) and isinstance(r_var, str)):
            schema_errors.append(
                f"eval-results: records[{idx}] missing string (id, platform, variant)"
            )
            continue
        if rec.get("status") != "ok":
            record_error_count += 1
        by_key[(r_id, r_plat, r_var)] = rec

    missing_or_error = False
    if len(expected) == 0 or len(records) == 0:
        missing_or_error = True

    for idx, exp in enumerate(expected):
        if (
            not isinstance(exp, dict)
            or not isinstance(exp.get("id"), str)
            or not isinstance(exp.get("platform"), str)
            or not isinstance(exp.get("variant"), str)
        ):
            schema_errors.append(
                f"eval-results: expected_keys[{idx}] missing string (id, platform, variant)"
            )
            missing_or_error = True
            continue
        comp = (exp["id"], exp["platform"], exp["variant"])
        rec = by_key.get(comp)
        if rec is None or rec.get("status") != "ok":
            missing_or_error = True

    explicit_truncated = bool(res.get("truncated", False))
    runner_errors_field = res.get("runner_errors", 0)
    if isinstance(runner_errors_field, bool) or not isinstance(runner_errors_field, int) or runner_errors_field < 0:
        schema_errors.append("eval-results: 'runner_errors' must be a non-negative integer")
        runner_errors = record_error_count
    else:
        runner_errors = max(runner_errors_field, record_error_count)

    if runner_errors > 0:
        missing_or_error = True

    return (explicit_truncated or missing_or_error), runner_errors, schema_errors


def scorecard_ranking_key(sc: dict[str, Any]) -> tuple[int, int, float, float, float, int]:
    """Exact Step 5.1 Pick BEST 6-tier tie-breaker key."""
    errors_obj = sc.get("errors", {})
    total_errs = errors_obj.get("total", 1) if isinstance(errors_obj, dict) else 1
    zero_errors = 1 if total_errs == 0 else 0

    truncated = bool(sc.get("truncated", True))
    exec_mode = sc.get("execution_mode", "dry_run")
    grade_mode = sc.get("grading_mode", "self_graded")

    if not truncated and exec_mode == "live" and grade_mode == "external_judge":
        mode_rank = 3
    elif not truncated and exec_mode == "live" and grade_mode == "self_graded":
        mode_rank = 2
    elif not truncated and exec_mode == "dry_run":
        mode_rank = 1
    else:
        mode_rank = 0

    scores = sc.get("scores", {})
    acc = float(scores.get("accuracy", 0.0)) if isinstance(scores, dict) else 0.0
    qual = float(scores.get("quality", 0.0)) if isinstance(scores, dict) else 0.0
    easy = float(scores.get("easy_for_llms", 0.0)) if isinstance(scores, dict) else 0.0

    min_score = min(acc, qual, easy)
    holdout_rate = float(sc.get("holdout_pass_rate", 0.0))
    avg_score = (acc + qual + easy) / 3.0
    iteration = int(sc.get("iteration", 0))
    return (zero_errors, mode_rank, min_score, holdout_rate, avg_score, iteration)


def read_isolated_history(
    history_dir: Path,
    exclude_iteration: int | None = None,
) -> list[dict[str, Any]]:
    """Read prior scorecard-v*.json files strictly from the flat --history directory."""
    if not history_dir.is_dir():
        return []

    items: list[tuple[int, dict[str, Any]]] = []
    for entry in sorted(history_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_file() or entry.is_symlink():
            continue
        match = SCORECARD_FILENAME_RE.match(entry.name)
        if not match:
            continue
        it_num = int(match.group(1))
        if exclude_iteration is not None and it_num == exclude_iteration:
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("iteration"), int):
            items.append((it_num, data))

    items.sort(key=lambda pair: pair[0])
    return [sc for _, sc in items]


def score_iteration(
    validation_path: Path,
    eval_results_path: Path,
    grading_path: Path,
    iteration: int,
    max_iterations: int,
    history_dir: Path,
) -> tuple[dict[str, Any], int]:
    """Compute scorecard-vN.json enforcing strict status precedence and mode separation."""
    schema_errors: list[str] = []

    val_data, val_errs = load_json_file(validation_path, "validation")
    res_data, res_errs = load_json_file(eval_results_path, "eval-results")
    grade_data, grade_errs = load_json_file(grading_path, "grading")
    schema_errors.extend(val_errs + res_errs + grade_errs)

    static_errors = 0
    if val_data is not None:
        err_cnt = val_data.get("error_count")
        err_list = val_data.get("errors")
        valid_flag = val_data.get("valid")
        if isinstance(err_cnt, bool) or not isinstance(err_cnt, int) or err_cnt < 0:
            schema_errors.append("validation: 'error_count' must be a non-negative integer")
        else:
            static_errors = max(static_errors, err_cnt)
        if isinstance(err_list, list):
            static_errors = max(static_errors, len(err_list))
        else:
            schema_errors.append("validation: 'errors' must be a list")
        if valid_flag is False and static_errors == 0:
            static_errors = 1

    truncated = True
    runner_errors = 0
    res_exec_mode: str | None = None
    if res_data is not None:
        res_exec_mode = res_data.get("execution_mode")
        if res_exec_mode not in {"dry_run", "live"}:
            schema_errors.append(
                f"eval-results: 'execution_mode' must be 'dry_run' or 'live', got {res_exec_mode!r}"
            )
            res_exec_mode = None
        res_schema_errs = res_data.get("schema_errors", [])
        if isinstance(res_schema_errs, list):
            schema_errors.extend(f"eval-results: {e}" for e in res_schema_errs)
        else:
            schema_errors.append("eval-results: 'schema_errors' must be a list")
        truncated, runner_errors, cov_errs = verify_eval_coverage(res_data)
        schema_errors.extend(cov_errs)

    grade_exec_mode: str | None = None
    grading_mode: str | None = None
    scores = {"accuracy": 0.0, "quality": 0.0, "easy_for_llms": 0.0}
    holdout_pass_rate = 0.0
    train_pass_rate = 0.0
    functional_pass_rate = 0.0

    if grade_data is not None:
        grade_exec_mode = grade_data.get("execution_mode")
        if grade_exec_mode not in {"dry_run", "live"}:
            schema_errors.append(
                f"grading: 'execution_mode' must be 'dry_run' or 'live', got {grade_exec_mode!r}"
            )
            grade_exec_mode = None

        grading_mode = grade_data.get("grading_mode")
        if grading_mode not in {"external_judge", "self_graded"}:
            schema_errors.append(
                f"grading: 'grading_mode' must be 'external_judge' or 'self_graded', got {grading_mode!r}"
            )
            grading_mode = None

        raw_scores = grade_data.get("scores")
        if not isinstance(raw_scores, dict):
            schema_errors.append("grading: 'scores' must be an object")
        else:
            for dim in ("accuracy", "quality", "easy_for_llms"):
                val = raw_scores.get(dim)
                if not _is_number(val) or not (0.0 <= float(val) <= 10.0):
                    schema_errors.append(
                        f"grading: scores.{dim} must be a number in [0.0, 10.0]"
                    )
                else:
                    scores[dim] = float(val)

        for rate_key, target_name in (
            ("holdout_pass_rate", "holdout"),
            ("train_pass_rate", "train"),
            ("functional_pass_rate", "functional"),
        ):
            r_val = grade_data.get(rate_key, 0.0)
            if not _is_number(r_val) or not (0.0 <= float(r_val) <= 1.0):
                schema_errors.append(f"grading: '{rate_key}' must be a number in [0.0, 1.0]")
            else:
                if target_name == "holdout":
                    holdout_pass_rate = float(r_val)
                elif target_name == "train":
                    train_pass_rate = float(r_val)
                else:
                    functional_pass_rate = float(r_val)

    # Cross-check execution_mode and grading_mode separation
    if res_exec_mode and grade_exec_mode and res_exec_mode != grade_exec_mode:
        schema_errors.append(
            f"mode mismatch: eval-results execution_mode='{res_exec_mode}' != "
            f"grading execution_mode='{grade_exec_mode}'"
        )

    effective_exec_mode = res_exec_mode or grade_exec_mode or "dry_run"
    effective_grading_mode = grading_mode or "self_graded"

    if effective_exec_mode == "dry_run" and grading_mode == "external_judge":
        schema_errors.append("invalid mode combination: dry_run cannot use external_judge")
        effective_grading_mode = "self_graded"

    total_errors = static_errors + runner_errors + len(schema_errors)
    all_scores_pass = all(v >= PASS_SCORE_THRESHOLD for v in scores.values())

    # Strict status precedence:
    # truncated > pass_promote > live_self_graded_only > dry_run_wiring_passed > cap_reached
    if truncated:
        status = "truncated"
        terminal = True
    elif (
        total_errors == 0
        and all_scores_pass
        and effective_exec_mode == "live"
        and effective_grading_mode == "external_judge"
    ):
        status = "pass_promote"
        terminal = True
    elif (
        total_errors == 0
        and all_scores_pass
        and effective_exec_mode == "live"
        and effective_grading_mode == "self_graded"
    ):
        status = "live_self_graded_only"
        terminal = True
    elif (
        total_errors == 0
        and all_scores_pass
        and effective_exec_mode == "dry_run"
        and effective_grading_mode == "self_graded"
    ):
        status = "dry_run_wiring_passed"
        terminal = True
    elif iteration >= max_iterations:
        status = "cap_reached"
        terminal = True
    else:
        status = "revise"
        terminal = False

    prior_cards = read_isolated_history(history_dir, exclude_iteration=iteration)

    current_card: dict[str, Any] = {
        "schema_version": "1.0",
        "iteration": iteration,
        "max_iterations": max_iterations,
        "status": status,
        "terminal": terminal,
        "execution_mode": effective_exec_mode,
        "grading_mode": effective_grading_mode,
        "truncated": truncated,
        "scores": scores,
        "min_score": min(scores.values()),
        "average_score": round(sum(scores.values()) / 3.0, 4),
        "holdout_pass_rate": holdout_pass_rate,
        "train_pass_rate": train_pass_rate,
        "functional_pass_rate": functional_pass_rate,
        "errors": {
            "static": static_errors,
            "runner": runner_errors,
            "schema": len(schema_errors),
            "total": total_errors,
            "details": schema_errors,
        },
        "history_iterations": [int(c["iteration"]) for c in prior_cards] + [iteration],
    }

    all_cards = prior_cards + [current_card]
    best_card = max(all_cards, key=scorecard_ranking_key)
    current_card["best_iteration"] = int(best_card["iteration"])
    current_card["best_status"] = str(best_card.get("status", status))

    return current_card, 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="score_iteration.py",
        description="Score a skill iteration enforcing strict status precedence and isolated history.",
    )
    parser.add_argument(
        "--validation",
        required=True,
        help="Path to $WORK/val-vN.json from validate_skill.py.",
    )
    parser.add_argument(
        "--eval-results",
        required=True,
        help="Path to canonical $WORK/res-vN.json from Step 4.2d.",
    )
    parser.add_argument(
        "--grading",
        required=True,
        help="Path to $WORK/grade-vN.json from Step 4.3.",
    )
    parser.add_argument(
        "--iteration",
        type=int,
        required=True,
        help="Current iteration N (1..max-iterations).",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=5,
        help="Iteration ceiling (default: 5).",
    )
    parser.add_argument(
        "--history",
        required=True,
        help="Isolated $WORK directory containing prior scorecard-v*.json files.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output path for $WORK/scorecard-vN.json.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.max_iterations < 1:
        parser.error("--max-iterations must be >= 1")
    if not (1 <= args.iteration <= args.max_iterations):
        parser.error(f"--iteration must be between 1 and {args.max_iterations}")

    card, code = score_iteration(
        validation_path=Path(args.validation),
        eval_results_path=Path(args.eval_results),
        grading_path=Path(args.grading),
        iteration=args.iteration,
        max_iterations=args.max_iterations,
        history_dir=Path(args.history),
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
