# Changelog

All notable changes to `prompt-to-skill` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-26

### Added

- Canonical `SKILL.md` defining the 5-step bounded workflow, 6 Hard Rules, environment fallback, offline mode, and signal-safe locked delivery swap.
- Reference specifications in `references/rubric.md`, `references/eval-design.md`, and `references/platform-adapters.md` with JSON schemas and client invocation guides.
- Zero-dependency Python 3 stdlib scripts:

  - `scripts/validate_skill.py` for YAML frontmatter, token/line ceilings, Markdown formatting, reference `## Contents`, secret scanning, and POSIX `sh -n` checks.
  - `scripts/run_evals.py` for zero-secret-leak `--detect-only` probing, default `--dry-run` execution, 4-counter `budget.json` reconciliation, and numeric-batch `(id, platform, variant)` merging.
  - `scripts/score_iteration.py` enforcing `truncated > pass_promote > live_self_graded_only > dry_run_wiring_passed > cap_reached` status precedence and isolated `--history` best-version selection.

- Agent Plugins 1.0.0 manifest (`plugin.json`) and GitHub Actions CI workflow (`.github/workflows/ci.yml`).
- Automated test suites in `tests/test_scripts.py` and `tests/test_shell_guards.py` (including 24 live POSIX `sh` failure-injection cases).
