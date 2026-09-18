"""Two-phase prepare/commit coordination on top of the transaction core.

The coordinator never writes vault content itself. Prepare is lock-free and
touches only the private runtime staging area; commit builds one
``content-conversion`` :class:`~core.transaction.engine.Transaction` that
replaces the whole file group atomically under the vault mutation lock.

Crash seams (NFR-5): ``DEX_CHANGESET_STOP_AFTER`` accepts
``after-stage:<index>`` and ``after-prepared``; the commit phase additionally
honours the transaction engine's own ``DEX_TX_TEST_STOP_AFTER`` points.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core import portable_contract
from core.changesets.fingerprints import (
    diff_task_maps,
    extract_task_occurrences_bytes,
    merge_file_occurrences,
    sha256_bytes,
)
from core.changesets.model import (
    CONTENT_CONVERSION_OPERATION,
    ChangeSet,
    FileIntent,
    OccurrenceMap,
)
from core.changesets.storage import (
    EV_ABORTED,
    EV_COMMIT_FAILED,
    EV_COMMITTED,
    EV_COMMITTING,
    EV_PREPARED,
    EV_PREPARE_FAILED,
    EV_REVERTED,
    ChangeSetStorageError,
    ChangeSetStore,
)
from core.path_safety import unsafe_existing_parent
from core.transaction.engine import (
    PlanEntry,
    PlanRejected,
    Transaction,
    TransactionError,
    TX_ROOT_RELATIVE,
)
from core.transaction.journal import Journal, JournalCorruptError
from core.transaction.lock import LockBusyError
from core.transaction.snapshot import Snapshot

#: Deterministic pure bytes-in/bytes-out conversion. Must not perform IO.
Converter = Callable[[bytes], bytes]

#: Prepare outcomes that produced no committable group.
REJECTED = "rejected"
PREPARED_STATUS = "prepared"
NOOP = "noop"

#: Structured rejection reasons (machine-readable; paths/ids only).
REASON_PENDING_GROUP = "pending-changeset-exists"
REASON_UNSAFE_PATH = "unsafe-path"
REASON_NOT_MARKDOWN = "not-markdown"
REASON_SYMLINK = "symlink-target"
REASON_NOT_REGULAR = "not-regular-file"
REASON_NOT_UTF8 = "not-utf-8"
REASON_CONTRACT_FORBIDS = "contract-forbids"
REASON_CONVERTER_FAILED = "converter-failed"
REASON_LIVE_DRIFT = "live-fingerprint-drift"
REASON_STAGED_DAMAGED = "staged-content-damaged"
REASON_TASK_INVARIANT = "task-occurrence-invariant"
REASON_NOT_READY = "changeset-not-ready"
REASON_NOT_COMMITTED = "changeset-not-committed"
REASON_SNAPSHOT_MISSING = "rollback-snapshot-unavailable"
REASON_POST_COMMIT_DRIFT = "post-commit-drift"

#: Plain-language recovery guidance kept next to the structured reason.
GIT_RECOVERY_HINT = (
    "No automatic undo is available for this change set. Before the conversion "
    "ran, a git backup commit was created in the vault; restore from it with "
    "the printed 'git restore --source=<backup commit>' command."
)


class ChangeSetCoordinatorError(RuntimeError):
    """Base class for coordinator failures."""


class ChangeSetConflict(ChangeSetCoordinatorError):
    """Live state no longer matches the recorded intent; no write is safe."""

    def __init__(self, reason: str, detail: dict | None = None) -> None:
        self.reason = reason
        self.detail = detail or {}
        super().__init__(reason)


class RollbackUnavailable(ChangeSetCoordinatorError):
    """A committed group cannot be undone automatically."""

    def __init__(self, reason: str, detail: dict | None = None) -> None:
        self.reason = reason
        self.detail = detail or {}
        super().__init__(reason)


@dataclass
class PrepareOutcome:
    status: str
    changeset_id: str | None = None
    files_scanned: int = 0
    files_changed: int = 0
    reason: str | None = None
    detail: dict = field(default_factory=dict)


def _stop_seam(label: str) -> None:
    if os.environ.get("DEX_CHANGESET_STOP_AFTER") == label:
        os._exit(137)


def pending_changesets(store: ChangeSetStore) -> list[str]:
    """Non-terminal change set ids (including unreadable ones), sorted."""
    pending: list[str] = []
    for cs_id in store.list_ids():
        try:
            change = store.load(cs_id)
        except ChangeSetStorageError:
            pending.append(cs_id)  # unreadable is, fail-safe, still pending
            continue
        if not change.is_terminal:
            pending.append(cs_id)
    return pending


def _authorize_target(root: Path, relative: str) -> str | None:
    """Return a rejection reason for one conversion target, else None."""
    target = root / relative
    unsafe = unsafe_existing_parent(root, relative)
    if unsafe is not None:
        return REASON_UNSAFE_PATH
    if target.is_symlink():
        return REASON_SYMLINK
    if not target.is_file():
        return REASON_NOT_REGULAR
    if not relative.endswith(".md"):
        return REASON_NOT_MARKDOWN
    verdict = portable_contract.update_write_verdict(
        relative, exists=True, operation=CONTENT_CONVERSION_OPERATION
    )
    if not verdict.allowed:
        return REASON_CONTRACT_FORBIDS
    return None


def prepare(
    vault_root: Path | str,
    targets: list[str],
    converter: Converter,
    *,
    operation: str = CONTENT_CONVERSION_OPERATION,
) -> PrepareOutcome:
    """Stage every conversion and validate the whole group. No lock, no vault
    content writes. Returns either a PREPARED group id or a structured
    rejection; a failed group is recorded PREPARE-FAILED with staging removed.
    """
    root = Path(vault_root).resolve()
    store = ChangeSetStore(root)

    # Never open a second group while work is unfinished: the operator must
    # explicitly resume or roll the pending one back first.
    pending = pending_changesets(store)
    if pending:
        return PrepareOutcome(
            REJECTED,
            reason=REASON_PENDING_GROUP,
            detail={"pending_changesets": pending},
        )

    relatives = sorted(dict.fromkeys(targets))
    if not relatives:
        return PrepareOutcome(NOOP, files_scanned=0, files_changed=0)

    cs_id = store.create(operation=operation)
    intents: list[FileIntent] = []
    before_map: OccurrenceMap = {}
    after_map: OccurrenceMap = {}
    changed_relatives: list[str] = []

    def fail(reason: str, **detail) -> PrepareOutcome:
        store.clear_staged(cs_id)
        payload = {"reason": reason, **{k: v for k, v in detail.items() if v}}
        store.append(cs_id, EV_PREPARE_FAILED, payload)
        return PrepareOutcome(
            REJECTED,
            changeset_id=cs_id,
            files_scanned=len(relatives),
            files_changed=len(intents),
            reason=reason,
            detail=detail,
        )

    for index, relative in enumerate(relatives):
        reason = _authorize_target(root, relative)
        if reason is not None:
            return fail(reason, locations=[relative])

        target = root / relative
        old_bytes = target.read_bytes()
        try:
            merge_file_occurrences(
                before_map, relative, extract_task_occurrences_bytes(old_bytes)
            )
        except UnicodeDecodeError:
            return fail(REASON_NOT_UTF8, locations=[relative])

        old_digest = sha256_bytes(old_bytes)
        try:
            new_bytes = converter(old_bytes)
        except Exception as error:  # converter bug must never reach the vault
            return fail(
                REASON_CONVERTER_FAILED,
                locations=[relative],
                error=type(error).__name__,
            )
        if not isinstance(new_bytes, bytes) or not new_bytes:
            return fail(
                REASON_CONVERTER_FAILED,
                locations=[relative],
                error="converter returned empty or non-bytes output",
            )

        if new_bytes == old_bytes:
            # Unchanged file: identical bytes on both sides, identical counts.
            merge_file_occurrences(
                after_map, relative, extract_task_occurrences_bytes(old_bytes)
            )
            continue

        try:
            new_occurrences = extract_task_occurrences_bytes(new_bytes)
        except UnicodeDecodeError:
            return fail(REASON_NOT_UTF8, locations=[relative])
        merge_file_occurrences(after_map, relative, new_occurrences)

        intent = FileIntent(
            relative=relative,
            old_sha256=old_digest,
            new_sha256=sha256_bytes(new_bytes),
            size=len(new_bytes),
            mode=target.stat().st_mode & 0o777,
        )
        intents.append(intent)
        changed_relatives.append(relative)
        store.stage_blob(cs_id, len(intents) - 1, new_bytes)
        _stop_seam(f"after-stage:{index}")

    if not intents:
        # Nothing to apply: drop the empty shell completely (dry reruns leave
        # no ledger trace).
        store.remove(cs_id)
        return PrepareOutcome(
            NOOP, files_scanned=len(relatives), files_changed=0
        )

    violations = diff_task_maps(before_map, after_map)
    if violations:
        return fail(
            REASON_TASK_INVARIANT, violations=[v.to_dict() for v in violations]
        )

    # Optimistic live recheck: the vault is live, and another writer may have
    # changed a target between read and stage. The transaction repeats this
    # under the lock; failing here avoids committing work onto stale bytes.
    drifted = []
    for intent in intents:
        target = root / intent.relative
        if (
            target.is_symlink()
            or not target.is_file()
            or sha256_bytes(target.read_bytes()) != intent.old_sha256
        ):
            drifted.append(intent.relative)
    if drifted:
        return fail(REASON_LIVE_DRIFT, locations=drifted)

    created_at = _utc_now()
    store.append(
        cs_id,
        EV_PREPARED,
        {
            "created_at": created_at,
            "operation": operation,
            "intents": [intent.to_dict() for intent in intents],
            "occurrences_before": before_map,
            "occurrences_after": after_map,
        },
    )
    _stop_seam("after-prepared")

    prepared = store.load(cs_id)
    if not store.staged_is_intact(prepared):
        return fail(REASON_STAGED_DAMAGED, locations=prepared.relatives)

    return PrepareOutcome(
        PREPARED_STATUS,
        changeset_id=cs_id,
        files_scanned=len(relatives),
        files_changed=len(intents),
    )


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# -- commit -------------------------------------------------------------------


def _transaction_id_for(cs_id: str) -> str:
    return f"conv-{cs_id}"


def _live_matches(root: Path, intents, attr: str) -> list[str]:
    """Relatives whose current bytes do NOT match the named fingerprint."""
    mismatched: list[str] = []
    for intent in intents:
        target = root / intent.relative
        if target.is_symlink() or not target.is_file():
            mismatched.append(intent.relative)
            continue
        if sha256_bytes(target.read_bytes()) != getattr(intent, attr):
            mismatched.append(intent.relative)
    return mismatched


def _plan_from_staged(
    root: Path, store: ChangeSetStore, change: ChangeSet
) -> list[PlanEntry]:
    plan: list[PlanEntry] = []
    for index, intent in enumerate(change.intents):
        data = store.read_staged(change.id, index)
        if len(data) != intent.size or sha256_bytes(data) != intent.new_sha256:
            raise ChangeSetConflict(
                REASON_STAGED_DAMAGED,
                {"changeset_id": change.id, "locations": [intent.relative]},
            )
        plan.append(
            PlanEntry(
                relative=intent.relative,
                content=data,
                mode=intent.mode,
                expected_current_sha256=intent.old_sha256,
            )
        )
    return plan


def commit(
    vault_root: Path | str,
    cs_id: str,
    *,
    during_commit: Callable[[], None] | None = None,
) -> dict:
    """Atomically apply a PREPARED group through one content-conversion
    transaction. The transaction owns the vault lock; the coordinator only
    journals intent/result around it.
    """
    root = Path(vault_root).resolve()
    store = ChangeSetStore(root)
    change = store.load(cs_id)

    if change.state not in {"prepared", "committing"}:
        raise ChangeSetCoordinatorError(
            f"change set {cs_id} is {change.state}, not ready to commit"
        )
    if not change.intents:
        raise ChangeSetCoordinatorError(f"change set {cs_id} has no intents")
    if not store.staged_is_intact(change):
        raise ChangeSetConflict(
            REASON_STAGED_DAMAGED,
            {"changeset_id": cs_id, "locations": change.relatives},
        )
    if _live_matches(root, change.intents, "old_sha256"):
        raise ChangeSetConflict(
            REASON_LIVE_DRIFT,
            {
                "changeset_id": cs_id,
                "locations": _live_matches(root, change.intents, "old_sha256"),
            },
        )

    tx_id = change.tx_id or _transaction_id_for(cs_id)
    plan = _plan_from_staged(root, store, change)

    # Intent before act: a crash after this point shows the group mid-commit.
    if change.tx_id is None:
        store.append(cs_id, EV_COMMITTING, {"tx_id": tx_id})
    try:
        transaction = Transaction._begin_with_id(
            root,
            plan,
            allow_empty=False,
            operation=CONTENT_CONVERSION_OPERATION,
            tx_id=tx_id,
        )
        result = transaction.run(before_commit=during_commit)
    except BaseException as error:
        # run() has already rolled the transaction back byte-for-byte. The
        # group stays explicitly pending; resume() may retry, rollback() cancels.
        reason = (
            "lock-busy"
            if isinstance(error, LockBusyError)
            else type(error).__name__
        )
        store.append(
            cs_id,
            EV_COMMIT_FAILED,
            {"tx_id": tx_id, "reason": reason},
        )
        raise ChangeSetConflict(
            "commit-failed",
            {"changeset_id": cs_id, "tx_id": tx_id, "reason": reason},
        ) from error

    # The transaction is fsync-committed before we claim the group is.
    store.append(cs_id, EV_COMMITTED, {"tx_id": result["tx_id"]})
    store.clear_staged(cs_id)
    store.prune(keep=3)
    return {
        "changeset_id": cs_id,
        "tx_id": result["tx_id"],
        "committed": True,
        "targets": change.relatives,
    }


# -- recovery -----------------------------------------------------------------


def _tx_journal_events(root: Path, tx_id: str) -> set[str] | None:
    """Event set for a related transaction, or None if it never began."""
    journal_path = root / TX_ROOT_RELATIVE / tx_id / "journal.jsonl"
    if not journal_path.exists():
        return None
    try:
        return {entry.event for entry in Journal(journal_path).read()}
    except JournalCorruptError:
        raise ChangeSetConflict(
            "transaction-log-unreadable", {"tx_id": tx_id}
        ) from None


def resume(vault_root: Path | str, cs_id: str) -> dict:
    """Explicitly converge one interrupted group. Never auto-commits — the
    caller (CLI/operator) must invoke this.
    """
    root = Path(vault_root).resolve()
    store = ChangeSetStore(root)
    change = store.load(cs_id)

    if change.state == "committed":
        return {"changeset_id": cs_id, "committed": True, "idempotent": True}
    if change.state in {"aborted", "reverted", "prepare-failed", "preparing"}:
        raise ChangeSetCoordinatorError(
            f"change set {cs_id} is {change.state}; nothing to resume"
        )

    events = _tx_journal_events(root, change.tx_id or _transaction_id_for(cs_id))

    if events is not None and "COMMITTED" in events:
        # Transaction finished; only the changeset result event was lost.
        # Repair the record only if live bytes truly match the new fingerprints.
        mismatched = _live_matches(root, change.intents, "new_sha256")
        if mismatched:
            raise ChangeSetConflict(
                REASON_POST_COMMIT_DRIFT,
                {"changeset_id": cs_id, "locations": mismatched},
            )
        store.append(
            cs_id, EV_COMMITTED, {"tx_id": change.tx_id, "repaired": True}
        )
        store.clear_staged(cs_id)
        store.prune(keep=3)
        return {"changeset_id": cs_id, "committed": True, "repaired": True}

    if events is not None:
        # A begin without COMMITTED: let the transaction core converge it
        # (its own policy is rollback-never-forward), then re-commit the
        # still-staged intent ourselves.
        Transaction.resume(root, transaction_ids=[change.tx_id])

    mismatched = _live_matches(root, change.intents, "old_sha256")
    if mismatched:
        raise ChangeSetConflict(
            REASON_LIVE_DRIFT,
            {"changeset_id": cs_id, "locations": mismatched},
        )
    if not store.staged_is_intact(change):
        raise ChangeSetConflict(
            REASON_STAGED_DAMAGED,
            {"changeset_id": cs_id, "locations": change.relatives},
        )

    return commit(root, cs_id)


def rollback(vault_root: Path | str, cs_id: str) -> dict:
    """Cancel before commit, or restore original bytes after commit using the
    transaction snapshot. Never guesses when the snapshot is gone.
    """
    root = Path(vault_root).resolve()
    store = ChangeSetStore(root)
    change = store.load(cs_id)

    if change.state in {"aborted", "reverted"}:
        return {
            "changeset_id": cs_id,
            "rolled_back": True,
            "idempotent": True,
            "state": change.state,
        }

    if change.state in {"prepared", "committing", "preparing", "prepare-failed"}:
        # Pre-commit: the vault was never changed (a failed transaction
        # already self-rolled-back). Discard staging and mark aborted.
        if change.state == "committing" and change.tx_id is not None:
            events = _tx_journal_events(root, change.tx_id)
            if events is not None and "COMMITTED" in events:
                return _rollback_committed(root, store, change)
            if events is not None:
                Transaction.resume(root, transaction_ids=[change.tx_id])
        store.clear_staged(cs_id)
        store.append(cs_id, EV_ABORTED, {"reason": "operator-rollback"})
        store.prune(keep=3)
        return {
            "changeset_id": cs_id,
            "rolled_back": True,
            "state": "aborted",
            "vault_changed": False,
        }

    if change.state == "committed":
        return _rollback_committed(root, store, change)

    raise ChangeSetCoordinatorError(
        f"change set {cs_id} is in an unknown state {change.state}"
    )


def _rollback_committed(
    root: Path, store: ChangeSetStore, change: ChangeSet
) -> dict:
    if not change.tx_id:
        raise RollbackUnavailable(
            REASON_SNAPSHOT_MISSING,
            {"changeset_id": change.id, "hint": GIT_RECOVERY_HINT},
        )
    snapshot_root = (
        root / TX_ROOT_RELATIVE / change.tx_id / "snapshot"
    )
    snapshot = Snapshot(snapshot_root)
    try:
        entries = snapshot.read_manifest()
    except Exception as error:  # missing/damaged snapshot: refuse to guess
        raise RollbackUnavailable(
            REASON_SNAPSHOT_MISSING,
            {
                "changeset_id": change.id,
                "tx_id": change.tx_id,
                "hint": GIT_RECOVERY_HINT,
            },
        ) from error

    entries_by_relative = {entry.relative: (i, entry) for i, entry in enumerate(entries)}
    reverse_plan: list[PlanEntry] = []
    for intent in change.intents:
        if intent.relative not in entries_by_relative:
            raise RollbackUnavailable(
                REASON_SNAPSHOT_MISSING,
                {
                    "changeset_id": change.id,
                    "locations": [intent.relative],
                    "hint": GIT_RECOVERY_HINT,
                },
            )
        index, entry = entries_by_relative[intent.relative]
        if not entry.existed or entry.sha256 is None or entry.mode is None:
            # The conversion only ever replaced pre-existing files; a target
            # absent at snapshot is outside this protocol's undo.
            raise RollbackUnavailable(
                REASON_SNAPSHOT_MISSING,
                {
                    "changeset_id": change.id,
                    "locations": [intent.relative],
                    "hint": GIT_RECOVERY_HINT,
                },
            )
        original = (snapshot_root / f"{index:06d}.bin").read_bytes()
        if sha256_bytes(original) != entry.sha256 or entry.sha256 != intent.old_sha256:
            raise RollbackUnavailable(
                "rollback-snapshot-damaged",
                {
                    "changeset_id": change.id,
                    "locations": [intent.relative],
                    "hint": GIT_RECOVERY_HINT,
                },
            )
        reverse_plan.append(
            PlanEntry(
                relative=intent.relative,
                content=original,
                mode=entry.mode & 0o777,
                expected_current_sha256=intent.new_sha256,
            )
        )

    # Require that live bytes are exactly what this group applied; otherwise
    # undoing would clobber later, legitimate edits.
    drifted = _live_matches(root, change.intents, "new_sha256")
    if drifted:
        raise ChangeSetConflict(
            REASON_POST_COMMIT_DRIFT,
            {"changeset_id": change.id, "locations": drifted},
        )

    try:
        transaction = Transaction.begin(
            root, reverse_plan, operation=CONTENT_CONVERSION_OPERATION
        )
        result = transaction.run()
    except (TransactionError, PlanRejected) as error:
        raise ChangeSetConflict(
            "rollback-transaction-failed",
            {"changeset_id": change.id, "reason": type(error).__name__},
        ) from error

    store.append(
        change.id,
        EV_REVERTED,
        {"tx_id": change.tx_id, "revert_tx_id": result["tx_id"]},
    )
    store.prune(keep=3)
    return {
        "changeset_id": change.id,
        "rolled_back": True,
        "state": "reverted",
        "tx_id": result["tx_id"],
    }
