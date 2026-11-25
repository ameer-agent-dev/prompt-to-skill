# Platform Adapters, Discovery Paths, and Invocation Settings

## Contents

- [Supported Clients Overview](#supported-clients-overview)
- [Discovery Paths and Invocation Syntax](#discovery-paths-and-invocation-syntax)
- [User-Only Invocation and Boundary Enforcement](#user-only-invocation-and-boundary-enforcement)
- [Live Evaluation Adapters and Billing Buckets](#live-evaluation-adapters-and-billing-buckets)
- [Zero-Secret-Leak `--detect-only` Contract](#zero-secret-leak---detect-only-contract)
- [Hosted Runtimes and `skills.sh` Portability](#hosted-runtimes-and-skillssh-portability)

## Supported Clients Overview

Skills built with `prompt-to-skill` follow the Open Agent Skills specification (`agentskills.io/specification`) so a single `<skill-name>/SKILL.md` folder works portably across:

- **Claude 2.0 / Claude Code** (`claude_api`, `claude_cli`)
- **ChatGPT 2.0 / Codex** (`openai_api`, `codex_cli`)
- **Gemini CLI** (`gemini_api`, `gemini_cli`)
- **OpenCode** (`opencode_cli`)
- **Hosted Agent Skills runtimes and `skills.sh` installers**

Stage 1 discovery loads only YAML frontmatter `name` and `description`. Stage 2 loads `SKILL.md` body when triggered. Stage 3 loads `references/*`, `scripts/*`, or `assets/*` on demand.

## Discovery Paths and Invocation Syntax

| Client | Project Discovery Path | Personal Discovery Path | Manual Invocation |
| :--- | :--- | :--- | :--- |
| Claude 2.0 / Claude Code | `.claude/skills/<name>/SKILL.md` | `~/.claude/skills/<name>/SKILL.md` | `/<name>` |
| ChatGPT 2.0 / Codex | `.agents/skills/<name>/SKILL.md` or `.codex/skills/<name>/SKILL.md` | `~/.agents/skills/<name>/SKILL.md` or `~/.codex/skills/<name>/SKILL.md` | `$<name>` or `/skills` |
| Gemini CLI | `.gemini/skills/<name>/SKILL.md` or `.agents/skills/<name>/SKILL.md` | `~/.gemini/skills/<name>/SKILL.md` | `@<name>` or `/skills` |
| OpenCode | `.opencode/skills/<name>/SKILL.md` or `.claude/skills/<name>/SKILL.md` | `~/.config/opencode/skills/<name>/SKILL.md` | `/<name>` |

Additional packaging notes:

- Single-project skills need only `<skill-name>/SKILL.md` (plus `references/` or `scripts/` when justified).
- Multi-project or cross-team distribution can include a root `plugin.json` manifest (Agent Plugins 1.0.0) and install via `npx skills add <owner>/<repo>`.

## User-Only Invocation and Boundary Enforcement

Skills with side effects (paid API spend, external writes, deploys, messaging, or destructive mutations) should document two boundary layers:

1. **Client-specific manual-only trigger settings** (optional extensions outside the core `agentskills.io` frontmatter spec):

   - **Claude Code**: Set `disable-model-invocation: true` in frontmatter when targeting Claude Code specifically so only explicit `/<name>` runs the skill, or scope tool permissions in `.claude/settings.json`.
   - **ChatGPT 2.0 / Codex**: Invoke explicitly with `$<name>` and enforce command/write approval gates via Codex `approval_policy` and sandbox settings.
   - **Gemini CLI**: Require explicit tool confirmation in `.gemini/settings.json` rather than auto-approving shell or network actions.
   - **OpenCode**: Set `permission.skill` rules (`"ask"` or `"deny"` for model-initiated loads) in `opencode.json`.

2. **Real enforcement boundaries**:

   - A `SKILL.md` file is procedural guidance, not an access-control boundary.
   - Local `.claude/settings.json`, CLI flags, and pre-tool hooks do not travel when a skill is copied to another repo or executed in a hosted runtime.
   - Enforce hard spend caps, write locks, and confirmation gates inside bundled `scripts/` and host permissions.

## Live Evaluation Adapters and Billing Buckets

`scripts/run_evals.py` probes seven adapter identifiers split across API-billed and quota-billed CLI buckets:

| Adapter ID | Kind | Host Requirement | `with_skill` Counter | `baseline` Counter |
| :--- | :--- | :--- | :--- | :--- |
| `claude_api` | API | `ANTHROPIC_API_KEY` set in env | `eval_calls` | `baseline_calls` |
| `openai_api` | API | `OPENAI_API_KEY` set in env | `eval_calls` | `baseline_calls` |
| `gemini_api` | API | `GEMINI_API_KEY` or `GOOGLE_API_KEY` set in env | `eval_calls` | `baseline_calls` |
| `claude_cli` | Quota CLI | `claude` binary on `PATH` | `quota_calls` | `quota_calls` |
| `codex_cli` | Quota CLI | `codex` binary on `PATH` | `quota_calls` | `quota_calls` |
| `gemini_cli` | Quota CLI | `gemini` binary on `PATH` | `quota_calls` | `quota_calls` |
| `opencode_cli` | Quota CLI | `opencode` binary on `PATH` | `quota_calls` | `quota_calls` |

Every live call increments exactly one counter in `$WORK/budget.json`. Quota CLI calls never increment `eval_calls`, `baseline_calls`, or `judge_calls`.

## Zero-Secret-Leak `--detect-only` Contract

Running `python3 scripts/run_evals.py --detect-only` inspects adapter readiness before any spend decision:

- Makes **zero** network requests, subprocess model invocations, or paid calls.
- Checks environment variable presence as a boolean (`key in os.environ and os.environ[key] != ""`) without copying, slicing, hashing, logging, or printing any secret value.
- Checks CLI binary presence with `shutil.which(<binary>)` and reports only boolean availability (not user home paths or tokens).
- Emits deterministic JSON to `stdout`:

```json
{
  "schema_version": "1.0",
  "mode": "detect_only",
  "calls_made": 0,
  "has_live_adapters": false,
  "usable_adapters": [],
  "adapters": {
    "claude_api": {
      "kind": "api",
      "billing_mode": "api",
      "env_vars": ["ANTHROPIC_API_KEY"],
      "env_present": false,
      "binary": null,
      "binary_present": false,
      "available": false
    }
  }
}
```

## Hosted Runtimes and `skills.sh` Portability

- Public GitHub repositories containing a valid root `SKILL.md` (or `skills/<name>/SKILL.md`) are immediately discoverable via `npx skills add <owner>/<repo> --list` and installable via `npx skills add <owner>/<repo>`.
- Hosted container sandboxes may lack network access, third-party `pip` packages, or POSIX tools. Bundled scripts must rely exclusively on Python 3 stdlib and trigger Rule 5 inline fallback cleanly if Python 3, POSIX `sh`, or write permissions are unavailable.
