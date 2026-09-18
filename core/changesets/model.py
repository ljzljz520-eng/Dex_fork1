"""Typed model for one two-phase change set.

A change set is a group of file intents that must appear all-or-nothing.
Each :class:`FileIntent` pins both ends of the replacement: the sha256 the
file must still have at apply time and the sha256 of the staged new bytes.
State itself is never asserted here — it is derived by the storage layer from
the append-only event log (see :mod:`core.changesets.storage`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Ownership-contract operation that authorizes user-approved conversion of
#: vault/seed markdown through the transaction core.
CONTENT_CONVERSION_OPERATION = "content-conversion"

STATE_PREPARING = "preparing"
STATE_PREPARED = "prepared"
STATE_COMMITTING = "committing"
STATE_COMMITTED = "committed"
STATE_ABORTED = "aborted"
STATE_REVERTED = "reverted"
#: Prepare finished without producing a valid group (failed validation); the
#: staging was discarded and no vault byte changed. Terminal for retention,
#: surfaced to readers as a failed attempt rather than a live conflict.
STATE_PREPARE_FAILED = "prepare-failed"

#: States from which no further transition exists.
TERMINAL_STATES = frozenset(
    {STATE_COMMITTED, STATE_ABORTED, STATE_REVERTED, STATE_PREPARE_FAILED}
)

#: Ordered lifecycle, used to validate event sequences.
_LIFECYCLE = (
    STATE_PREPARING,
    STATE_PREPARED,
    STATE_COMMITTING,
    STATE_COMMITTED,
)


@dataclass(frozen=True)
class FileIntent:
    """One intended whole-file replacement.

    ``old_sha256`` is the optimistic precondition pinned at prepare time and
    re-checked under the mutation lock by the transaction. ``new_sha256`` is
    the sha256 of the staged replacement bytes.
    """

    relative: str
    old_sha256: str
    new_sha256: str
    size: int
    mode: int = 0o644

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative": self.relative,
            "old_sha256": self.old_sha256,
            "new_sha256": self.new_sha256,
            "size": self.size,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "FileIntent":
        if not isinstance(data, dict):
            raise ValueError("a file intent must be an object")
        try:
            relative = data["relative"]
            old_sha256 = data["old_sha256"]
            new_sha256 = data["new_sha256"]
            size = data["size"]
        except KeyError as error:
            raise ValueError(f"file intent is missing {error}") from error
        mode = data.get("mode", 0o644)
        if (
            not isinstance(relative, str)
            or not relative
            or not isinstance(old_sha256, str)
            or not isinstance(new_sha256, str)
            or type(size) is not int
            or size < 0
            or type(mode) is not int
        ):
            raise ValueError(f"invalid file intent: {relative!r}")
        return cls(
            relative=relative,
            old_sha256=old_sha256,
            new_sha256=new_sha256,
            size=size,
            mode=mode,
        )


OccurrenceMap = dict[str, dict[str, int]]
"""task_id -> {vault-relative path -> occurrence count}."""


@dataclass
class ChangeSet:
    """In-memory view of one change set reconstructed from its event log."""

    id: str
    state: str = STATE_PREPARING
    operation: str = CONTENT_CONVERSION_OPERATION
    created_at: str | None = None
    intents: list[FileIntent] = field(default_factory=list)
    occurrences_before: OccurrenceMap = field(default_factory=dict)
    occurrences_after: OccurrenceMap = field(default_factory=dict)
    tx_id: str | None = None
    failure: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def relatives(self) -> list[str]:
        return [intent.relative for intent in self.intents]

    @property
    def occurrence_relatives(self) -> list[str]:
        """Files participating in the task-ID occurrence invariant.

        Prepare builds the before/after occurrence maps for **every** target,
        including files whose converted bytes stay identical, so read-side
        reconciliation must slice live content over this universe — never over
        :attr:`relatives`, which only lists files that actually change.
        """
        files: set[str] = set()
        for file_counts in tuple(self.occurrences_before.values()) + tuple(
            self.occurrences_after.values()
        ):
            files.update(file_counts)
        return sorted(files)

    def intent_for(self, relative: str) -> FileIntent | None:
        for intent in self.intents:
            if intent.relative == relative:
                return intent
        return None

    def summary(self) -> dict[str, Any]:
        """Reader-facing, PII-safe summary (paths/ids/fingerprints only)."""
        return {
            "id": self.id,
            "state": self.state,
            "terminal": self.is_terminal,
            "operation": self.operation,
            "created_at": self.created_at,
            "files_total": len(self.intents),
            "tx_id": self.tx_id,
            "failure": self.failure,
        }


def lifecycle_rank(state: str) -> int:
    """Numeric position in the forward lifecycle, or -1 for terminal sides."""
    try:
        return _LIFECYCLE.index(state)
    except ValueError:
        return -1
