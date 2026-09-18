"""Two-phase change-set protocol for user-approved batch Markdown conversion.

Layered on top of :mod:`core.transaction` (the one safe mutation door):

- prepare converts every target in memory, stages the new bytes privately,
  and validates per-file fingerprints plus the task-ID fan-out invariant;
- commit applies the whole group through a ``content-conversion``
  Transaction under the vault mutation lock;
- a durable pending event log lets an explicit resume finish the group and an
  explicit rollback undo it after an interruption — never silently.

Readers get an explicit clean/pending/conflict answer from
:mod:`core.changesets.consistency` instead of accepting partial fan-out.
"""

from core.changesets.model import (
    CONTENT_CONVERSION_OPERATION,
    STATE_ABORTED,
    STATE_COMMITTED,
    STATE_COMMITTING,
    STATE_PREPARE_FAILED,
    STATE_PREPARED,
    STATE_PREPARING,
    STATE_REVERTED,
    TERMINAL_STATES,
    ChangeSet,
    FileIntent,
)

__all__ = [
    "CONTENT_CONVERSION_OPERATION",
    "STATE_ABORTED",
    "STATE_COMMITTED",
    "STATE_COMMITTING",
    "STATE_PREPARE_FAILED",
    "STATE_PREPARED",
    "STATE_PREPARING",
    "STATE_REVERTED",
    "TERMINAL_STATES",
    "ChangeSet",
    "FileIntent",
]
