"""Task-ID occurrence extraction and the fan-out invariant diff."""

from __future__ import annotations

import io

from core.changesets.fingerprints import (
    COUNT_MISMATCH,
    SET_EXTRA,
    SET_MISSING,
    TASK_ID_PATTERN,
    diff_task_maps,
    extract_task_occurrences,
    extract_task_occurrences_bytes,
    maps_equal,
    merge_file_occurrences,
    sha256_bytes,
    sha256_stream,
)


def test_bare_and_wikilink_anchors_both_count():
    text = (
        "- [ ] Do thing ^task-20260118-001\n"
        "See [[^task-20260118-001]] for the backlink\n"
        "Aliased [[^task-20260118-002|the task]] too\n"
    )

    assert extract_task_occurrences(text) == {
        "task-20260118-001": 2,
        "task-20260118-002": 1,
    }


def test_occurrences_inside_code_spans_are_still_counted():
    # The converter never edits code spans, so their counts are equal on both
    # sides — counting them keeps the invariant honest instead of hiding them.
    text = (
        "inline `^task-20260118-001` span\n"
        "```\nblock ^task-20260118-001 and ^task-20260118-009\n```\n"
    )

    assert extract_task_occurrences(text) == {
        "task-20260118-001": 2,
        "task-20260118-009": 1,
    }


def test_anchor_boundary_does_not_match_counter_prefix():
    # task-...-100 must not be found inside task-...-1000.
    assert TASK_ID_PATTERN.findall("x ^task-20260118-1000 y") == ["^task-20260118-1000"]
    assert extract_task_occurrences("^task-20260118-1000") == {"task-20260118-1000": 1}


def test_bytes_extractor_matches_text_and_round_trips_utf8():
    text = "café ^task-20260118-003 — [[^task-20260118-003]]"
    assert extract_task_occurrences_bytes(text.encode("utf-8")) == {
        "task-20260118-003": 2
    }


def test_merge_builds_global_file_set_map():
    target: dict = {}
    merge_file_occurrences(target, "03-Tasks/Tasks.md", {"task-20260118-001": 1})
    merge_file_occurrences(
        target, "05-Areas/People/Jane.md", {"task-20260118-001": 2, "task-20260118-002": 1}
    )

    assert target == {
        "task-20260118-001": {"03-Tasks/Tasks.md": 1, "05-Areas/People/Jane.md": 2},
        "task-20260118-002": {"05-Areas/People/Jane.md": 1},
    }


def test_diff_reports_missing_extra_and_count_mismatch():
    before = {
        "task-20260118-001": {
            "03-Tasks/Tasks.md": 1,
            "05-Areas/People/Jane.md": 1,  # disappears after conversion
        },
        "task-20260118-002": {
            "04-Projects/P.md": 1,
        },
        "task-20260118-003": {
            "03-Tasks/Tasks.md": 2,  # count changes
        },
    }
    after = {
        "task-20260118-001": {"03-Tasks/Tasks.md": 1},
        "task-20260118-002": {
            "04-Projects/P.md": 1,
            "06-Resources/New.md": 1,  # brand-new location
        },
        "task-20260118-003": {"03-Tasks/Tasks.md": 1},
    }

    violations = {
        (v.task_id, v.kind, v.relative): (v.before_count, v.after_count)
        for v in diff_task_maps(before, after)
    }

    assert violations == {
        ("task-20260118-001", SET_MISSING, "05-Areas/People/Jane.md"): (1, None),
        ("task-20260118-002", SET_EXTRA, "06-Resources/New.md"): (None, 1),
        ("task-20260118-003", COUNT_MISMATCH, "03-Tasks/Tasks.md"): (2, 1),
    }
    assert maps_equal(before, before) is True


def test_wikilink_wrapping_conversion_preserves_the_map():
    # The actual Obsidian conversion: bare anchors become wikilinks, but the
    # task-ID file-set and per-file counts are structurally identical.
    before = {"03-Tasks/Tasks.md": extract_task_occurrences("- [ ] x ^task-20260118-001")}
    after = {
        "03-Tasks/Tasks.md": extract_task_occurrences("- [ ] x [[^task-20260118-001]]")
    }

    assert diff_task_maps(before, after) == []


def test_streaming_hash_matches_in_memory_hash_across_chunk_boundary():
    payload = ("^task-20260118-001 " * 200_000).encode("utf-8")  # > 1 MiB
    assert len(payload) > 1 << 20

    stream_digest, stream_size = sha256_stream(io.BytesIO(payload))

    assert stream_size == len(payload)
    assert stream_digest == sha256_bytes(payload)
