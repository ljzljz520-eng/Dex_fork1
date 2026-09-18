"""CLI: status / resume / rollback exit codes, JSON, and plain copy."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.changesets import coordinator
from core.changesets.cli import (
    EXIT_CLEAN,
    EXIT_CONFLICT,
    EXIT_FAILED,
    EXIT_PENDING,
    main,
    pending_guidance,
)
from core.changesets.storage import ChangeSetStore

TASKS_MD = "03-Tasks/Tasks.md"
PEOPLE_MD = "05-Areas/People/Jane_Doe.md"
TASK = "task-20260820-001"


def _converter(data: bytes) -> bytes:
    return re.sub(
        rb"(?<!\[\[)\^(task-\d{8}-\d{3,})(?!\]\])", rb"[[^\1]]", data
    )


def _seed(root: Path) -> list[str]:
    files = {
        TASKS_MD: f"- [ ] Call Jane ^{TASK}\n",
        PEOPLE_MD: f"# Jane\nLinked ^{TASK}\n",
    }
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return list(files)


def _prepare(root: Path) -> str:
    outcome = coordinator.prepare(root, _seed(root), _converter)
    assert outcome.status == "prepared", outcome.reason
    return outcome.changeset_id


def test_status_clean_human_and_json(tmp_path, capsys):
    _seed(tmp_path)
    code = main(["--vault", str(tmp_path), "status"])
    out = capsys.readouterr().out
    assert code == EXIT_CLEAN
    assert "Everything looks good" in out

    code = main(["--vault", str(tmp_path), "status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_CLEAN
    assert payload["status"] == "clean"


def test_status_pending_exits_3_and_names_one_resume_command(tmp_path, capsys):
    cs_id = _prepare(tmp_path)
    code = main(["--vault", str(tmp_path), "status"])
    out = capsys.readouterr().out
    assert code == EXIT_PENDING
    assert "files are exactly as you left them" in out
    assert f"python -m core.changesets resume --id {cs_id}" in out

    code = main(["--vault", str(tmp_path), "status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_PENDING
    assert payload["changesets"][0]["id"] == cs_id


def test_status_conflict_exits_4(tmp_path, capsys):
    cs_id = _prepare(tmp_path)
    store = ChangeSetStore(tmp_path)
    change = store.load(cs_id)
    (tmp_path / change.intents[0].relative).write_bytes(
        store.read_staged(cs_id, 0)
    )  # half applied

    code = main(["--vault", str(tmp_path), "status"])
    out = capsys.readouterr().out
    assert code == EXIT_CONFLICT
    assert "inconsistent file state" in out
    assert cs_id in out


def test_resume_finishes_prepared_group(tmp_path, capsys):
    cs_id = _prepare(tmp_path)
    code = main(["--vault", str(tmp_path), "resume", "--id", cs_id])
    assert code == EXIT_CLEAN
    assert "fully finished" in capsys.readouterr().out
    assert f"[[^{TASK}]]" in (tmp_path / TASKS_MD).read_text()

    code = main(
        ["--vault", str(tmp_path), "resume", "--id", cs_id, "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_CLEAN and payload["success"] is True


def test_resume_preparing_shell_is_refused(tmp_path, capsys):
    prepared_id = _prepare(tmp_path)
    coordinator.rollback(tmp_path, prepared_id)
    shell_id = ChangeSetStore(tmp_path).create()  # PREPARING, no intents

    code = main(["--vault", str(tmp_path), "resume", "--id", shell_id])
    assert code == EXIT_FAILED
    assert "Nothing to resume" in capsys.readouterr().out


def test_rollback_prepared_aborts_without_touching_files(tmp_path, capsys):
    cs_id = _prepare(tmp_path)
    before = (tmp_path / TASKS_MD).read_bytes()
    code = main(["--vault", str(tmp_path), "rollback", "--id", cs_id])
    assert code == EXIT_CLEAN
    assert "files were never touched" in capsys.readouterr().out
    assert (tmp_path / TASKS_MD).read_bytes() == before
    assert main(["--vault", str(tmp_path), "status"]) == EXIT_CLEAN


def test_rollback_committed_restores_bytes(tmp_path, capsys):
    cs_id = _prepare(tmp_path)
    coordinator.commit(tmp_path, cs_id)
    assert f"[[^{TASK}]]" in (tmp_path / TASKS_MD).read_text()

    code = main(
        ["--vault", str(tmp_path), "rollback", "--id", cs_id, "--force"]
    )
    assert code == EXIT_CLEAN
    assert "restored to its original text" in capsys.readouterr().out
    assert f"^{TASK}" in (tmp_path / TASKS_MD).read_text()
    assert f"[[^{TASK}]]" not in (tmp_path / TASKS_MD).read_text()


def test_rollback_without_snapshot_reports_git_hint(tmp_path, capsys):
    import shutil

    cs_id = _prepare(tmp_path)
    coordinator.commit(tmp_path, cs_id)
    # Remove every retained transaction snapshot.
    shutil.rmtree(tmp_path / "System" / ".dex" / "tx")

    code = main(
        ["--vault", str(tmp_path), "rollback", "--id", cs_id, "--force"]
    )
    out = capsys.readouterr().out
    assert code == EXIT_FAILED
    assert "cannot undo" in out
    assert "git" in out.lower()
    # Zero writes: conversion stays in place.
    assert f"[[^{TASK}]]" in (tmp_path / TASKS_MD).read_text()


def test_status_unknown_id_fails(tmp_path, capsys):
    _seed(tmp_path)
    code = main(
        ["--vault", str(tmp_path), "status", "--id", "19990101T000000-deadbeef"]
    )
    assert code == EXIT_FAILED
    assert "No change set" in capsys.readouterr().out


def test_pending_guidance_carries_both_commands(tmp_path):
    cs_id = _prepare(tmp_path)
    text = pending_guidance(tmp_path)
    assert text is not None
    assert "did not finish" in text
    assert f"resume --id {cs_id}" in text
    assert f"rollback --id {cs_id}" in text
    assert "not damaged" in text
    coordinator.rollback(tmp_path, cs_id)
    assert pending_guidance(tmp_path) is None


def test_pending_guidance_ignores_bare_mixed_task_state(tmp_path):
    """Issue I-2: a vault that was never migrated but whose mirror pages
    momentarily disagree on one checkbox is in the ordinary pre-sync state.
    A new migration must proceed, and the user must never be told a previous
    migration did not finish.
    """
    _seed(tmp_path)
    (tmp_path / TASKS_MD).write_text(f"- [x] Call Jane ^{TASK}\n", encoding="utf-8")
    (tmp_path / PEOPLE_MD).write_text(
        f"- [ ] Linked ^{TASK}\n", encoding="utf-8"
    )

    assert pending_guidance(tmp_path) is None


def test_pending_guidance_blocks_on_attributable_conflict_without_pending_group(
    tmp_path,
):
    """Issue I-2: a change-set-attributable conflict blocks a rerun even when
    no pending group can be summarized (unreadable event log), and the copy
    must describe the real state rather than claim a migration did not finish.
    """
    cs_id = _prepare(tmp_path)
    events = ChangeSetStore(tmp_path).journal_for(cs_id).path
    raw = events.read_text(encoding="utf-8")
    first, rest = raw.split("\n", 1)
    events.write_text(
        first.replace("PREPARING", "PREPAXING") + "\n" + rest, encoding="utf-8"
    )

    text = pending_guidance(tmp_path)
    assert text is not None
    assert cs_id in text
    assert "did not finish" not in text
    assert "unresolved file state" in text


def test_human_output_has_no_file_contents_or_absolute_paths(tmp_path, capsys):
    _prepare(tmp_path)
    main(["--vault", str(tmp_path), "status"])
    out = capsys.readouterr().out
    assert "Call Jane" not in out
    assert str(tmp_path) not in out
