"""Scale budget: a 12k-file vault converts with streaming memory.

Most files are unchanged; a few hundred carry a task anchor and actually
change. The assertions guard NFR-3: per-file streaming (no whole-vault byte
residency), staging bounded by changed bytes only, and full release after the
terminal state.
"""

from __future__ import annotations

import time
import tracemalloc
from pathlib import Path

from core.changesets import coordinator
from core.changesets.consistency import STATUS_CLEAN, consistency_scan

TOTAL_FILES = 12_000
CHANGED_FILES = 400
TASK_ID = "task-20260820-001"

WALL_BUDGET_SECONDS = 30.0
PEAK_BYTES_BUDGET = 25 * 1024 * 1024  # streaming: far below 48MB of live bytes


def _build_vault(root: Path) -> tuple[list[str], int]:
    notes = root / "05-Areas" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    filler = ("The quick brown fox keeps notes in plain markdown. " * 80) + "\n"
    changed_relatives: list[str] = []
    for index in range(TOTAL_FILES):
        relative = f"05-Areas/Notes/n{index:05d}.md"
        target = root / relative
        if index < CHANGED_FILES:
            target.write_text(f"# Note {index}\n- [ ] Follow up ^{TASK_ID}\n{filler}")
            changed_relatives.append(relative)
        else:
            target.write_text(f"# Note {index}\n{filler}")
    return changed_relatives, len(filler.encode("utf-8"))


def test_twelve_thousand_file_change_set_streams_and_releases(tmp_path: Path):
    changed_relatives, _ = _build_vault(tmp_path)

    def converter(data: bytes) -> bytes:
        if b"^task-" not in data:
            return data
        return data.replace(f"^{TASK_ID}".encode(), f"[[^{TASK_ID}]]".encode())

    started = time.perf_counter()
    tracemalloc.start()
    try:
        outcome = coordinator.prepare(
            tmp_path,
            [f"05-Areas/Notes/n{i:05d}.md" for i in range(TOTAL_FILES)],
            converter,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert outcome.status == "prepared", outcome.reason
    assert outcome.files_scanned == TOTAL_FILES
    assert outcome.files_changed == CHANGED_FILES
    assert peak < PEAK_BYTES_BUDGET, (
        f"prepare peaked at {peak / 1024 / 1024:.1f}MB; "
        "it must stream instead of holding the vault"
    )

    # Staging occupies exactly the new bytes of changed files — nothing more.
    from core.changesets.storage import ChangeSetStore

    store = ChangeSetStore(tmp_path)
    # Live files are still the ORIGINAL bytes here (commit has not run). Each
    # conversion adds exactly the 4 wrapper characters "[[" and "]]".
    staged_bytes = sum(
        store.staged_blob_path(outcome.changeset_id, index).stat().st_size
        for index in store.staged_indices(outcome.changeset_id)
    )
    expected = sum(
        len((tmp_path / relative).read_bytes()) + 4
        for relative in changed_relatives
    )
    assert staged_bytes == expected

    commit_started = time.perf_counter()
    coordinator.commit(tmp_path, outcome.changeset_id)
    elapsed = time.perf_counter() - started

    assert elapsed < WALL_BUDGET_SECONDS, (
        f"prepare+commit took {elapsed:.1f}s (budget {WALL_BUDGET_SECONDS}s)"
    )
    assert time.perf_counter() - commit_started < WALL_BUDGET_SECONDS

    # Terminal state releases staged blobs; live content is the new bytes.
    assert not store.staged_dir_for(outcome.changeset_id).exists()
    sample = tmp_path / changed_relatives[0]
    assert f"[[^{TASK_ID}]]" in sample.read_text()
    untouched = tmp_path / "05-Areas" / "Notes" / f"n{TOTAL_FILES - 1:05d}.md"
    assert "^task-" not in untouched.read_text()

    # The read side must also scan the vault inside the budget and report
    # clean (the committed group fully matches its new fingerprints).
    scan_started = time.perf_counter()
    envelope = consistency_scan(tmp_path)
    assert time.perf_counter() - scan_started < WALL_BUDGET_SECONDS
    assert envelope["status"] == STATUS_CLEAN
    assert envelope["files_scanned"] == TOTAL_FILES
