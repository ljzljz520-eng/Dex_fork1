"""FileIntent / ChangeSet model serialization and summary shape."""

from __future__ import annotations

import pytest

from core.changesets.model import (
    STATE_ABORTED,
    STATE_COMMITTED,
    STATE_PREPARED,
    TERMINAL_STATES,
    ChangeSet,
    FileIntent,
    lifecycle_rank,
)


def test_file_intent_roundtrip():
    intent = FileIntent(
        relative="05-Areas/People/Jane.md",
        old_sha256="a" * 64,
        new_sha256="b" * 64,
        size=42,
        mode=0o644,
    )

    restored = FileIntent.from_dict(intent.to_dict())

    assert restored == intent


def test_file_intent_defaults_mode():
    restored = FileIntent.from_dict(
        {
            "relative": "x.md",
            "old_sha256": "a" * 64,
            "new_sha256": "b" * 64,
            "size": 1,
        }
    )
    assert restored.mode == 0o644


@pytest.mark.parametrize(
    "data",
    [
        "not-an-object",
        {"relative": "x.md"},  # missing fields
        {"relative": 5, "old_sha256": "a", "new_sha256": "b", "size": 1},
        {"relative": "x.md", "old_sha256": "a", "new_sha256": "b", "size": -3},
        {"relative": "x.md", "old_sha256": "a", "new_sha256": "b", "size": 1, "mode": "777"},
    ],
)
def test_file_intent_rejects_bad_payloads(data):
    with pytest.raises(ValueError):
        FileIntent.from_dict(data)


def test_changeset_summary_is_path_and_id_only():
    change = ChangeSet(
        id="cs-20260918-abc123",
        state=STATE_PREPARED,
        intents=[
            FileIntent("a.md", "a" * 64, "b" * 64, 10),
            FileIntent("b.md", "c" * 64, "d" * 64, 20),
        ],
    )

    summary = change.summary()

    assert summary == {
        "id": "cs-20260918-abc123",
        "state": "prepared",
        "terminal": False,
        "operation": "content-conversion",
        "created_at": None,
        "files_total": 2,
        "tx_id": None,
        "failure": None,
    }
    assert change.relatives == ["a.md", "b.md"]
    assert change.intent_for("b.md").new_sha256 == "d" * 64
    assert change.intent_for("missing.md") is None


def test_terminal_states_and_lifecycle_rank():
    assert STATE_COMMITTED in TERMINAL_STATES
    assert STATE_ABORTED in TERMINAL_STATES
    assert STATE_PREPARED not in TERMINAL_STATES
    assert lifecycle_rank("preparing") < lifecycle_rank("prepared") < lifecycle_rank(
        "committing"
    ) < lifecycle_rank("committed")
    assert lifecycle_rank("aborted") == -1
