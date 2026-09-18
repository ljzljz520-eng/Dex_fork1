"""Prepare/commit coordinator: staging, invariants, lock, all-or-nothing."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from core.changesets import coordinator
from core.changesets.coordinator import (
    NOOP,
    PREPARED_STATUS,
    REJECTED,
    ChangeSetConflict,
    REASON_CONTRACT_FORBIDS,
    REASON_LIVE_DRIFT,
    REASON_PENDING_GROUP,
    REASON_TASK_INVARIANT,
)
from core.changesets.storage import ChangeSetStore
from core.transaction.lock import LOCK_RELATIVE, LockBusyError, acquire_owned_lock

TASKS_MD = "03-Tasks/Tasks.md"
PEOPLE_MD = "05-Areas/People/Jane_Doe.md"


def _wikilink_converter(data: bytes) -> bytes:
    return re.sub(
        rb"(?<!\[\[)\^(task-\d{8}-\d{3,})(?!\]\])",
        rb"[[^\1]]",
        data,
    )


def _seed_vault(root: Path) -> list[str]:
    tasks = root / TASKS_MD
    people = root / PEOPLE_MD
    tasks.parent.mkdir(parents=True)
    people.parent.mkdir(parents=True)
    tasks.write_text(
        "# Tasks\n- [ ] Call Jane ^task-20260118-001\n"
        "- [x] Done thing ^task-20260118-002\n",
        encoding="utf-8",
    )
    people.write_text(
        "# Jane Doe\nLinked task: ^task-20260118-001\n", encoding="utf-8"
    )
    # An unrelated file the converter leaves byte-identical.
    notes = root / "04-Projects/Launch.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("# Launch\nNo references here.\n", encoding="utf-8")
    return [TASKS_MD, PEOPLE_MD, "04-Projects/Launch.md"]


def _sha(root: Path, relative: str) -> str:
    return hashlib.sha256((root / relative).read_bytes()).hexdigest()


def test_prepare_then_commit_replaces_whole_group(tmp_path: Path):
    targets = _seed_vault(tmp_path)
    old_tasks = _sha(tmp_path, TASKS_MD)

    outcome = coordinator.prepare(tmp_path, targets, _wikilink_converter)

    assert outcome.status == PREPARED_STATUS
    assert outcome.files_scanned == 3
    assert outcome.files_changed == 2

    store = ChangeSetStore(tmp_path)
    change = store.load(outcome.changeset_id)
    assert change.occurrences_before == change.occurrences_after
    intent = change.intent_for(TASKS_MD)
    assert intent.old_sha256 == old_tasks

    result = coordinator.commit(tmp_path, outcome.changeset_id)

    assert result["committed"] is True
    converted = (tmp_path / TASKS_MD).read_text(encoding="utf-8")
    assert "[[^task-20260118-001]]" in converted
    assert " ^task-20260118-001" not in converted
    assert _sha(tmp_path, TASKS_MD) == intent.new_sha256
    assert store.load(outcome.changeset_id).state == "committed"
    # Staging is released at the terminal state; the tx snapshot remains.
    assert store.staged_indices(outcome.changeset_id) == []
    assert not (tmp_path / LOCK_RELATIVE).exists()


def test_no_changes_is_noop_and_leaves_no_ledger(tmp_path: Path):
    targets = _seed_vault(tmp_path)
    # First conversion lands, second run finds nothing left to convert.
    first = coordinator.prepare(tmp_path, targets, _wikilink_converter)
    coordinator.commit(tmp_path, first.changeset_id)

    second = coordinator.prepare(tmp_path, targets, _wikilink_converter)

    assert second.status == NOOP
    # Only the committed group remains; no PREPARING shell was left behind.
    assert store_ids(tmp_path) == [first.changeset_id]


def test_task_invariant_violation_rejects_without_touching_vault(tmp_path: Path):
    targets = _seed_vault(tmp_path)
    before = {rel: _sha(tmp_path, rel) for rel in targets}

    # This converter drops the anchor on the person page: the task ID's file
    # set shrinks, which the protocol must refuse.
    def dropping_converter(data: bytes) -> bytes:
        return data.replace(b"Linked task: ^task-20260118-001", b"Linked task: done")

    outcome = coordinator.prepare(tmp_path, targets, dropping_converter)

    assert outcome.status == REJECTED
    assert outcome.reason == REASON_TASK_INVARIANT
    violations = outcome.detail["violations"]
    assert len(violations) == 1
    assert violations[0]["task_id"] == "task-20260118-001"
    assert violations[0]["kind"] == "missing-file"
    assert violations[0]["relative"] == PEOPLE_MD

    assert {rel: _sha(tmp_path, rel) for rel in targets} == before
    store = ChangeSetStore(tmp_path)
    failed_id = outcome.changeset_id
    assert store.load(failed_id).state == "prepare-failed"
    assert store.staged_indices(failed_id) == []
    # Prepare never acquires the vault mutation lock.
    assert not (tmp_path / LOCK_RELATIVE).exists()


def test_contract_forbidden_target_rejects(tmp_path: Path):
    _seed_vault(tmp_path)
    brain_skill = tmp_path / ".claude/skills/daily-plan/SKILL.md"
    brain_skill.parent.mkdir(parents=True)
    brain_skill.write_text("# skill ^task-20260118-001", encoding="utf-8")

    outcome = coordinator.prepare(
        tmp_path,
        [TASKS_MD, ".claude/skills/daily-plan/SKILL.md"],
        _wikilink_converter,
    )

    assert outcome.status == REJECTED
    assert outcome.reason == REASON_CONTRACT_FORBIDS
    assert outcome.detail["locations"] == [
        ".claude/skills/daily-plan/SKILL.md"
    ]


def test_non_markdown_target_rejects(tmp_path: Path):
    _seed_vault(tmp_path)
    notes = tmp_path / "03-Tasks/notes.txt"
    notes.write_text("plain text", encoding="utf-8")

    outcome = coordinator.prepare(
        tmp_path, [TASKS_MD, "03-Tasks/notes.txt"], _wikilink_converter
    )

    assert outcome.status == REJECTED
    assert outcome.reason == "not-markdown"


def test_second_prepare_is_refused_while_one_is_pending(tmp_path: Path):
    targets = _seed_vault(tmp_path)
    first = coordinator.prepare(tmp_path, targets, _wikilink_converter)

    second = coordinator.prepare(tmp_path, targets, _wikilink_converter)

    assert second.status == REJECTED
    assert second.reason == REASON_PENDING_GROUP
    assert second.detail["pending_changesets"] == [first.changeset_id]


def test_commit_holds_vault_lock_for_second_mutator(tmp_path: Path):
    targets = _seed_vault(tmp_path)
    outcome = coordinator.prepare(tmp_path, targets, _wikilink_converter)

    def second_mutator_is_busy() -> None:
        with pytest.raises(LockBusyError):
            acquire_owned_lock(tmp_path, "second-mutator")

    result = coordinator.commit(
        tmp_path, outcome.changeset_id, during_commit=second_mutator_is_busy
    )
    assert result["committed"] is True


def test_drift_between_prepare_and_commit_rolls_whole_group_back(tmp_path: Path):
    targets = _seed_vault(tmp_path)
    untouched_before = _sha(tmp_path, TASKS_MD)
    outcome = coordinator.prepare(tmp_path, targets, _wikilink_converter)

    # A live edit lands on one target after staging.
    (tmp_path / PEOPLE_MD).write_text(
        "# Jane\nuser edited this file\n", encoding="utf-8"
    )

    with pytest.raises(ChangeSetConflict) as error:
        coordinator.commit(tmp_path, outcome.changeset_id)
    assert error.value.reason == REASON_LIVE_DRIFT
    assert error.value.detail["locations"] == [PEOPLE_MD]

    store = ChangeSetStore(tmp_path)
    reloaded = store.load(outcome.changeset_id)
    assert reloaded.state == "prepared"
    # User bytes win everywhere; no target was half-converted.
    assert "user edited" in (tmp_path / PEOPLE_MD).read_text(encoding="utf-8")
    assert _sha(tmp_path, TASKS_MD) == untouched_before
    assert not (tmp_path / LOCK_RELATIVE).exists()

    # After the conflict is removed, explicit resume completes the group.
    (tmp_path / PEOPLE_MD).write_text(
        "# Jane Doe\nLinked task: ^task-20260118-001\n", encoding="utf-8"
    )
    resumed = coordinator.resume(tmp_path, outcome.changeset_id)
    assert resumed["committed"] is True
    assert "[[^task-20260118-001]]" in (tmp_path / TASKS_MD).read_text(
        encoding="utf-8"
    )


def store_ids(root: Path) -> list[str]:
    return ChangeSetStore(root).list_ids()
