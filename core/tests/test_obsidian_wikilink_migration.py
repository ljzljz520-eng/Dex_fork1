"""Command-level coverage for the Obsidian wiki-link migration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_SCRIPT = REPO_ROOT / "core/obsidian/migrate_to_wikilinks.py"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=Dex Test", "-c", "user.email=test@example.com", *args],
        cwd=vault,
        check=True,
        capture_output=True,
        text=True,
    )


def test_fresh_vault_migration_only_rewrites_user_content(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    product_files = {
        "CLAUDE.md": "Read `.claude/skills/README.md`, README, and ^task-20260128-010.\n",
        ".claude/skills/README.md": "This README is product documentation.\n",
        "docs/Dex_Technical_Guide.md": "Example: `^task-20260128-011`.\n",
    }
    placeholders = (
        "04-Projects/README.md",
        "05-Areas/People/README.md",
        "05-Areas/Companies/README.md",
    )
    for relative, content in product_files.items():
        _write(vault / relative, content)
    for relative in placeholders:
        _write(vault / relative, "Placeholder README only.\n")
    tasks = vault / "03-Tasks/Tasks.md"
    _write(
        tasks,
        "Keep README plain and `^task-20260128-001` literal.\n"
        "Convert the real anchor ^task-20260128-002.\n",
    )

    tools = tmp_path / "bin"
    for name in ("osascript", "afplay"):
        executable = tools / name
        _write(executable, "#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)

    _git(vault, "init", "-q")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-q", "-m", "pristine fresh vault")
    head_before = _git(vault, "rev-parse", "HEAD").stdout.strip()

    result = subprocess.run(
        [sys.executable, str(MIGRATION_SCRIPT)],
        cwd=vault,
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "VAULT_PATH": str(vault)},
        input="\n",
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Found 0 people" in result.stdout
    assert "Found 0 projects" in result.stdout
    assert "Found 0 companies" in result.stdout
    assert "git reset --hard HEAD~1" not in result.stdout
    assert head_before in result.stdout
    assert _git(vault, "rev-parse", "HEAD").stdout.strip() == head_before
    for relative, content in product_files.items():
        assert (vault / relative).read_text(encoding="utf-8") == content
    assert tasks.read_text(encoding="utf-8") == (
        "Keep README plain and `^task-20260128-001` literal.\n"
        "Convert the real anchor [[^task-20260128-002]].\n"
    )


def _pristine_vault_with_task(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    tasks = vault / "03-Tasks" / "Tasks.md"
    _write(
        tasks,
        "- [ ] Convert the real anchor ^task-20260301-001.\n",
    )
    _git(vault, "init", "-q")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-q", "-m", "pristine vault")
    return vault, tasks


def _run_migration(vault: Path, tmp_path: Path, *, extra_env=None, stdin="\n"):
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    for name in ("osascript", "afplay"):
        executable = tools / name
        if not executable.exists():
            _write(executable, "#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "VAULT_PATH": str(vault),
    }
    env.pop("DEX_CHANGESET_STOP_AFTER", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(MIGRATION_SCRIPT)],
        cwd=vault,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_dry_run_creates_no_changeset(tmp_path: Path) -> None:
    vault = tmp_path / "vault2"
    tasks = vault / "03-Tasks" / "Tasks.md"
    _write(tasks, "- [ ] Anchor ^task-20260301-002.\n")
    _git(vault, "init", "-q")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-q", "-m", "pristine")

    dry = subprocess.run(
        [sys.executable, str(MIGRATION_SCRIPT), "--dry-run"],
        cwd=vault,
        env={**os.environ, "VAULT_PATH": str(vault)},
        input="\n",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "[DRY RUN] No files were modified" in dry.stdout
    assert not (vault / "System" / ".dex" / "changesets").exists()
    assert tasks.read_text() == "- [ ] Anchor ^task-20260301-002.\n"


def test_rerun_after_interruption_guides_instead_of_opening_second_group(
    tmp_path: Path,
) -> None:
    vault, tasks = _pristine_vault_with_task(tmp_path)

    crashed = _run_migration(
        vault, tmp_path, extra_env={"DEX_CHANGESET_STOP_AFTER": "after-prepared"}
    )
    assert crashed.returncode == 137, crashed.stdout + crashed.stderr
    # Preparation finished but no live content changed.
    assert tasks.read_text() == "- [ ] Convert the real anchor ^task-20260301-001.\n"
    groups_root = vault / "System" / ".dex" / "changesets"
    group_ids = [p.name for p in groups_root.iterdir() if p.is_dir()]
    assert len(group_ids) == 1
    cs_id = group_ids[0]

    rerun = _run_migration(vault, tmp_path)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "did not finish" in rerun.stdout
    assert f"python -m core.changesets resume --id {cs_id}" in rerun.stdout
    assert f"python -m core.changesets rollback --id {cs_id}" in rerun.stdout
    # Still exactly one group; files still untouched.
    assert [p.name for p in groups_root.iterdir() if p.is_dir()] == [cs_id]
    assert tasks.read_text() == "- [ ] Convert the real anchor ^task-20260301-001.\n"

    # The single printed resume command actually finishes the migration.
    resumed = subprocess.run(
        [sys.executable, "-m", "core.changesets", "resume", "--id", cs_id],
        cwd=REPO_ROOT,
        env={**os.environ, "PATH": os.environ["PATH"], "VAULT_PATH": str(vault)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert tasks.read_text() == (
        "- [ ] Convert the real anchor [[^task-20260301-001]].\n"
    )


def test_bare_mixed_task_state_does_not_block_first_migration(
    tmp_path: Path,
) -> None:
    """Issue I-2: a never-migrated vault where one mirror page is ticked and
    another still open must run the migration normally — never be told a
    previous migration did not finish."""
    vault = tmp_path / "vault3"
    tasks = vault / "03-Tasks" / "Tasks.md"
    mirror = vault / "05-Areas" / "People" / "Jane.md"
    _write(tasks, "- [ ] Convert ^task-20260301-001.\n- [x] Done ^task-20260301-007\n")
    _write(mirror, "- [ ] Pending ^task-20260301-007\n")
    _git(vault, "init", "-q")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-q", "-m", "pristine")

    result = _run_migration(vault, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "did not finish" not in result.stdout
    assert "[[ ^task-20260301-001]]".replace(" ", "") in tasks.read_text(encoding="utf-8")


def test_migration_no_longer_writes_vault_files_directly() -> None:
    source = MIGRATION_SCRIPT.read_text(encoding="utf-8")
    assert "write_text" not in source
    assert "write_bytes" not in source
