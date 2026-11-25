#!/usr/bin/env python3
"""24-case live POSIX sh failure-injection suite testing canonical bash blocks extracted directly from SKILL.md."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import validate_skill  # noqa: E402


def to_posix_sh_path(path: Path) -> str:
    """Convert a host filesystem Path into a POSIX '/...' path for sh (including Git Bash on Windows)."""
    raw = path.resolve().as_posix()
    if os.name == "nt" and len(raw) >= 2 and raw[1] == ":":
        return f"/{raw[0].lower()}{raw[2:]}"
    return raw


def extract_canonical_bash_blocks() -> tuple[str, str, str]:
    """Extract the 3 fenced bash blocks (Preflight, Revise, Deliver) directly from SKILL.md."""
    text = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)
    if len(blocks) != 3:
        raise AssertionError(f"Expected exactly 3 bash blocks in SKILL.md, found {len(blocks)}")
    return blocks[0], blocks[1], blocks[2]


def extract_exists_check_snippet() -> str:
    """Extract the inline Step 5.2 EXISTS/CLEAR check directly from SKILL.md."""
    text = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(
        r"`(if \[ -e \"\$DEST\" \] \|\| \[ -L \"\$DEST\" \]; then echo EXISTS; else echo CLEAR; fi)`",
        text,
    )
    if not match:
        raise AssertionError("Could not locate inline EXISTS/CLEAR snippet in SKILL.md")
    return match.group(1)


class TestSkillMdPosixShellGuards(unittest.TestCase):
    """Live POSIX sh execution of all 24 preflight, revise, lock, symlink, collision, and rollback guards."""

    sh_bin: str

    @classmethod
    def setUpClass(cls) -> None:
        found = validate_skill.find_posix_sh()
        if not found:
            raise AssertionError("POSIX sh binary is required to run test_shell_guards.py")
        cls.sh_bin = found
        cls.preflight_tpl, cls.revise_tpl, cls.deliver_tpl = extract_canonical_bash_blocks()
        cls.exists_snippet = extract_exists_check_snippet()

    def _base_env(self, shim_dir: Path, include_python3: bool = True) -> dict[str, str]:
        env = dict(os.environ)
        shim_dir.mkdir(parents=True, exist_ok=True)
        sh_dir = str(Path(self.sh_bin).parent)
        usr_bin = str(Path(self.sh_bin).parent.parent / "usr" / "bin")

        if include_python3:
            py_posix = to_posix_sh_path(Path(sys.executable))
            py_shim = shim_dir / "python3"
            py_shim.write_text(f'#!/bin/sh\nexec "{py_posix}" "$@"\n', encoding="utf-8", newline="\n")
            py_shim.chmod(0o755)
            path_parts = [str(shim_dir), sh_dir]
            if os.path.isdir(usr_bin):
                path_parts.append(usr_bin)
            path_parts.extend(["/usr/bin", "/bin"])
        else:
            # Build an isolated bin dir with core POSIX utilities (dirname, mkdir, etc.) and NO python3
            isolated_bin = shim_dir / "no-py-bin"
            isolated_bin.mkdir(parents=True, exist_ok=True)
            util_Script = (
                'for cmd in sh dirname mkdir cp mv rm rmdir ln cat touch sleep kill; do\n'
                '  p="$(command -v "$cmd" 2>/dev/null || true)"\n'
                '  if [ -n "$p" ] && [ -x "$p" ]; then\n'
                '    printf "#!/bin/sh\\nexec \\"%s\\" \\"\\$@\\"\\n" "$p" > "$1/$cmd"\n'
                '    chmod +x "$1/$cmd"\n'
                '  fi\n'
                'done\n'
            )
            setup_env = dict(env)
            setup_env["PATH"] = os.pathsep.join(
                [sh_dir, usr_bin, env.get("PATH", "")]
                if os.path.isdir(usr_bin)
                else [sh_dir, env.get("PATH", "")]
            )
            subprocess.run(
                [self.sh_bin, "-c", util_Script, "sh", to_posix_sh_path(isolated_bin)],
                env=setup_env,
                check=True,
                capture_output=True,
            )
            path_parts = [str(isolated_bin)]

        env["PATH"] = os.pathsep.join(path_parts)
        if os.name == "nt":
            env["MSYS"] = "winsymlinks:lnk"
        return env

    def _run_sh(self, script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.sh_bin, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _render_preflight(
        self,
        skill_dir: str,
        project_dir: str,
        skill_name: str = "demo-skill",
        run_id: str = "20261126T010000Z-42",
        n: str = "1",
    ) -> str:
        s = self.preflight_tpl
        replacements = [
            ('SKILL_DIR="<abs-skill-dir>"', f'SKILL_DIR="{skill_dir}"'),
            ('PROJECT_DIR="<abs-project-dir>"', f'PROJECT_DIR="{project_dir}"'),
            ('SKILL_NAME="<kebab-name>"', f'SKILL_NAME="{skill_name}"'),
            ('RUN_ID="<unique-run-id>"', f'RUN_ID="{run_id}"'),
            ('N="<current-iteration>"', f'N="{n}"'),
        ]
        for old, new in replacements:
            self.assertIn(old, s)
            s = s.replace(old, new, 1)
        return s

    def _render_deliver(
        self,
        project_dir: str,
        work_root: str,
        work: str,
        skill_name: str,
        best: str = "1",
        confirmed: str | None = None,
        remove_bak: str | None = None,
    ) -> str:
        header = [
            f'PROJECT_DIR="{project_dir}"',
            f'WORK_ROOT="{work_root}"',
            f'WORK="{work}"',
            f'SKILL_NAME="{skill_name}"',
        ]
        if confirmed is not None:
            header.append(f'CONFIRMED_OVERWRITE="{confirmed}"')
        if remove_bak is not None:
            header.append(f'REMOVE_BAK="{remove_bak}"')
        body = self.deliver_tpl.replace(
            'BEST="<winning-iteration-from-scorecards>"', f'BEST="{best}"', 1
        )
        return "\n".join(header) + "\n" + body

    def _prepare_delivery_fixture(self, root: Path, skill_name: str = "demo-skill") -> tuple[Path, Path, Path, dict[str, str]]:
        env = self._base_env(root / "shim")
        project_dir = root / "repo" / ".claude" / "skills"
        work_root = root / "repo" / ".claude" / ".prompt-to-skill-work"
        work = work_root / skill_name / "run-1"
        v1 = work / "v1" / skill_name
        v1.mkdir(parents=True, exist_ok=True)
        (v1 / "SKILL.md").write_text("---\nname: demo-skill\ndescription: \"v1\"\n---\n", encoding="utf-8")
        return project_dir, work_root, work, env

    def _create_dangling_symlink(self, link_path: Path, env: dict[str, str]) -> None:
        link_path.parent.mkdir(parents=True, exist_ok=True)
        missing_target = to_posix_sh_path(link_path.parent / "definitely-nonexistent-target")
        link_posix = to_posix_sh_path(link_path)
        proc = self._run_sh(
            f'ln -s "{missing_target}" "{link_posix}" && [ -L "{link_posix}" ] && [ ! -e "{link_posix}" ]',
            env,
        )
        self.assertEqual(proc.returncode, 0, f"Failed to create dangling symlink: {proc.stderr}")

    # --- Group A: Step 1 Preflight Guards (Cases 1..11) ---

    def test_01_preflight_ok_creates_isolated_work_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._base_env(root / "shim")
            proj = root / "app" / ".claude" / "skills"
            script = self._render_preflight(to_posix_sh_path(REPO_ROOT), to_posix_sh_path(proj))
            proc = self._run_sh(script, env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "PREFLIGHT_OK")
            expected_v1 = root / "app" / ".claude" / ".prompt-to-skill-work" / "demo-skill" / "20261126T010000Z-42" / "v1" / "demo-skill"
            self.assertTrue(expected_v1.is_dir())

    def test_02_preflight_rejects_relative_skill_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._base_env(root / "shim")
            script = self._render_preflight("relative/skill/dir", to_posix_sh_path(root / "skills"))
            proc = self._run_sh(script, env)
            self.assertIn("PREFLIGHT_FAIL:BAD_INPUT", proc.stdout)
            self.assertNotIn("PREFLIGHT_OK", proc.stdout)

    def test_03_preflight_rejects_relative_project_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._base_env(root / "shim")
            script = self._render_preflight(to_posix_sh_path(REPO_ROOT), "relative/project/dir")
            proc = self._run_sh(script, env)
            self.assertIn("PREFLIGHT_FAIL:BAD_INPUT", proc.stdout)
            self.assertNotIn("PREFLIGHT_OK", proc.stdout)

    def test_04_preflight_rejects_traversal_or_invalid_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._base_env(root / "shim")
            for bad_run_id in ("", "../escape", "run..1", "bad space", "semi;colon"):
                script = self._render_preflight(
                    to_posix_sh_path(REPO_ROOT),
                    to_posix_sh_path(root / "skills"),
                    run_id=bad_run_id,
                )
                proc = self._run_sh(script, env)
                self.assertIn("PREFLIGHT_FAIL:BAD_INPUT", proc.stdout, f"run_id={bad_run_id!r}")
                self.assertNotIn("PREFLIGHT_OK", proc.stdout)

    def test_05_preflight_rejects_out_of_range_iteration_n(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._base_env(root / "shim")
            for bad_n in ("0", "6", "10", "abc", ""):
                script = self._render_preflight(
                    to_posix_sh_path(REPO_ROOT),
                    to_posix_sh_path(root / "skills"),
                    n=bad_n,
                )
                proc = self._run_sh(script, env)
                self.assertIn("PREFLIGHT_FAIL:BAD_INPUT", proc.stdout, f"N={bad_n!r}")
                self.assertNotIn("PREFLIGHT_OK", proc.stdout)

    def test_06_preflight_rejects_non_kebab_skill_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._base_env(root / "shim")
            for bad_name in ("", "-leading", "trailing-", "double--dash", "UpperCase", "under_score"):
                script = self._render_preflight(
                    to_posix_sh_path(REPO_ROOT),
                    to_posix_sh_path(root / "skills"),
                    skill_name=bad_name,
                )
                proc = self._run_sh(script, env)
                self.assertIn("PREFLIGHT_FAIL:BAD_NAME", proc.stdout, f"name={bad_name!r}")
                self.assertNotIn("PREFLIGHT_OK", proc.stdout)

    def test_07_preflight_rejects_skill_name_over_64_chars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._base_env(root / "shim")
            script = self._render_preflight(
                to_posix_sh_path(REPO_ROOT),
                to_posix_sh_path(root / "skills"),
                skill_name="a" * 65,
            )
            proc = self._run_sh(script, env)
            self.assertIn("PREFLIGHT_FAIL:BAD_NAME", proc.stdout)
            self.assertNotIn("PREFLIGHT_OK", proc.stdout)

    def test_08_preflight_rejects_work_inside_skills_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._base_env(root / "shim")
            # When PROJECT_DIR="/", dirname("/") is "/" -> WORK_ROOT="/.prompt-to-skill-work" is inside PROJECT_DIR/*
            script = self._render_preflight(to_posix_sh_path(REPO_ROOT), "/")
            proc = self._run_sh(script, env)
            self.assertIn("PREFLIGHT_FAIL:WORK_INSIDE_SKILLS", proc.stdout)
            self.assertNotIn("PREFLIGHT_OK", proc.stdout)

    def test_09_preflight_fails_when_python3_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._base_env(root / "shim", include_python3=False)
            script = self._render_preflight(
                to_posix_sh_path(REPO_ROOT), to_posix_sh_path(root / "skills")
            )
            proc = self._run_sh(script, env)
            self.assertIn("PREFLIGHT_FAIL:NO_PYTHON", proc.stdout)
            self.assertNotIn("PREFLIGHT_OK", proc.stdout)

    def test_10_preflight_fails_when_bundled_script_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._base_env(root / "shim")
            partial_skill = root / "partial-skill"
            (partial_skill / "scripts").mkdir(parents=True)
            (partial_skill / "scripts" / "validate_skill.py").write_text("# stub\n", encoding="utf-8")
            (partial_skill / "scripts" / "run_evals.py").write_text("# stub\n", encoding="utf-8")
            # Omit score_iteration.py
            script = self._render_preflight(
                to_posix_sh_path(partial_skill), to_posix_sh_path(root / "skills")
            )
            proc = self._run_sh(script, env)
            self.assertIn("PREFLIGHT_FAIL:MISSING_score_iteration.py", proc.stdout)
            self.assertNotIn("PREFLIGHT_OK", proc.stdout)

    def test_11_preflight_fails_on_unwritable_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._base_env(root / "shim")
            proj_parent = root / "locked-parent"
            proj_parent.mkdir()
            # Place regular file at .prompt-to-skill-work so mkdir -p "$V" fails across all OSes/privileges
            (proj_parent / ".prompt-to-skill-work").write_text("blocker", encoding="utf-8")
            script = self._render_preflight(
                to_posix_sh_path(REPO_ROOT),
                to_posix_sh_path(proj_parent / "skills"),
            )
            proc = self._run_sh(script, env)
            self.assertIn("PREFLIGHT_FAIL:NO_WRITE", proc.stdout)
            self.assertNotIn("PREFLIGHT_OK", proc.stdout)

    # --- Group B: Step 4.5 Revise Guards (Cases 12..13) ---

    def test_12_revise_copies_and_increments_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, work, env = self._prepare_delivery_fixture(root)
            v1 = work / "v1" / "demo-skill"
            script = (
                f'WORK="{to_posix_sh_path(work)}"\n'
                'SKILL_NAME="demo-skill"\n'
                'N="1"\n'
                f'V="{to_posix_sh_path(v1)}"\n'
                + self.revise_tpl
                + '\necho "AFTER_N=$N AFTER_V=$V"\n'
            )
            proc = self._run_sh(script, env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("AFTER_N=2", proc.stdout)
            self.assertTrue((work / "v2" / "demo-skill" / "SKILL.md").is_file())

    def test_13_revise_fails_when_source_skill_md_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._base_env(root / "shim")
            work = root / "work"
            empty_v1 = work / "v1" / "demo-skill"
            empty_v1.mkdir(parents=True)
            script = (
                f'WORK="{to_posix_sh_path(work)}"\n'
                'SKILL_NAME="demo-skill"\n'
                'N="1"\n'
                f'V="{to_posix_sh_path(empty_v1)}"\n'
                + self.revise_tpl
                + '\necho "STILL_N=$N"\n'
            )
            proc = self._run_sh(script, env)
            self.assertIn("REVISE_FAIL:COPY", proc.stdout)
            self.assertIn("STILL_N=1", proc.stdout)

    # --- Group C: Step 5.2 Delivery, Symlink, Lock, Collision, Rollback & Cleanup (Cases 14..24) ---

    def test_14_deliver_fresh_clear_destination_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir, work_root, work, env = self._prepare_delivery_fixture(root)
            dest = project_dir / "demo-skill"

            chk = self._run_sh(f'DEST="{to_posix_sh_path(dest)}"; {self.exists_snippet}', env)
            self.assertEqual(chk.stdout.strip(), "CLEAR")

            script = self._render_deliver(
                to_posix_sh_path(project_dir),
                to_posix_sh_path(work_root),
                to_posix_sh_path(work),
                "demo-skill",
                best="1",
            )
            proc = self._run_sh(script, env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("DELIVER_OK:", proc.stdout)
            self.assertTrue((dest / "SKILL.md").is_file())
            self.assertFalse((work / "stage-demo-skill").exists())
            self.assertFalse((work_root / "demo-skill.lock").exists())

    def test_15_deliver_existing_dest_unconfirmed_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir, work_root, work, env = self._prepare_delivery_fixture(root)
            dest = project_dir / "demo-skill"
            dest.mkdir(parents=True)
            (dest / "SKILL.md").write_text("OLD_CONTENT", encoding="utf-8")

            script = self._render_deliver(
                to_posix_sh_path(project_dir),
                to_posix_sh_path(work_root),
                to_posix_sh_path(work),
                "demo-skill",
                best="1",
            )
            proc = self._run_sh(script, env)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("DELIVER_FAIL:UNCONFIRMED", proc.stdout)
            self.assertEqual((dest / "SKILL.md").read_text(encoding="utf-8"), "OLD_CONTENT")
            self.assertFalse((work / "stage-demo-skill").exists())
            self.assertFalse((work_root / "demo-skill.lock").exists())

    def test_16_deliver_dangling_symlink_dest_detected_and_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir, work_root, work, env = self._prepare_delivery_fixture(root)
            dest = project_dir / "demo-skill"
            self._create_dangling_symlink(dest, env)

            # Inline check must report EXISTS via -L even though -e is false
            chk = self._run_sh(f'DEST="{to_posix_sh_path(dest)}"; {self.exists_snippet}', env)
            self.assertEqual(chk.stdout.strip(), "EXISTS")

            # Unconfirmed swap must block with exit 2
            unconf = self._run_sh(
                self._render_deliver(
                    to_posix_sh_path(project_dir),
                    to_posix_sh_path(work_root),
                    to_posix_sh_path(work),
                    "demo-skill",
                ),
                env,
            )
            self.assertEqual(unconf.returncode, 2)
            self.assertIn("DELIVER_FAIL:UNCONFIRMED", unconf.stdout)

            # Confirmed swap backs up dangling symlink to $BAK and installs new skill cleanly
            conf = self._run_sh(
                self._render_deliver(
                    to_posix_sh_path(project_dir),
                    to_posix_sh_path(work_root),
                    to_posix_sh_path(work),
                    "demo-skill",
                    confirmed="yes",
                ),
                env,
            )
            self.assertEqual(conf.returncode, 0, conf.stderr)
            self.assertIn("DELIVER_OK:", conf.stdout)
            self.assertTrue((dest / "SKILL.md").is_file())
            bak_chk = self._run_sh(
                f'([ -L "{to_posix_sh_path(work / "bak-demo-skill")}" ] || [ -f "{to_posix_sh_path(work / "bak-demo-skill")}" ]) && echo BAK_SAVED',
                env,
            )
            self.assertEqual(bak_chk.stdout.strip(), "BAK_SAVED")

    def test_17_deliver_confirmed_overwrite_preserves_backup_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir, work_root, work, env = self._prepare_delivery_fixture(root)
            dest = project_dir / "demo-skill"
            dest.mkdir(parents=True)
            (dest / "SKILL.md").write_text("PREVIOUS_V0", encoding="utf-8")

            proc = self._run_sh(
                self._render_deliver(
                    to_posix_sh_path(project_dir),
                    to_posix_sh_path(work_root),
                    to_posix_sh_path(work),
                    "demo-skill",
                    confirmed="yes",
                    remove_bak="no",
                ),
                env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("DELIVER_OK:", proc.stdout)
            self.assertIn("name: demo-skill", (dest / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual(
                (work / "bak-demo-skill" / "SKILL.md").read_text(encoding="utf-8"),
                "PREVIOUS_V0",
            )

    def test_18_deliver_confirmed_overwrite_removes_backup_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir, work_root, work, env = self._prepare_delivery_fixture(root)
            dest = project_dir / "demo-skill"
            dest.mkdir(parents=True)
            (dest / "SKILL.md").write_text("PREVIOUS_V0", encoding="utf-8")

            proc = self._run_sh(
                self._render_deliver(
                    to_posix_sh_path(project_dir),
                    to_posix_sh_path(work_root),
                    to_posix_sh_path(work),
                    "demo-skill",
                    confirmed="yes",
                    remove_bak="yes",
                ),
                env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("DELIVER_OK:", proc.stdout)
            self.assertFalse((work / "bak-demo-skill").exists())

    def test_19_deliver_rejects_invalid_best_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir, work_root, work, env = self._prepare_delivery_fixture(root)
            for bad_best in ("0", "6", "none"):
                proc = self._run_sh(
                    self._render_deliver(
                        to_posix_sh_path(project_dir),
                        to_posix_sh_path(work_root),
                        to_posix_sh_path(work),
                        "demo-skill",
                        best=bad_best,
                    ),
                    env,
                )
                self.assertEqual(proc.returncode, 1)
                self.assertIn("DELIVER_FAIL:BAD_BEST", proc.stdout)

    def test_20_deliver_lock_contention_preserves_existing_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir, work_root, work, env = self._prepare_delivery_fixture(root)
            lock_dir = work_root / "demo-skill.lock"
            lock_dir.mkdir(parents=True)
            (lock_dir / "owner.pid").write_text("99999", encoding="utf-8")

            proc = self._run_sh(
                self._render_deliver(
                    to_posix_sh_path(project_dir),
                    to_posix_sh_path(work_root),
                    to_posix_sh_path(work),
                    "demo-skill",
                ),
                env,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("DELIVER_FAIL:LOCKED", proc.stdout)
            self.assertTrue((lock_dir / "owner.pid").is_file(), "Active lock must not be removed")

    def test_21_deliver_stage_or_bak_collision_preserves_unowned_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir, work_root, work, env = self._prepare_delivery_fixture(root)
            stage_dir = work / "stage-demo-skill"
            bak_link = work / "bak-demo-skill"
            stage_dir.mkdir(parents=True)
            (stage_dir / "keep.txt").write_text("KEEP_STAGE", encoding="utf-8")
            self._create_dangling_symlink(bak_link, env)

            proc = self._run_sh(
                self._render_deliver(
                    to_posix_sh_path(project_dir),
                    to_posix_sh_path(work_root),
                    to_posix_sh_path(work),
                    "demo-skill",
                ),
                env,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("DELIVER_FAIL:COLLISION", proc.stdout)
            # Neither unowned $STAGE nor unowned $BAK may be deleted by cleanup()
            self.assertTrue((stage_dir / "keep.txt").is_file())
            bak_still = self._run_sh(f'[ -L "{to_posix_sh_path(bak_link)}" ] && echo STILL_THERE', env)
            self.assertEqual(bak_still.stdout.strip(), "STILL_THERE")
            self.assertFalse((work_root / "demo-skill.lock").exists())

    def test_22_deliver_copy_failure_cleans_stage_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir, work_root, work, env = self._prepare_delivery_fixture(root)
            # Remove SKILL.md from v1 candidate so cp/check fails after STAGE_OWNED=1
            (work / "v1" / "demo-skill" / "SKILL.md").unlink()

            proc = self._run_sh(
                self._render_deliver(
                    to_posix_sh_path(project_dir),
                    to_posix_sh_path(work_root),
                    to_posix_sh_path(work),
                    "demo-skill",
                ),
                env,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("DELIVER_FAIL:COPY", proc.stdout)
            self.assertFalse((work / "stage-demo-skill").exists())
            self.assertFalse((work_root / "demo-skill.lock").exists())

    def test_23_deliver_mid_swap_failure_rolls_back_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir, work_root, work, env = self._prepare_delivery_fixture(root)
            dest = project_dir / "demo-skill"
            dest.mkdir(parents=True)
            (dest / "SKILL.md").write_text("SAFE_ORIGINAL", encoding="utf-8")

            # Intercept mv via inherited POSIX shell function:
            # 1st mv ($DEST -> $BAK) succeeds; 2nd mv ($STAGE -> $DEST) fails;
            # 3rd mv ($BAK -> $DEST in cleanup rollback) succeeds!
            count_file = to_posix_sh_path(root / "mv-count.txt")
            mv_hook = (
                "mv() {\n"
                f'  CF="{count_file}"\n'
                '  n=0; [ -f "$CF" ] && n=$(cat "$CF")\n'
                '  n=$((n + 1)); echo "$n" > "$CF"\n'
                '  if [ "$n" -eq 2 ]; then return 1; fi\n'
                '  command mv "$@"\n'
                "}\n"
            )
            script = mv_hook + self._render_deliver(
                to_posix_sh_path(project_dir),
                to_posix_sh_path(work_root),
                to_posix_sh_path(work),
                "demo-skill",
                confirmed="yes",
            )
            proc = self._run_sh(script, env)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("DELIVER_FAIL:SWAP_ROLLBACK", proc.stdout)
            # Original skill restored at $DEST, $STAGE and $LOCK cleaned up
            self.assertEqual((dest / "SKILL.md").read_text(encoding="utf-8"), "SAFE_ORIGINAL")
            self.assertFalse((work / "stage-demo-skill").exists())
            self.assertFalse((work_root / "demo-skill.lock").exists())

    def test_24_deliver_mid_swap_sigterm_rolls_back_and_manual_recovery_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir, work_root, work, env = self._prepare_delivery_fixture(root)
            dest = project_dir / "demo-skill"
            dest.mkdir(parents=True)
            (dest / "SKILL.md").write_text("PRE_SIGTERM_ORIGINAL", encoding="utf-8")

            count_file = to_posix_sh_path(root / "sig-mv-count.txt")
            # On 2nd mv ($STAGE -> $DEST), child sh sends SIGTERM to its parent ($PPID == delivery subshell)
            # so trap 'cleanup; trap - EXIT; exit 1' INT TERM HUP fires mid-swap!
            sig_hook = (
                "mv() {\n"
                f'  CF="{count_file}"\n'
                '  n=0; [ -f "$CF" ] && n=$(cat "$CF")\n'
                '  n=$((n + 1)); echo "$n" > "$CF"\n'
                '  if [ "$n" -eq 2 ]; then\n'
                '    sh -c \'kill -TERM "$PPID"\'\n'
                "    return 1\n"
                "  fi\n"
                '  command mv "$@"\n'
                "}\n"
            )
            proc = self._run_sh(
                sig_hook
                + self._render_deliver(
                    to_posix_sh_path(project_dir),
                    to_posix_sh_path(work_root),
                    to_posix_sh_path(work),
                    "demo-skill",
                    confirmed="yes",
                ),
                env,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                (dest / "SKILL.md").read_text(encoding="utf-8"),
                "PRE_SIGTERM_ORIGINAL",
                f"SIGTERM rollback failed: stdout={proc.stdout} stderr={proc.stderr}",
            )
            self.assertFalse((work / "stage-demo-skill").exists())
            self.assertFalse((work_root / "demo-skill.lock").exists())

            # Also verify DELIVER_FAIL:MANUAL_RECOVERY:$BAK when $DEST unexpectedly reappears before cleanup rollback
            (root / "sig-mv-count.txt").unlink()
            intruder_hook = (
                "mv() {\n"
                f'  CF="{count_file}"\n'
                '  n=0; [ -f "$CF" ] && n=$(cat "$CF")\n'
                '  n=$((n + 1)); echo "$n" > "$CF"\n'
                '  if [ "$n" -eq 2 ]; then\n'
                '    mkdir -p "$2"\n'
                '    echo "INTRUDER" > "$2/intruder.txt"\n'
                "    return 1\n"
                "  fi\n"
                '  command mv "$@"\n'
                "}\n"
            )
            rec_proc = self._run_sh(
                intruder_hook
                + self._render_deliver(
                    to_posix_sh_path(project_dir),
                    to_posix_sh_path(work_root),
                    to_posix_sh_path(work),
                    "demo-skill",
                    confirmed="yes",
                ),
                env,
            )
            self.assertEqual(rec_proc.returncode, 1)
            self.assertIn("DELIVER_FAIL:MANUAL_RECOVERY:", rec_proc.stdout)
            self.assertEqual(
                (work / "bak-demo-skill" / "SKILL.md").read_text(encoding="utf-8"),
                "PRE_SIGTERM_ORIGINAL",
            )


if __name__ == "__main__":
    unittest.main()
