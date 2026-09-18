"""Read-only cross-file consistency view.

Answers the one question every reader (CLI, read-only MCP, the Obsidian sync
daemon) needs: is this vault ``clean``, ``pending`` (an unfinished but
consistent change set exists), or ``conflict`` (live bytes disagree with the
recorded intent or a task fan-out is visibly partial)?

The output deliberately contains only vault-relative paths, task ids,
fingerprints and counts — never file contents or absolute paths (NFR-6).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from core import portable_contract
from core.changesets.fingerprints import (
    TASK_ID_PATTERN,
    diff_task_maps,
    extract_task_occurrences_bytes,
    sha256_bytes,
)
from core.changesets.model import STATE_COMMITTED
from core.changesets.storage import ChangeSetStorageError, ChangeSetStore

STATUS_CLEAN = "clean"
STATUS_PENDING = "pending"
STATUS_CONFLICT = "conflict"

KIND_INTERRUPTED = "interrupted-changeset"
KIND_SET_DRIFT = "task-occurrence-set-drift"
KIND_COUNT_DRIFT = "task-occurrence-count-drift"
KIND_MIXED_STATE = "mixed-task-state"
KIND_FINGERPRINT_DRIFT = "fingerprint-drift"
KIND_UNREADABLE_FILE = "unreadable-file"

# Conflicts an automatic status fan-out must not touch: every one of them is
# attributable to a recorded (interrupted/drifted) change set. A bare
# mixed-task-state with no change set is deliberately NOT gated — it is the
# normal pre-sync state the Obsidian daemon exists to converge, and blocking
# it would dead-lock ordinary checkbox sync. It stays visible in the envelope.
GATED_CONFLICT_KINDS = frozenset(
    {
        KIND_INTERRUPTED,
        KIND_SET_DRIFT,
        KIND_COUNT_DRIFT,
        KIND_FINGERPRINT_DRIFT,
    }
)

_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[([ bBsSxX/])\]")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scannable_markdown(root: Path, relative: str) -> bool:
    """Ownership-scoped, regular, non-denied vault/seed markdown only."""
    target = root / relative
    if target.is_symlink() or not target.is_file():
        return False
    try:
        resolution = portable_contract.resolve(relative)
    except Exception:  # unclassified or contract violation: not content scope
        return False
    if resolution is None or resolution.denied:
        return False
    return resolution.ownership in {"vault", "seed"}


def _content_markdown_files(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    relatives: list[str] = []
    for target in root.rglob("*.md"):
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError:
            continue
        if _scannable_markdown(root, relative):
            relatives.append(relative)
    return sorted(relatives)


def _checkbox_states_by_task(text: str) -> dict[str, set[str]]:
    """task_id -> checkbox states ('open'/'done') found on its own lines."""
    states: dict[str, set[str]] = {}
    for line in text.splitlines():
        if "^task-" not in line:
            continue
        marker = _CHECKBOX_RE.match(line)
        state = "done" if marker and marker.group(1).lower() == "x" else "open"
        for task_id in TASK_ID_PATTERN.findall(line):
            bare = task_id[1:]  # strip the leading ^
            states.setdefault(bare, set()).add(state)
    return states


def consistency_scan(
    vault_root: Path | str,
    *,
    relatives: list[str] | None = None,
) -> dict:
    """Read-only envelope: status, change sets, conflicts, warnings."""
    root = Path(vault_root).resolve()
    store = ChangeSetStore(root)
    scan_relatives = relatives if relatives is not None else _content_markdown_files(root)

    # One pass over the live content: bytes hash, occurrences, checkbox state.
    live_occurrences: dict[str, dict[str, int]] = {}
    live_sha: dict[str, str] = {}
    state_files: dict[str, dict[str, set[str]]] = {}
    warnings: list[dict] = []

    for relative in scan_relatives:
        target = root / relative
        try:
            data = target.read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            warnings.append(
                {"kind": KIND_UNREADABLE_FILE, "locations": [relative]}
            )
            continue
        live_sha[relative] = sha256_bytes(data)
        for task_id, count in extract_task_occurrences_bytes(data).items():
            live_occurrences.setdefault(task_id, {})[relative] = count
        for task_id, states in _checkbox_states_by_task(text).items():
            for state in states:
                state_files.setdefault(task_id, {}).setdefault(
                    state, set()
                ).add(relative)

    conflicts: list[dict] = []
    changesets: list[dict] = []

    # --- change sets: pending groups and their live-byte reconciliation -----
    for cs_id in store.list_ids():
        try:
            change = store.load(cs_id)
        except ChangeSetStorageError:
            conflicts.append(
                {
                    "kind": KIND_INTERRUPTED,
                    "changeset_id": cs_id,
                    "locations": [],
                    "detail": {"unreadable_event_log": True},
                }
            )
            continue

        entry = change.summary()
        entry["relatives"] = change.relatives
        # Full invariant scope (changed targets plus unchanged targets that
        # carry task ids and were validated at prepare time).
        entry["occurrence_relatives"] = change.occurrence_relatives
        entry["task_ids"] = sorted(
            set(change.occurrences_before) | set(change.occurrences_after)
        )
        entry["interrupted"] = False
        changesets.append(entry)

        if change.is_terminal:
            continue

        if not change.intents:
            # PREPARING shell (crash mid-stage): intent unknown, but the
            # vault content itself is never touched during staging.
            entry["interrupted"] = True
            continue

        old_files = [i.relative for i in change.intents if live_sha.get(i.relative) == i.old_sha256]
        new_files = [i.relative for i in change.intents if live_sha.get(i.relative) == i.new_sha256]
        other_files = [
            i.relative
            for i in change.intents
            if live_sha.get(i.relative) not in (i.old_sha256, i.new_sha256)
        ]

        if change.state != STATE_COMMITTED:
            if other_files:
                # At least one target matches neither fingerprint.
                conflicts.append(
                    {
                        "kind": KIND_FINGERPRINT_DRIFT,
                        "changeset_id": cs_id,
                        "locations": sorted(other_files),
                        "detail": {
                            "applied": sorted(new_files),
                            "not_applied": sorted(old_files),
                            "drifted": sorted(other_files),
                        },
                    }
                )
            elif new_files and old_files:
                # Classic partial fan-out: some targets replaced, others not.
                conflicts.append(
                    {
                        "kind": KIND_FINGERPRINT_DRIFT,
                        "changeset_id": cs_id,
                        "locations": sorted(new_files),
                        "detail": {
                            "applied": sorted(new_files),
                            "not_applied": sorted(old_files),
                            "drifted": [],
                        },
                    }
                )
            elif new_files and not old_files:
                # Every target already carries the new bytes while the group
                # is not recorded committed: the commit result event was lost
                # or the group was interrupted at the very end — recoverable,
                # surfaced as an interrupted pending group, not a live drift.
                entry["interrupted"] = True
        elif new_files and (old_files or other_files):
            conflicts.append(
                {
                    "kind": KIND_FINGERPRINT_DRIFT,
                    "changeset_id": cs_id,
                    "locations": sorted(other_files or old_files),
                    "detail": {
                        "applied": sorted(new_files),
                        "not_applied": sorted(old_files),
                        "drifted": sorted(other_files),
                    },
                }
            )

        if change.failure:
            entry["interrupted"] = True

        # Task-ID occurrence baseline check for non-terminal groups. While a
        # group is pending, live content must still equal its before-map. The
        # invariant universe is every file prepare extracted occurrences from
        # (changed targets plus byte-identical targets carrying task ids), not
        # just the intent (changed-file) set.
        live_slice: dict[str, dict[str, int]] = {}
        target_relatives = set(change.occurrence_relatives)
        for task_id, files in live_occurrences.items():
            slice_files = {
                relative: count
                for relative, count in files.items()
                if relative in target_relatives
            }
            if slice_files:
                live_slice[task_id] = slice_files
        for violation in diff_task_maps(change.occurrences_before, live_slice):
            kind = (
                KIND_SET_DRIFT
                if violation.kind in {"missing-file", "extra-file"}
                else KIND_COUNT_DRIFT
            )
            conflicts.append(
                {
                    "kind": kind,
                    "changeset_id": cs_id,
                    "task_id": violation.task_id,
                    "locations": [violation.relative],
                    "detail": {
                        "difference": violation.kind,
                        "before_count": violation.before_count,
                        "live_count": violation.after_count,
                    },
                }
            )

    # --- mixed checkbox state across locations (partial status fan-out) -----
    for task_id, by_state in sorted(state_files.items()):
        done = by_state.get("done", set())
        open_ = by_state.get("open", set())
        if done and open_:
            conflicts.append(
                {
                    "kind": KIND_MIXED_STATE,
                    "task_id": task_id,
                    "locations": sorted(done | open_),
                    "detail": {
                        "completed_in": sorted(done),
                        "open_in": sorted(open_),
                    },
                }
            )

    pending_exists = any(not item["terminal"] for item in changesets)
    if conflicts:
        status = STATUS_CONFLICT
    elif pending_exists:
        status = STATUS_PENDING
    else:
        status = STATUS_CLEAN

    return {
        "status": status,
        "scanned_at": _utc_now(),
        "files_scanned": len(scan_relatives),
        "changesets": changesets,
        "conflicts": conflicts,
        "warnings": warnings,
    }


def pending_overlap(
    envelope: dict,
    *,
    task_ids: set[str] | list[str] | None = None,
    relatives: set[str] | list[str] | None = None,
) -> list[str]:
    """Non-terminal change set ids whose tasks or paths overlap the request."""
    wanted_tasks = set(task_ids or [])
    wanted_paths = set(relatives or [])
    hits: list[str] = []
    for change in envelope.get("changesets", []):
        if change.get("terminal"):
            continue
        if wanted_tasks & set(change.get("task_ids", [])):
            hits.append(change["id"])
            continue
        guarded_paths = set(change.get("relatives", [])) | set(
            change.get("occurrence_relatives", [])
        )
        if wanted_paths & guarded_paths:
            hits.append(change["id"])
    return hits


def fanout_gate(
    vault_root: Path | str,
    *,
    task_ids: set[str] | list[str] | None = None,
    relatives: set[str] | list[str] | None = None,
) -> dict:
    """Decision wrapper for automatic task-status fan-out writers.

    ``allowed`` is True only when no change-set-attributable conflict exists
    AND no pending change set overlaps the tasks/paths the fan-out would
    touch. A bare mixed checkbox state (the ordinary pre-sync state the
    daemon itself converges) does not block; see GATED_CONFLICT_KINDS.
    """
    envelope = consistency_scan(vault_root)
    overlapping = pending_overlap(
        envelope, task_ids=task_ids, relatives=relatives
    )
    blocking = [
        conflict
        for conflict in envelope["conflicts"]
        if conflict["kind"] in GATED_CONFLICT_KINDS
    ]
    blocked_reason = None
    if blocking:
        blocked_reason = "vault-conflict"
    elif overlapping:
        blocked_reason = "pending-changeset-overlap"
    return {
        "allowed": blocked_reason is None,
        "reason": blocked_reason,
        "overlapping_changesets": overlapping,
        "blocking_conflicts": blocking,
        "envelope": envelope,
    }
