"""Content fingerprints and task-ID occurrence maps.

The occurrence invariant for a batch conversion is deliberately structural,
not semantic: every task ID (``^task-YYYYMMDD-XXX``) must appear in the SAME
set of files the SAME number of times before and after. The same extractor
matches both the bare anchor (``^task-...``) and the Obsidian wikilink form
(``[[^task-...]]``), so link-wrapping conversions are covered for free.
Occurrences inside code spans are counted too — the converter never touches
them, so equal counts there are exactly what we expect.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import BinaryIO

from core.changesets.model import OccurrenceMap

#: Anchored like Work MCP's ``find_task_by_id``: the trailing (?!\\d) stops
#: task-...-100 matching inside task-...-1000.
TASK_ID_PATTERN = re.compile(r"\^task-\d{8}-\d{3,}(?!\d)")

#: Diff kinds (stable strings — they travel in events and reader envelopes).
SET_MISSING = "missing-file"
SET_EXTRA = "extra-file"
COUNT_MISMATCH = "count-mismatch"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_stream(handle: BinaryIO) -> tuple[str, int]:
    """Stream-hash a binary file object without holding it in memory."""
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = handle.read(1 << 20)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class OccurrenceViolation:
    """One fan-out invariant difference for one task ID in one file."""

    task_id: str
    kind: str
    relative: str
    before_count: int | None
    after_count: int | None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "relative": self.relative,
            "before_count": self.before_count,
            "after_count": self.after_count,
        }


def extract_task_ids(text: str) -> list[str]:
    """All task IDs in one document, in document order (with repeats)."""
    return [
        match.group(1)
        for match in re.finditer(r"\^(task-\d{8}-\d{3,})(?!\d)", text)
    ]


def extract_task_occurrences(text: str) -> dict[str, int]:
    """task_id -> occurrence count within one document."""
    counts: dict[str, int] = {}
    for task_id in extract_task_ids(text):
        counts[task_id] = counts.get(task_id, 0) + 1
    return counts


def extract_task_occurrences_bytes(data: bytes) -> dict[str, int]:
    """Occurrence counts for raw file bytes (strict UTF-8 — a file we cannot
    decode must surface at prepare time, never get a silently empty map)."""
    return extract_task_occurrences(data.decode("utf-8"))


def merge_file_occurrences(
    target: OccurrenceMap, relative: str, counts: dict[str, int]
) -> None:
    """Merge one file's counts into a global task_id -> {file -> count} map."""
    for task_id, count in counts.items():
        files = target.setdefault(task_id, {})
        files[relative] = files.get(relative, 0) + count


def diff_task_maps(
    before: OccurrenceMap, after: OccurrenceMap
) -> list[OccurrenceViolation]:
    """Every structural difference between two occurrence maps.

    For each task ID seen on either side: files only on the left are
    ``missing-file`` (conversion dropped an occurrence location), files only
    on the right are ``extra-file``, shared files with unequal counts are
    ``count-mismatch``. Output is deterministically ordered.
    """
    violations: list[OccurrenceViolation] = []
    for task_id in sorted(set(before) | set(after)):
        left = before.get(task_id, {})
        right = after.get(task_id, {})
        for relative in sorted(set(left) - set(right)):
            violations.append(
                OccurrenceViolation(task_id, SET_MISSING, relative, left[relative], None)
            )
        for relative in sorted(set(right) - set(left)):
            violations.append(
                OccurrenceViolation(task_id, SET_EXTRA, relative, None, right[relative])
            )
        for relative in sorted(set(left) & set(right)):
            if left[relative] != right[relative]:
                violations.append(
                    OccurrenceViolation(
                        task_id, COUNT_MISMATCH, relative, left[relative], right[relative]
                    )
                )
    return violations


def maps_equal(before: OccurrenceMap, after: OccurrenceMap) -> bool:
    return not diff_task_maps(before, after)
