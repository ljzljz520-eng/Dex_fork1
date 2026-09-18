"""Read-only consistency envelope: clean / pending / conflict surfaces."""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.changesets import coordinator
from core.changesets.consistency import (
    STATUS_CLEAN,
    STATUS_CONFLICT,
    STATUS_PENDING,
    consistency_scan,
    fanout_gate,
    pending_overlap,
)
from core.changesets.storage import (
    EV_COMMITTING,
    ChangeSetStore,
)

TASKS_MD = "03-Tasks/Tasks.md"
PEOPLE_MD = "05-Areas/People/Jane_Doe.md"
LAUNCH_MD = "04-Projects/Launch.md"

TASK = "task-20260118-001"


def _converter(data: bytes) -> bytes:
    return re.sub(
        rb"(?<!\[\[)\^(task-\d{8}-\d{3,})(?!\]\])", rb"[[^\1]]", data
    )


def _seed(root: Path) -> list[str]:
    files = {
        TASKS_MD: (
            f"# Tasks\n- [ ] Call Jane ^{TASK}\n- [x] Done ^task-20260118-002\n"
        ),
        PEOPLE_MD: f"# Jane\nLinked: ^{TASK}\n",
        LAUNCH_MD: "# Launch\nplain notes only\n",
    }
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return list(files)


def _prepared(root: Path, targets: list[str]) -> str:
    outcome = coordinator.prepare(root, targets, _converter)
    assert outcome.status == "prepared", outcome.reason
    return outcome.changeset_id


def _kinds(envelope: dict) -> set[str]:
    return {conflict["kind"] for conflict in envelope["conflicts"]}


def test_clean_vault(tmp_path: Path):
    _seed(tmp_path)
    envelope = consistency_scan(tmp_path)

    assert envelope["status"] == STATUS_CLEAN
    assert envelope["conflicts"] == []
    assert envelope["files_scanned"] == 3


def test_prepared_group_is_pending(tmp_path: Path):
    targets = _seed(tmp_path)
    cs_id = _prepared(tmp_path, targets)

    envelope = consistency_scan(tmp_path)

    assert envelope["status"] == STATUS_PENDING
    summary = envelope["changesets"][0]
    assert summary["id"] == cs_id
    assert summary["terminal"] is False
    assert summary["interrupted"] is False
    assert set(summary["relatives"]) == {TASKS_MD, PEOPLE_MD}
    assert TASK in summary["task_ids"]


def test_all_new_bytes_without_commit_record_is_interrupted_pending(tmp_path: Path):
    targets = _seed(tmp_path)
    cs_id = _prepared(tmp_path, targets)
    store = ChangeSetStore(tmp_path)
    change = store.load(cs_id)
    # Simulate a crash after the last apply but before any commit record:
    # publish staged bytes live and mark the group committing.
    for index, intent in enumerate(change.intents):
        (tmp_path / intent.relative).write_bytes(store.read_staged(cs_id, index))
    store.append(cs_id, EV_COMMITTING, {"tx_id": f"conv-{cs_id}"})

    envelope = consistency_scan(tmp_path)

    assert envelope["status"] == STATUS_PENDING
    assert envelope["conflicts"] == []
    assert envelope["changesets"][0]["interrupted"] is True


def test_partial_fan_out_is_conflict(tmp_path: Path):
    targets = _seed(tmp_path)
    cs_id = _prepared(tmp_path, targets)
    store = ChangeSetStore(tmp_path)
    change = store.load(cs_id)
    # Only the first target gets its staged bytes — the half-fan-out window.
    first = change.intents[0]
    (tmp_path / first.relative).write_bytes(store.read_staged(cs_id, 0))

    envelope = consistency_scan(tmp_path)

    assert envelope["status"] == STATUS_CONFLICT
    conflict = next(
        c for c in envelope["conflicts"] if c["kind"] == "fingerprint-drift"
    )
    assert conflict["changeset_id"] == cs_id
    assert conflict["detail"]["applied"] == [first.relative]
    assert conflict["detail"]["not_applied"] == [change.intents[1].relative]


def test_live_edit_on_pending_group_is_fingerprint_drift(tmp_path: Path):
    targets = _seed(tmp_path)
    cs_id = _prepared(tmp_path, targets)
    (tmp_path / PEOPLE_MD).write_text("# Jane\nuser changed this\n", encoding="utf-8")

    envelope = consistency_scan(tmp_path)

    assert envelope["status"] == STATUS_CONFLICT
    conflict = next(
        c for c in envelope["conflicts"] if c["kind"] == "fingerprint-drift"
    )
    assert conflict["locations"] == [PEOPLE_MD]


def test_mixed_checkbox_state_across_files_is_conflict(tmp_path: Path):
    _seed(tmp_path)
    # Status fan-out completed the task in Tasks but never reached Jane.
    (tmp_path / TASKS_MD).write_text(
        f"- [x] Call Jane ^{TASK}\n", encoding="utf-8"
    )
    (tmp_path / PEOPLE_MD).write_text(
        f"- [ ] Linked: ^{TASK}\n", encoding="utf-8"
    )

    envelope = consistency_scan(tmp_path)

    assert envelope["status"] == STATUS_CONFLICT
    conflict = next(
        c for c in envelope["conflicts"] if c["kind"] == "mixed-task-state"
    )
    assert conflict["task_id"] == TASK
    assert set(conflict["locations"]) == {TASKS_MD, PEOPLE_MD}
    assert conflict["detail"]["completed_in"] == [TASKS_MD]
    assert conflict["detail"]["open_in"] == [PEOPLE_MD]


def test_task_set_drift_against_pending_baseline(tmp_path: Path):
    targets = _seed(tmp_path)
    cs_id = _prepared(tmp_path, targets)
    # The anchor disappears from the person page after staging.
    (tmp_path / PEOPLE_MD).write_text("# Jane\nno anchor now\n", encoding="utf-8")

    envelope = consistency_scan(tmp_path)

    drift = next(
        c
        for c in envelope["conflicts"]
        if c["kind"] == "task-occurrence-set-drift"
    )
    assert drift["changeset_id"] == cs_id
    assert drift["task_id"] == TASK
    assert drift["locations"] == [PEOPLE_MD]
    assert envelope["status"] == STATUS_CONFLICT


def test_task_count_drift_against_pending_baseline(tmp_path: Path):
    targets = _seed(tmp_path)
    cs_id = _prepared(tmp_path, targets)
    # Same file set, but the anchor now appears twice on the person page.
    (tmp_path / PEOPLE_MD).write_text(
        f"# Jane\n^{TASK}\nagain ^{TASK}\n", encoding="utf-8"
    )

    envelope = consistency_scan(tmp_path)

    drift = next(
        c
        for c in envelope["conflicts"]
        if c["kind"] == "task-occurrence-count-drift"
    )
    assert drift["task_id"] == TASK
    assert drift["locations"] == [PEOPLE_MD]
    assert drift["detail"]["before_count"] == 1
    assert drift["detail"]["live_count"] == 2


def test_unreadable_changeset_log_is_conflict(tmp_path: Path):
    targets = _seed(tmp_path)
    cs_id = _prepared(tmp_path, targets)
    events = ChangeSetStore(tmp_path).journal_for(cs_id).path
    raw = events.read_text(encoding="utf-8")
    first, rest = raw.split("\n", 1)
    events.write_text(first.replace("PREPARING", "PREPAXING") + "\n" + rest)

    envelope = consistency_scan(tmp_path)

    assert envelope["status"] == STATUS_CONFLICT
    conflict = next(
        c for c in envelope["conflicts"] if c["kind"] == "interrupted-changeset"
    )
    assert conflict["changeset_id"] == cs_id
    assert conflict["detail"]["unreadable_event_log"] is True


def test_overlap_queries_and_fanout_gate(tmp_path: Path):
    targets = _seed(tmp_path)
    cs_id = _prepared(tmp_path, targets)
    envelope = consistency_scan(tmp_path)

    assert pending_overlap(envelope, task_ids=[TASK]) == [cs_id]
    assert pending_overlap(envelope, relatives=[PEOPLE_MD]) == [cs_id]
    assert pending_overlap(envelope, task_ids=["task-19990101-999"]) == []
    assert pending_overlap(envelope, relatives=["06-Resources/Other.md"]) == []

    blocked = fanout_gate(tmp_path, task_ids=[TASK])
    assert blocked["allowed"] is False
    assert blocked["reason"] == "pending-changeset-overlap"
    assert blocked["overlapping_changesets"] == [cs_id]

    # A clean vault (commit the group first) lets the fan-out proceed.
    coordinator.commit(tmp_path, cs_id)
    assert fanout_gate(tmp_path, task_ids=[TASK])["allowed"] is True

    # A bare mixed checkbox state is the ordinary pre-sync state the Obsidian
    # daemon exists to converge: it stays visible in the envelope but does
    # NOT gate automatic fan-out (otherwise checkbox sync would dead-lock).
    (tmp_path / TASKS_MD).write_text("- [x] z ^task-20260118-009\n")
    (tmp_path / LAUNCH_MD).write_text("- [ ] z ^task-20260118-009\n")
    bare_mixed = fanout_gate(tmp_path, relatives=["unrelated.md"])
    assert bare_mixed["envelope"]["status"] == STATUS_CONFLICT
    assert bare_mixed["allowed"] is True

    # A change-set-attributable conflict blocks even without direct overlap.
    targets = _seed(tmp_path)
    cs2 = _prepared(tmp_path, targets)
    store = ChangeSetStore(tmp_path)
    change = store.load(cs2)
    (tmp_path / change.intents[0].relative).write_bytes(
        store.read_staged(cs2, 0)
    )  # publish only one target: real partial fan-out
    gate = fanout_gate(tmp_path, relatives=["unrelated.md"])
    assert gate["allowed"] is False
    assert gate["reason"] == "vault-conflict"
    assert gate["blocking_conflicts"][0]["changeset_id"] == cs2


def test_unchanged_target_with_task_id_is_not_false_conflict(tmp_path: Path):
    """Regression Review R1 / Issue I-1.

    A prepare target whose bytes are unchanged (here: the anchor is already
    wikilinked) still belongs to the occurrence-invariant universe. A healthy
    prepared group must read as ``pending`` — never as task-occurrence-set-drift
    with the unchanged file reported missing.
    """
    targets = _seed(tmp_path)
    (tmp_path / PEOPLE_MD).write_text(
        f"# Jane\nLinked: [[^{TASK}]]\n", encoding="utf-8"
    )

    outcome = coordinator.prepare(tmp_path, targets, _converter)
    assert outcome.status == "prepared", outcome.reason
    assert outcome.files_changed == 1  # only Tasks.md changes
    cs_id = outcome.changeset_id

    envelope = consistency_scan(tmp_path)
    assert envelope["status"] == STATUS_PENDING
    assert envelope["conflicts"] == []
    summary = envelope["changesets"][0]
    assert summary["relatives"] == [TASKS_MD]
    assert set(summary["occurrence_relatives"]) == {TASKS_MD, PEOPLE_MD}

    # Unrelated fan-out is allowed while the healthy group is pending; the
    # group's own task and its guarded files still overlap.
    unrelated = fanout_gate(
        tmp_path,
        task_ids=["task-19990101-999"],
        relatives=["06-Resources/Other.md"],
    )
    assert unrelated["allowed"] is True
    assert unrelated["reason"] is None
    assert pending_overlap(envelope, relatives=[PEOPLE_MD]) == [cs_id]
    own = fanout_gate(tmp_path, task_ids=[TASK])
    assert own["allowed"] is False
    assert own["reason"] == "pending-changeset-overlap"

    coordinator.commit(tmp_path, cs_id)
    assert consistency_scan(tmp_path)["status"] == STATUS_CLEAN


def test_occurrence_drift_in_unchanged_scope_file_still_gates(tmp_path: Path):
    """Issue I-1 companion: widening the slice universe must keep real drift
    in an unchanged target detectable and gated (it previously escaped the
    intent-derived slice entirely)."""
    targets = _seed(tmp_path)
    (tmp_path / PEOPLE_MD).write_text(
        f"# Jane\nLinked: [[^{TASK}]]\n", encoding="utf-8"
    )
    cs_id = _prepared(tmp_path, targets)

    # The anchor disappears from the byte-unchanged scope file post-prepare.
    (tmp_path / PEOPLE_MD).write_text("# Jane\nno anchor now\n", encoding="utf-8")

    envelope = consistency_scan(tmp_path)
    assert envelope["status"] == STATUS_CONFLICT
    drift = next(
        c
        for c in envelope["conflicts"]
        if c["kind"] == "task-occurrence-set-drift"
    )
    assert drift["changeset_id"] == cs_id
    assert drift["task_id"] == TASK

    gate = fanout_gate(
        tmp_path,
        task_ids=["task-19990101-999"],
        relatives=["06-Resources/Other.md"],
    )
    assert gate["allowed"] is False
    assert gate["reason"] == "vault-conflict"


def test_envelope_is_json_safe_and_free_of_content_and_absolute_paths(tmp_path: Path):
    targets = _seed(tmp_path)
    cs_id = _prepared(tmp_path, targets)
    store = ChangeSetStore(tmp_path)
    (tmp_path / TASKS_MD).write_text("- [x] z ^task-20260118-009\n")
    (tmp_path / PEOPLE_MD).write_text("- [ ] z ^task-20260118-009\n")

    encoded = json.dumps(consistency_scan(tmp_path))
    decoded = json.loads(encoded)

    assert decoded["status"] == STATUS_CONFLICT
    assert str(tmp_path) not in encoded  # no absolute path
    assert "Call Jane" not in encoded    # no file body
    assert cs_id in encoded
