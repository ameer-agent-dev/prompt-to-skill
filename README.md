# prompt-to-skill

Turn any rough prompt or recurring workflow into a validated, portable Agent Skill (`SKILL.md` plus optional `references/` and `scripts/`) via a bounded evaluation loop inside a run-isolated workspace.

Built on the [Open Agent Skills specification](https://agentskills.io/specification), Agent Plugins 1.0.0 (`plugin.json`), and `skills.sh` zero-config discovery across Claude 2.0 / Claude Code, ChatGPT 2.0 / Codex, Gemini CLI, OpenCode, and hosted runtimes.

## Quick Install

Preview discovered skill metadata before installing:

```bash
npx skills add ameer-agent-dev/prompt-to-skill --list
```

Install into your agent client:

```bash
npx skills add ameer-agent-dev/prompt-to-skill
```

## Key Features

- **Bounded 5-iteration loop**: Drafts candidate skill packages and custom eval suites (20 train/holdout trigger cases plus 3 to 5 functional prompts checked against a no-skill baseline) inside an isolated sibling workspace (`.prompt-to-skill-work/`).
- **Zero-dependency Python 3 stdlib scripts**: Ships `validate_skill.py`, `run_evals.py`, and `score_iteration.py` with zero third-party `pip` packages required.
- **Strict spend and overwrite guardrails**: Defaults `run_evals.py` to `--dry-run`, enforces four-counter live spend accounting (`eval_calls`, `baseline_calls`, `judge_calls`, `quota_calls`), and performs lock-protected delivery swaps with signal-safe rollback.
- **Honest verification gates**: Separates `execution_mode` (`dry_run` vs. `live`) from `grading_mode` (`self_graded` vs. `external_judge`) so dry-run or self-graded runs never masquerade as `pass_promote`.
- **Graceful environment fallback**: Falls back cleanly to manual Core Standards and inline file emission when shell, Python 3, POSIX `sh`, or write permissions are unavailable.

## Repository Layout

```text
./
├── SKILL.md                       # Canonical Agent Skill specification and workflow
├── README.md                      # Overview, install, safety model, and local verification
├── LICENSE                        # MIT License
├── CHANGELOG.md                   # Release history
├── .gitignore                     # Workdir, Python bytecode, virtualenv, and env exclusions
├── plugin.json                    # Agent Plugins 1.0.0 manifest
├── references/
│   ├── rubric.md                  # 0-10 Accuracy / Quality / Easy for LLMs rubric and grade-vN.json schema
│   ├── eval-design.md             # evals.json, res-vN*.json, merge rules, and budget.json contract
│   └── platform-adapters.md       # Client discovery paths, invocation syntax, and adapter mapping
├── scripts/
│   ├── validate_skill.py          # Static frontmatter, formatting, secret scan, and POSIX sh -n validator
│   ├── run_evals.py               # Zero-leak adapter detector, dry-run/live runner, budget reconciler, merger
│   └── score_iteration.py         # Status precedence scorer and isolated history best-iteration selector
├── tests/
│   ├── test_scripts.py            # End-to-end dry-run, CLI contracts, merge/budget, and prompt-injection tests
│   └── test_shell_guards.py       # 24-case live POSIX sh preflight, revise, lock, symlink, and rollback suite
└── .github/
    └── workflows/
        └── ci.yml                 # GitHub Actions CI running validator, sh -n, and full unittest suite
```

## Safety Model

1. **Approval before spend**: `scripts/run_evals.py` defaults to `--dry-run`. Live batches require explicit user approval, enforce `BATCH <= min(MAX_CALLS, remaining - reserved_judge)`, and reconcile `$WORK/budget.json` across four mutually exclusive counters before any subsequent call.
2. **Zero secret exposure**: `--detect-only` checks host environment variable presence as booleans without reading, printing, or logging secret values. `scripts/validate_skill.py` scans skill files to block hardcoded API keys and tokens.
3. **Prompt-injection isolation**: Candidate skill files, rough user prompts, and `evals.json` inputs are parsed strictly as inert UTF-8 data and never interpolated into shell commands or allowed to spoof `pass_promote`.
4. **Locked delivery with rollback**: Step 5 checks both `-e` and `-L` (catching dangling symlinks), requires `CONFIRMED_OVERWRITE=yes` before replacing an existing skill directory, holds an exclusive lock directory, and restores `$BAK` automatically on `INT`, `TERM`, `HUP`, or mid-swap failure.

## Local Verification Commands

Run static skill validation:

```bash
python3 scripts/validate_skill.py . --json
```

Probe host adapters with zero model calls:

```bash
python3 scripts/run_evals.py --detect-only
```

Run the full automated Python and 24-case POSIX `sh` test suite:

```bash
python3 -m unittest discover -s tests -v
```

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 ameer.
