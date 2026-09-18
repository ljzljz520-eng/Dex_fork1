"""Durable pending log and private staging for change sets.

On-disk layout under ``System/.dex/changesets/`` (runtime class, never
shipped)::

    <cs_id>/
        events.jsonl      # append-only, fsynced, hash-chained (core.transaction.journal)
        staged/000000.bin  # new bytes, 0600, fsynced; deleted at terminal state

The journal is the single source of truth — every state transition is
appended (and fsynced) BEFORE the act it describes, exactly like the
transaction journal. State is derived, never stored as a mutable assertion.
"""

from __future__ import annotations

import os
import re
import shutil
import time
import uuid
from pathlib import Path

from core.changesets.fingerprints import sha256_bytes
from core.changesets.model import (
    CONTENT_CONVERSION_OPERATION,
    STATE_ABORTED,
    STATE_COMMITTED,
    STATE_COMMITTING,
    STATE_PREPARE_FAILED,
    STATE_PREPARED,
    STATE_PREPARING,
    STATE_REVERTED,
    ChangeSet,
    FileIntent,
    OccurrenceMap,
)
from core.path_safety import unsafe_existing_parent
from core.transaction.fsync import fsync_directory
from core.transaction.journal import Journal, JournalCorruptError

CHANGESETS_RELATIVE = Path("System") / ".dex" / "changesets"
EVENTS_NAME = "events.jsonl"
STAGED_NAME = "staged"

#: Lifecycle events. Each maps 1:1 to a state except COMMIT-FAILED, which
#: annotates a COMMITTING group whose transaction failed/rolled back — the
#: group stays resume-or-rollback pending with a recorded reason.
EV_PREPARING = "PREPARING"
EV_PREPARED = "PREPARED"
EV_COMMITTING = "COMMITTING"
EV_COMMITTED = "COMMITTED"
EV_ABORTED = "ABORTED"
EV_REVERTED = "REVERTED"
EV_PREPARE_FAILED = "PREPARE-FAILED"
EV_COMMIT_FAILED = "COMMIT-FAILED"

_SUPPLIED_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ChangeSetStorageError(RuntimeError):
    """A change set cannot be read or written safely."""


class ChangeSetNotFound(ChangeSetStorageError):
    """The referenced change set id has no directory."""


def new_changeset_id() -> str:
    return time.strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8]


def valid_changeset_id(cs_id: str) -> bool:
    return (
        isinstance(cs_id, str)
        and bool(_SUPPLIED_ID_RE.fullmatch(cs_id))
        and not cs_id.startswith(".")
    )


def _coerce_occurrence_map(value: object) -> OccurrenceMap:
    if not isinstance(value, dict):
        raise ChangeSetStorageError("occurrence map must be an object")
    result: OccurrenceMap = {}
    for task_id, files in value.items():
        if (
            not isinstance(task_id, str)
            or not isinstance(files, dict)
            or not files
        ):
            raise ChangeSetStorageError(f"invalid occurrence map entry: {task_id!r}")
        coerced: dict[str, int] = {}
        for relative, count in files.items():
            if not isinstance(relative, str) or type(count) is not int or count < 0:
                raise ChangeSetStorageError(
                    f"invalid occurrence count for {task_id!r}: {relative!r}"
                )
            coerced[relative] = count
        result[task_id] = coerced
    return result


class ChangeSetStore:
    """Owns every byte under ``System/.dex/changesets/`` for one vault."""

    def __init__(self, vault_root: Path | str) -> None:
        self.vault_root = Path(vault_root).resolve()
        unsafe = unsafe_existing_parent(
            self.vault_root, CHANGESETS_RELATIVE.as_posix()
        )
        if unsafe is not None:
            raise ChangeSetStorageError(
                f"refusing unsafe changeset directory "
                f"{CHANGESETS_RELATIVE.as_posix()}: {unsafe}"
            )
        self.root = self.vault_root / CHANGESETS_RELATIVE

    # -- paths ---------------------------------------------------------------

    def _require_id(self, cs_id: str) -> None:
        if not valid_changeset_id(cs_id):
            raise ChangeSetStorageError(f"invalid change set id: {cs_id!r}")

    def dir_for(self, cs_id: str) -> Path:
        self._require_id(cs_id)
        return self.root / cs_id

    def journal_for(self, cs_id: str) -> Journal:
        return Journal(self.dir_for(cs_id) / EVENTS_NAME)

    def staged_dir_for(self, cs_id: str) -> Path:
        return self.dir_for(cs_id) / STAGED_NAME

    def staged_blob_path(self, cs_id: str, index: int) -> Path:
        return self.staged_dir_for(cs_id) / f"{index:06d}.bin"

    def list_ids(self) -> list[str]:
        """Every change set directory, including unreadable ones.

        Readers must never silently skip a directory they cannot parse — that
        is how a half-written group would vanish from view.
        """
        if not self.root.is_dir():
            return []
        return sorted(
            candidate.name
            for candidate in self.root.iterdir()
            if candidate.is_dir()
        )

    def exists(self, cs_id: str) -> bool:
        return self.dir_for(cs_id).is_dir()

    # -- write side ----------------------------------------------------------

    def create(
        self,
        cs_id: str | None = None,
        *,
        operation: str = CONTENT_CONVERSION_OPERATION,
    ) -> str:
        cs_id = cs_id or new_changeset_id()
        self._require_id(cs_id)
        cs_dir = self.dir_for(cs_id)
        if cs_dir.exists():
            raise ChangeSetStorageError(f"change set already exists: {cs_id}")
        unsafe = unsafe_existing_parent(
            self.vault_root,
            (CHANGESETS_RELATIVE / cs_id / EVENTS_NAME).as_posix(),
        )
        if unsafe is not None:
            raise ChangeSetStorageError(
                f"refusing unsafe change set path {cs_id}: {unsafe}"
            )
        # The first journal append creates the 0o700 directory fsynced.
        Journal(cs_dir / EVENTS_NAME).append(
            EV_PREPARING, {"operation": operation}
        )
        return cs_id

    def append(self, cs_id: str, event: str, payload: dict | None = None) -> None:
        if not self.exists(cs_id):
            raise ChangeSetNotFound(cs_id)
        self.journal_for(cs_id).append(event, payload or {})

    def stage_blob(self, cs_id: str, index: int, data: bytes) -> Path:
        staged_dir = self.staged_dir_for(cs_id)
        staged_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(staged_dir, 0o700)
        target = self.staged_blob_path(cs_id, index)
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(target, 0o600)
        fsync_directory(staged_dir)
        return target

    def read_staged(self, cs_id: str, index: int) -> bytes:
        return self.staged_blob_path(cs_id, index).read_bytes()

    def staged_indices(self, cs_id: str) -> list[int]:
        staged_dir = self.staged_dir_for(cs_id)
        if not staged_dir.is_dir():
            return []
        indices = []
        for blob in staged_dir.iterdir():
            match = re.fullmatch(r"(\d{6})\.bin", blob.name)
            if match and blob.is_file():
                indices.append(int(match.group(1)))
        return sorted(indices)

    def staged_is_intact(self, change: ChangeSet) -> bool:
        """Every staged blob exists with exactly its declared bytes."""
        for index, intent in enumerate(change.intents):
            blob = self.staged_blob_path(change.id, index)
            if not blob.is_file():
                return False
            data = blob.read_bytes()
            if len(data) != intent.size or sha256_bytes(data) != intent.new_sha256:
                return False
        return True

    def clear_staged(self, cs_id: str) -> None:
        staged_dir = self.staged_dir_for(cs_id)
        if staged_dir.is_dir():
            shutil.rmtree(staged_dir)
            fsync_directory(self.dir_for(cs_id))

    def remove(self, cs_id: str) -> None:
        """Delete a whole change set directory (prune / abort cleanup only)."""
        cs_dir = self.dir_for(cs_id)
        if cs_dir.is_dir():
            shutil.rmtree(cs_dir)
            if self.root.is_dir():
                fsync_directory(self.root)

    def prune(self, *, keep: int = 3) -> list[str]:
        """Delete terminal change sets beyond the newest ``keep``.

        Non-terminal and unreadable directories are ALWAYS retained — they
        represent work an operator may still need to resume.
        """
        terminal: list[tuple[str, ChangeSet]] = []
        for cs_id in self.list_ids():
            try:
                change = self.load(cs_id)
            except ChangeSetStorageError:
                continue  # unreadable: protected, never pruned
            if change.is_terminal:
                terminal.append((cs_id, change))
        terminal.sort(key=lambda item: item[0])
        stale = terminal[:-keep] if keep else terminal
        removed = [cs_id for cs_id, _change in stale]
        for cs_id in removed:
            self.remove(cs_id)
        return removed

    # -- read side -----------------------------------------------------------

    def load(self, cs_id: str) -> ChangeSet:
        self._require_id(cs_id)
        if not self.exists(cs_id):
            raise ChangeSetNotFound(cs_id)
        try:
            entries = self.journal_for(cs_id).read()
        except JournalCorruptError as error:
            raise ChangeSetStorageError(
                f"change set {cs_id} has an unreadable event log"
            ) from error

        change = ChangeSet(id=cs_id)
        failure_reason: str | None = None
        for entry in entries:
            payload = entry.payload
            if entry.event == EV_PREPARING:
                operation = payload.get("operation", CONTENT_CONVERSION_OPERATION)
                if isinstance(operation, str):
                    change.operation = operation
                change.state = STATE_PREPARING
            elif entry.event == EV_PREPARED:
                self._apply_prepared(change, payload)
            elif entry.event == EV_COMMITTING:
                change.state = STATE_COMMITTING
                tx_id = payload.get("tx_id")
                if isinstance(tx_id, str):
                    change.tx_id = tx_id
            elif entry.event == EV_COMMITTED:
                change.state = STATE_COMMITTED
                tx_id = payload.get("tx_id")
                if isinstance(tx_id, str):
                    change.tx_id = tx_id
            elif entry.event == EV_ABORTED:
                change.state = STATE_ABORTED
                reason = payload.get("reason")
                if isinstance(reason, str):
                    failure_reason = reason
            elif entry.event == EV_REVERTED:
                change.state = STATE_REVERTED
                tx_id = payload.get("tx_id")
                if isinstance(tx_id, str):
                    change.tx_id = tx_id
            elif entry.event == EV_PREPARE_FAILED:
                change.state = STATE_PREPARE_FAILED
                reason = payload.get("reason")
                if isinstance(reason, str):
                    failure_reason = reason
            elif entry.event == EV_COMMIT_FAILED:
                # Annotates COMMITTING; never a terminal claim on its own.
                reason = payload.get("reason")
                if isinstance(reason, str):
                    failure_reason = reason
        change.failure = failure_reason
        return change

    @staticmethod
    def _apply_prepared(change: ChangeSet, payload: dict) -> None:
        created_at = payload.get("created_at")
        intents_raw = payload.get("intents")
        if not isinstance(intents_raw, list) or not intents_raw:
            raise ChangeSetStorageError(
                f"change set {change.id} PREPARED payload has no intents"
            )
        intents = [FileIntent.from_dict(item) for item in intents_raw]
        relatives = [intent.relative for intent in intents]
        if len(relatives) != len(set(relatives)):
            raise ChangeSetStorageError(
                f"change set {change.id} lists a duplicate target"
            )
        change.intents = intents
        change.occurrences_before = _coerce_occurrence_map(
            payload.get("occurrences_before", {})
        )
        change.occurrences_after = _coerce_occurrence_map(
            payload.get("occurrences_after", {})
        )
        change.created_at = created_at if isinstance(created_at, str) else None
        operation = payload.get("operation")
        if isinstance(operation, str):
            change.operation = operation
        change.state = STATE_PREPARED
