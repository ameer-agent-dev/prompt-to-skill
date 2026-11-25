#!/usr/bin/env python3
"""Static validator for portable Agent Skills (Python 3 stdlib only)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any


NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
HTML_ENTITY_RE = re.compile(
    r"&(?:[a-zA-Z][a-zA-Z0-9]{1,31}|#[0-9]{1,7}|#x[0-9a-fA-F]{1,6});"
)
LIST_ITEM_RE = re.compile(r"^(\s*)(?:([-*+])|(\d+\.))\s+\S")
FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
INLINE_CODE_RE = re.compile(r"`+[^`]*`+")
WIN_DRIVE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[A-Za-z0-9_.\-\\]+")
REL_BACKSLASH_PATH_RE = re.compile(
    r"(?:^|[\s(\[\"'`])(?:scripts|references|assets|\$WORK|\$PROJECT_DIR|\$SKILL_DIR|\.\.?)"
    r"\\[A-Za-z0-9_.\-\\]+"
)
SECRET_PATTERNS = [
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{24,}\b")),
    ("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{24,}\b")),
    ("github_fine_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{24,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{32,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
]


def find_posix_sh() -> str | None:
    """Locate a POSIX sh binary across Unix and Windows (Git for Windows)."""
    found = shutil.which("sh")
    if found:
        return found
    if os.name == "nt":
        for candidate in (
            r"C:\Program Files\Git\bin\sh.exe",
            r"C:\Program Files\Git\usr\bin\sh.exe",
            r"C:\Program Files (x86)\Git\bin\sh.exe",
        ):
            if os.path.isfile(candidate):
                return candidate
    return None


def unescape_double_quoted_yaml(raw_inner: str) -> tuple[str | None, str | None]:
    """Validate and unescape a YAML double-quoted scalar string."""
    out: list[str] = []
    i = 0
    n = len(raw_inner)
    escapes = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "b": "\b",
        "f": "\f",
        "0": "\0",
    }
    while i < n:
        ch = raw_inner[i]
        if ch == '"':
            return None, "unescaped double quote inside double-quoted YAML scalar"
        if ch == "\\":
            if i + 1 >= n:
                return None, "trailing backslash at end of double-quoted YAML scalar"
            nxt = raw_inner[i + 1]
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
            return None, f"invalid YAML escape sequence '\\{nxt}'"
        out.append(ch)
        i += 1
    return "".join(out), None


def parse_frontmatter(lines: list[str]) -> tuple[dict[str, tuple[str, int]], int | None, list[str]]:
    """Parse flat YAML frontmatter, preserving raw scalar strings and line numbers."""
    errors: list[str] = []
    fields: dict[str, tuple[str, int]] = {}
    if not lines or lines[0].rstrip("\r\n") != "---":
        return fields, None, ["SKILL.md:1: missing opening YAML frontmatter delimiter '---'"]

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r\n") == "---":
            end_idx = idx
            break
    if end_idx is None:
        return fields, None, ["SKILL.md:1: missing closing YAML frontmatter delimiter '---'"]

    for idx in range(1, end_idx):
        raw_line = lines[idx].rstrip("\r\n")
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in raw_line or raw_line[0].isspace():
            errors.append(
                f"SKILL.md:{idx + 1}: frontmatter entries must be single-line 'key: value' scalars"
            )
            continue
        key, raw_val = raw_line.split(":", 1)
        key = key.strip()
        val = raw_val.strip()
        if not re.match(r"^[a-zA-Z0-9_-]+$", key):
            errors.append(f"SKILL.md:{idx + 1}: invalid frontmatter key '{key}'")
            continue
        if key in fields:
            errors.append(f"SKILL.md:{idx + 1}: duplicate frontmatter key '{key}'")
            continue
        fields[key] = (val, idx + 1)

    return fields, end_idx, errors


def strip_inline_code(line: str) -> str:
    """Remove inline backtick spans from a single markdown line."""
    return INLINE_CODE_RE.sub("", line)


def scan_markdown_file(
    rel_path: str,
    text: str,
    require_toc_over_100: bool,
) -> tuple[list[str], list[tuple[int, str]]]:
    """Check dashes, HTML entities, list spacing, path slashes, TOC, and collect bash blocks."""
    errors: list[str] = []
    bash_blocks: list[tuple[int, str]] = []
    lines = text.splitlines()

    for idx, line in enumerate(lines, start=1):
        if "\u2014" in line:
            errors.append(f"{rel_path}:{idx}: em-dash (U+2014) is prohibited")
        if "\u2013" in line:
            errors.append(f"{rel_path}:{idx}: en-dash (U+2013) is prohibited")

    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_lang = ""
    fence_start_line = 0
    fence_buf: list[str] = []
    has_contents_heading = False

    prev_line = ""
    prev_list_sig: tuple[str, str] | None = None

    for idx, line in enumerate(lines, start=1):
        fence_match = FENCE_RE.match(line)
        if not in_fence:
            if fence_match:
                marker = fence_match.group(2)
                info = fence_match.group(3).strip().lower()
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
                fence_lang = info.split()[0] if info else ""
                fence_start_line = idx
                fence_buf = []
                prev_line = line
                prev_list_sig = None
                continue

            if re.match(r"^##\s+Contents\s*$", line.strip()):
                has_contents_heading = True

            unquoted = strip_inline_code(line)
            ent = HTML_ENTITY_RE.search(unquoted)
            if ent:
                errors.append(
                    f"{rel_path}:{idx}: raw HTML entity '{ent.group(0)}' is prohibited"
                )

            if WIN_DRIVE_PATH_RE.search(line) or REL_BACKSLASH_PATH_RE.search(line):
                errors.append(
                    f"{rel_path}:{idx}: backslash file path detected; use forward slashes"
                )

            list_match = LIST_ITEM_RE.match(line)
            if list_match:
                indent = list_match.group(1)
                kind = "bullet" if list_match.group(2) else "ordered"
                curr_sig = (indent, kind)
                if idx > 1 and prev_line.strip() != "" and prev_list_sig != curr_sig:
                    errors.append(
                        f"{rel_path}:{idx}: missing blank line before Markdown list"
                    )
                prev_list_sig = curr_sig
            else:
                prev_list_sig = None

            prev_line = line
        else:
            if fence_match:
                marker = fence_match.group(2)
                rest = fence_match.group(3).strip()
                if marker[0] == fence_char and len(marker) >= fence_len and rest == "":
                    if fence_lang in {"bash", "sh", "posix"}:
                        bash_blocks.append((fence_start_line, "\n".join(fence_buf) + "\n"))
                    in_fence = False
                    fence_char = ""
                    fence_len = 0
                    fence_lang = ""
                    fence_buf = []
                    prev_line = line
                    prev_list_sig = None
                    continue
            fence_buf.append(line)

    if in_fence:
        errors.append(f"{rel_path}:{fence_start_line}: unclosed fenced code block")

    if require_toc_over_100 and len(lines) > 100 and not has_contents_heading:
        errors.append(
            f"{rel_path}:1: reference file exceeds 100 lines ({len(lines)}) and lacks '## Contents'"
        )

    return errors, bash_blocks


def check_bash_blocks(rel_path: str, blocks: list[tuple[int, str]]) -> list[str]:
    """Run POSIX sh -n syntax checks on extracted shell blocks."""
    if not blocks:
        return []
    sh_bin = find_posix_sh()
    if not sh_bin:
        return [f"{rel_path}:1: POSIX 'sh' binary not found; cannot verify bash blocks with sh -n"]

    errors: list[str] = []
    for start_line, code in blocks:
        proc = subprocess.run(
            [sh_bin, "-n"],
            input=code.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            err_msg = proc.stderr.decode("utf-8", errors="replace").strip()
            err_msg = " | ".join(part.strip() for part in err_msg.splitlines() if part.strip())
            errors.append(
                f"{rel_path}:{start_line}: sh -n failed on bash block: {err_msg or 'syntax error'}"
            )
    return errors


def scan_for_secrets(skill_dir: Path) -> list[str]:
    """Scan skill package files for obvious hardcoded credential patterns."""
    errors: list[str] = []
    skip_dirs = {".git", "__pycache__", ".prompt-to-skill-work", ".venv", "node_modules"}
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
        for fname in sorted(files):
            fpath = Path(root) / fname
            if fpath.suffix.lower() not in {".md", ".py", ".sh", ".json", ".yml", ".yaml", ".toml"}:
                continue
            try:
                content = fpath.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            rel = fpath.relative_to(skill_dir).as_posix()
            for idx, line in enumerate(content.splitlines(), start=1):
                for label, pattern in SECRET_PATTERNS:
                    if pattern.search(line):
                        errors.append(
                            f"{rel}:{idx}: potential hardcoded credential detected ({label})"
                        )
    return errors


def validate_skill(skill_dir: Path) -> dict[str, Any]:
    """Validate an Agent Skill directory and return a structured report."""
    errors: list[str] = []
    warnings: list[str] = []

    resolved_dir = skill_dir.resolve()
    folder_name = resolved_dir.name
    skill_md = resolved_dir / "SKILL.md"

    if not resolved_dir.exists() or not resolved_dir.is_dir():
        return {
            "schema_version": "1.0",
            "skill_dir": str(skill_dir),
            "skill_name": folder_name,
            "valid": False,
            "error_count": 1,
            "warning_count": 0,
            "errors": [f"{skill_dir}: skill directory does not exist"],
            "warnings": [],
            "metrics": {
                "line_count": 0,
                "estimated_tokens": 0,
                "bash_blocks_checked": 0,
                "reference_files_checked": 0,
            },
        }

    if not skill_md.is_file():
        return {
            "schema_version": "1.0",
            "skill_dir": resolved_dir.as_posix(),
            "skill_name": folder_name,
            "valid": False,
            "error_count": 1,
            "warning_count": 0,
            "errors": ["SKILL.md: missing required SKILL.md at skill root"],
            "warnings": [],
            "metrics": {
                "line_count": 0,
                "estimated_tokens": 0,
                "bash_blocks_checked": 0,
                "reference_files_checked": 0,
            },
        }

    try:
        text = skill_md.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return {
            "schema_version": "1.0",
            "skill_dir": resolved_dir.as_posix(),
            "skill_name": folder_name,
            "valid": False,
            "error_count": 1,
            "warning_count": 0,
            "errors": [f"SKILL.md:1: file is not valid UTF-8 ({exc})"],
            "warnings": [],
            "metrics": {
                "line_count": 0,
                "estimated_tokens": 0,
                "bash_blocks_checked": 0,
                "reference_files_checked": 0,
            },
        }

    raw_lines = text.splitlines()
    line_count = len(raw_lines)
    estimated_tokens = (len(text) + 3) // 4

    if line_count >= 500:
        errors.append(f"SKILL.md:1: line count {line_count} exceeds < 500 line ceiling")
    if estimated_tokens >= 5000:
        errors.append(
            f"SKILL.md:1: estimated tokens {estimated_tokens} exceeds < 5,000 token ceiling"
        )
    elif estimated_tokens > 2000:
        warnings.append(
            f"SKILL.md:1: estimated tokens {estimated_tokens} exceeds 2,000 standard target (within 5,000 ceiling)"
        )

    fields, _, fm_errors = parse_frontmatter(raw_lines)
    errors.extend(fm_errors)

    skill_name = folder_name
    if "name" not in fields:
        errors.append("SKILL.md:1: missing required frontmatter field 'name'")
    else:
        raw_name, name_line = fields["name"]
        if len(raw_name) >= 2 and raw_name[0] == raw_name[-1] and raw_name[0] in {'"', "'"}:
            parsed_name = raw_name[1:-1]
        else:
            parsed_name = raw_name
        skill_name = parsed_name
        if not (1 <= len(parsed_name) <= 64) or not NAME_RE.match(parsed_name):
            errors.append(
                f"SKILL.md:{name_line}: 'name' must be 1-64 lowercase kebab-case chars "
                f"(a-z, 0-9, single hyphens), got '{parsed_name}'"
            )
        if parsed_name != folder_name:
            errors.append(
                f"SKILL.md:{name_line}: frontmatter name '{parsed_name}' does not match folder '{folder_name}'"
            )

    if "description" not in fields:
        errors.append("SKILL.md:1: missing required frontmatter field 'description'")
    else:
        raw_desc, desc_line = fields["description"]
        if len(raw_desc) < 2 or not (raw_desc.startswith('"') and raw_desc.endswith('"')):
            errors.append(
                f"SKILL.md:{desc_line}: 'description' must be a double-quoted YAML string"
            )
        else:
            unescaped, esc_err = unescape_double_quoted_yaml(raw_desc[1:-1])
            if esc_err is not None or unescaped is None:
                errors.append(f"SKILL.md:{desc_line}: invalid 'description' string: {esc_err}")
            else:
                if len(unescaped.strip()) == 0:
                    errors.append(f"SKILL.md:{desc_line}: 'description' must not be empty")
                if len(unescaped) >= 1024:
                    errors.append(
                        f"SKILL.md:{desc_line}: 'description' length {len(unescaped)} must be < 1,024 chars"
                    )
                lower_desc = unescaped.lower()
                if "use when" not in lower_desc and "when to use" not in lower_desc:
                    warnings.append(
                        f"SKILL.md:{desc_line}: 'description' should state when to use the skill"
                    )
                if "do not use" not in lower_desc and "never use" not in lower_desc:
                    warnings.append(
                        f"SKILL.md:{desc_line}: 'description' should state when not to use the skill"
                    )

    if "compatibility" in fields:
        raw_comp, comp_line = fields["compatibility"]
        if len(raw_comp) >= 2 and raw_comp[0] == '"' and raw_comp[-1] == '"':
            comp_val, comp_err = unescape_double_quoted_yaml(raw_comp[1:-1])
            if comp_err is not None or comp_val is None:
                errors.append(f"SKILL.md:{comp_line}: invalid 'compatibility' string: {comp_err}")
                comp_val = ""
        elif len(raw_comp) >= 2 and raw_comp[0] == "'" and raw_comp[-1] == "'":
            comp_val = raw_comp[1:-1]
        else:
            comp_val = raw_comp
        if len(comp_val) >= 500:
            errors.append(
                f"SKILL.md:{comp_line}: 'compatibility' length {len(comp_val)} must be < 500 chars"
            )

    md_errors, bash_blocks = scan_markdown_file("SKILL.md", text, require_toc_over_100=False)
    errors.extend(md_errors)
    errors.extend(check_bash_blocks("SKILL.md", bash_blocks))

    ref_count = 0
    refs_dir = resolved_dir / "references"
    if refs_dir.exists():
        if not refs_dir.is_dir():
            errors.append("references: must be a directory when present")
        else:
            for entry in sorted(refs_dir.iterdir(), key=lambda p: p.name):
                rel_entry = f"references/{entry.name}"
                if entry.is_dir():
                    errors.append(
                        f"{rel_entry}: nested subdirectories inside references/ are prohibited; keep references one level deep"
                    )
                    continue
                if entry.suffix.lower() != ".md":
                    continue
                ref_count += 1
                ref_text = entry.read_text(encoding="utf-8")
                ref_errors, ref_bash_blocks = scan_markdown_file(
                    rel_entry, ref_text, require_toc_over_100=True
                )
                errors.extend(ref_errors)
                errors.extend(check_bash_blocks(rel_entry, ref_bash_blocks))

    scripts_dir = resolved_dir / "scripts"
    sh_scripts_checked = 0
    if scripts_dir.is_dir():
        for sh_file in sorted(scripts_dir.glob("*.sh")):
            sh_scripts_checked += 1
            sh_code = sh_file.read_text(encoding="utf-8")
            errors.extend(check_bash_blocks(f"scripts/{sh_file.name}", [(1, sh_code)]))

    errors.extend(scan_for_secrets(resolved_dir))

    return {
        "schema_version": "1.0",
        "skill_dir": resolved_dir.as_posix(),
        "skill_name": skill_name,
        "valid": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "line_count": line_count,
            "estimated_tokens": estimated_tokens,
            "bash_blocks_checked": len(bash_blocks) + sh_scripts_checked,
            "reference_files_checked": ref_count,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_skill.py",
        description="Validate an Agent Skill directory against Open Agent Skills and formatting rules.",
    )
    parser.add_argument(
        "skill_dir",
        help="Path to the skill directory containing SKILL.md.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit validation report as JSON to stdout.",
    )
    parser.add_argument(
        "--output",
        help="Write the JSON validation report to this file path.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    report = validate_skill(Path(args.skill_dir))

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if args.as_json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        status = "VALID" if report["valid"] else "INVALID"
        sys.stdout.write(
            f"{status}: {report['skill_name']} "
            f"(errors={report['error_count']}, warnings={report['warning_count']}, "
            f"lines={report['metrics']['line_count']}, "
            f"est_tokens={report['metrics']['estimated_tokens']})\n"
        )
        for err in report["errors"]:
            sys.stdout.write(f"  ERROR: {err}\n")
        for warn in report["warnings"]:
            sys.stdout.write(f"  WARN:  {warn}\n")

    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
