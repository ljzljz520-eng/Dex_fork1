"""Durable pending log: event replay, staging integrity, crash tails, prune."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from core.changesets.fingerprints import sha256_bytes
from core.changesets.model import (
    STATE_ABORTED,
    STATE_COMMITTED,
    STATE_COMMITTING,
    STATE_PREPARED,
    STATE_PREPARING,
    FileIntent,
)
from core.changesets.storage import (
    EV_ABORTED,
    EV_COMMITTED,
    EV_COMMITTING,
    EV_PREPARED,
    ChangeSetStorageError,
    ChangeSetStore,
)


def _prepared_payload() -> dict:
    return {
        "created_at": "2026-09-18T10:00:00+00:00",
        "operation": "content-conversion",
        "intents": [
            FileIntent("a.md", "a" * 64, "b" * 64, 10).to_dict(),
            FileIntent("b.md", "c" * 64, "d" * 64, 20).to_dict(),
        ],
        "occurrences_before": {"task-20260918-001": {"a.md": 1}},
        "occurrences_after": {"task-20260918-001": {"a.md": 1}},
    }


@pytest.fixture
def store(tmp_path: Path) -> ChangeSetStore:
    return ChangeSetStore(tmp_path)


def test_state_is_derived_from_events_not_stored(store: ChangeSetStore):
    cs_id = store.create()

    assert store.load(cs_id).state == STATE_PREPARING

    store.append(cs_id, EV_PREPARED, _prepared_payload())
    prepared = store.load(cs_id)
    assert prepared.state == STATE_PREPARED
    assert prepared.relatives == ["a.md", "b.md"]
    assert prepared.occurrences_before == {"task-20260918-001": {"a.md": 1}}
    assert prepared.intents[1].new_sha256 == "d" * 64

    store.append(cs_id, EV_COMMITTING, {"tx_id": "conv-x"})
    assert store.load(cs_id).state == STATE_COMMITTING
    store.append(cs_id, EV_COMMITTED, {"tx_id": "conv-x"})
    committed = store.load(cs_id)
    assert committed.state == STATE_COMMITTED
    assert committed.tx_id == "conv-x"
    assert committed.is_terminal


def test_prepared_payload_rejects_duplicate_targets(store: ChangeSetStore):
    cs_id = store.create()
    payload = _prepared_payload()
    payload["intents"].append(payload["intents"][0])
    store.append(cs_id, EV_PREPARED, payload)

    with pytest.raises(ChangeSetStorageError):
        store.load(cs_id)


def test_unsafe_ids_are_refused(store: ChangeSetStore):
    for bad in ("../escape", "a/b", "..", ".hidden", "", "x" * 80):
        with pytest.raises(ChangeSetStorageError):
            store.dir_for(bad)


def test_staged_blobs_are_private_and_verified(store: ChangeSetStore):
    first_blob = b"\x00" * 10
    second_blob = b"\x01" * 20
    payload = {
        "created_at": "2026-09-18T10:00:00+00:00",
        "operation": "content-conversion",
        "intents": [
            FileIntent("a.md", "a" * 64, sha256_bytes(first_blob), 10).to_dict(),
            FileIntent("b.md", "c" * 64, sha256_bytes(second_blob), 20).to_dict(),
        ],
        "occurrences_before": {"task-20260918-001": {"a.md": 1}},
        "occurrences_after": {"task-20260918-001": {"a.md": 1}},
    }
    cs_id = store.create()
    store.append(cs_id, EV_PREPARED, payload)
    change = store.load(cs_id)

    store.stage_blob(cs_id, 0, first_blob)
    store.stage_blob(cs_id, 1, second_blob)

    mode = stat.S_IMODE(os.stat(store.staged_blob_path(cs_id, 0)).st_mode)
    assert mode == 0o600
    assert store.staged_indices(cs_id) == [0, 1]
    assert store.read_staged(cs_id, 1) == second_blob
    assert store.staged_is_intact(change)

    # A torn or tampered blob breaks the declared fingerprint.
    store.staged_blob_path(cs_id, 0).write_bytes(b"\x00" * 9 + b"\xff")
    assert store.staged_is_intact(change) is False

    store.clear_staged(cs_id)
    assert store.staged_indices(cs_id) == []


def test_torn_event_tail_is_truncated_on_next_append(store: ChangeSetStore):
    cs_id = store.create()
    events = store.journal_for(cs_id).path
    # Simulate a crash mid-append: junk bytes after the last newline.
    with open(events, "ab") as handle:
        handle.write(b'{"schema_version": 2, "event": "ZZZ_TORN_TAIL_ZZZ",')

    # The torn line is ignored on read...
    assert store.load(cs_id).state == STATE_PREPARING
    # ...and removed when the journal accepts a new event.
    store.append(cs_id, EV_ABORTED, {"reason": "operator cancelled"})
    reloaded = store.load(cs_id)
    assert reloaded.state == STATE_ABORTED
    assert reloaded.failure == "operator cancelled"
    assert b"ZZZ_TORN_TAIL_ZZZ" not in events.read_bytes()


def test_interior_journal_damage_fails_closed(store: ChangeSetStore):
    cs_id = store.create()
    store.append(cs_id, EV_PREPARED, _prepared_payload())
    events = store.journal_for(cs_id).path

    raw = events.read_text(encoding="utf-8")
    first_line, rest = raw.split("\n", 1)
    # Damage the FIRST line's body while leaving line structure intact —
    # this cannot be a torn tail, so the log must not be trusted.
    damaged = first_line.replace("PREPARING", "PREPAXING") + "\n" + rest
    events.write_text(damaged, encoding="utf-8")

    with pytest.raises(ChangeSetStorageError):
        store.load(cs_id)
    # It still shows up for readers/pruning — never silently disappears.
    assert store.list_ids() == [cs_id]
    assert store.prune(keep=0) == []


def test_prune_keeps_newest_terminal_and_protects_live_work(
    store: ChangeSetStore,
):
    ids = [f"20260101T00000{i}-aaaa000{i}" for i in range(1, 6)]
    for cs_id in ids:
        store.create(cs_id=cs_id)
        store.append(cs_id, EV_PREPARED, _prepared_payload())
        store.append(cs_id, EV_COMMITTED, {"tx_id": f"tx-{cs_id}"})

    # A prepared group is live work and must survive pruning.
    live_id = "20260101T000010-bbbb0001"
    store.create(cs_id=live_id)
    store.append(live_id, EV_PREPARED, _prepared_payload())

    removed = store.prune(keep=3)

    assert removed == ids[:2]
    survivors = set(store.list_ids())
    assert survivors == {*ids[2:], live_id}
    assert store.load(live_id).state == STATE_PREPARED
