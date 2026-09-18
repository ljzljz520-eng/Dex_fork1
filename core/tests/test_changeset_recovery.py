"""Crash-window recovery: killed prepare/commit processes, explicit resume.

Every fault injection runs in a real subprocess (``os._exit(137)``) so the
recovery is exercised from a fresh process exactly as a user would hit it.
Nothing is ever auto-committed: the group only converges on an explicit
``resume`` / ``rollback`` call.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.changesets import coordinator
from core.changesets.coordinator import RollbackUnavailable
from core.changesets.storage import ChangeSetStore
from core.transaction.engine import TX_ROOT_RELATIVE

REPO_ROOT = Path(__file__).resolve().parents[2]

TASKS_MD = "03-Tasks/Tasks.md"
PEOPLE_MD = "05-Areas/People/Jane_Doe.md"
TARGETS = [TASKS_MD, PEOPLE_MD, "04-Projects/Launch.md"]

CHILD = r"""
import re, sys
from pathlib import Path
from core.changesets import coordinator

root = Path(sys.argv[1])
mode = sys.argv[2]
cs_id = sys.argv[3] if len(sys.argv) > 3 else ""
targets = ["03-Tasks/Tasks.md", "05-Areas/People/Jane_Doe.md", "04-Projects/Launch.md"]

def conv(data):
    return re.sub(rb"(?<!\[\[)\^(task-\d{8}-\d{3,})(?!\]\])", rb"[[^\1]]", data)

if mode == "prepare":
    outcome = coordinator.prepare(root, targets, conv)
    print(outcome.changeset_id or outcome.status)
elif mode == "commit":
    print(coordinator.commit(root, cs_id)["tx_id"])
elif mode == "resume":
    print(coordinator.resume(root, cs_id)["committed"])
elif mode == "rollback":
    outcome = coordinator.rollback(root, cs_id)
    print(outcome.get("state", "ok"))
else:
    raise SystemExit(f"unknown mode {mode}")
"""


def _seed_vault(root: Path) -> dict[str, bytes]:
    originals = {
        TASKS_MD: (
            b"# Tasks\n- [ ] Call Jane ^task-20260118-001\n"
            b"- [x] Done ^task-20260118-002\n"
        ),
        PEOPLE_MD: b"# Jane Doe\nLinked task: ^task-20260118-001\n",
        "04-Projects/Launch.md": b"# Launch\nNo references.\n",
    }
    for relative, data in originals.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return originals


def _run_child(root: Path, mode: str, cs_id: str = "", *, seam: str | None = None,
               tx_seam: str | None = None) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DEX_CHANGESET_STOP_AFTER", "DEX_TX_TEST_STOP_AFTER"}
    }
    if seam:
        env["DEX_CHANGESET_STOP_AFTER"] = seam
    if tx_seam:
        env["DEX_TX_TEST_STOP_AFTER"] = tx_seam
    return subprocess.run(
        [sys.executable, "-c", CHILD, str(root), mode, cs_id],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def _only_changeset(root: Path) -> str:
    ids = ChangeSetStore(root).list_ids()
    assert len(ids) == 1, ids
    return ids[0]


def test_kill_after_prepared_resumes_only_when_asked(tmp_path: Path):
    originals = _seed_vault(tmp_path)

    killed = _run_child(tmp_path, "prepare", seam="after-prepared")
    assert killed.returncode == 137

    cs_id = _only_changeset(tmp_path)
    store = ChangeSetStore(tmp_path)
    assert store.load(cs_id).state == "prepared"

    # No silent auto-commit: vault bytes are exactly the originals.
    for relative, data in originals.items():
        assert (tmp_path / relative).read_bytes() == data

    result = _run_child(tmp_path, "resume", cs_id)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"

    assert b"[[^task-20260118-001]]" in (tmp_path / TASKS_MD).read_bytes()
    assert b"[[^task-20260118-001]]" in (tmp_path / PEOPLE_MD).read_bytes()
    assert store.load(cs_id).state == "committed"


def test_kill_mid_apply_resumes_after_tx_self_rollback(tmp_path: Path):
    originals = _seed_vault(tmp_path)
    prepared = _run_child(tmp_path, "prepare")
    cs_id = prepared.stdout.strip()

    killed = _run_child(tmp_path, "commit", cs_id, tx_seam="mid-apply:0")
    assert killed.returncode == 137

    store = ChangeSetStore(tmp_path)
    assert store.load(cs_id).state == "committing"
    # The crash really did land a partial fan-out on disk: the first plan
    # entry (Tasks.md) applied, the second (Jane_Doe.md) never did.
    assert b"[[^task-" in (tmp_path / TASKS_MD).read_bytes()
    assert b"[[^task-" not in (tmp_path / PEOPLE_MD).read_bytes()

    # ...and nothing heals it until an explicit resume runs in a new process.
    result = _run_child(tmp_path, "resume", cs_id)
    assert result.returncode == 0, result.stderr

    for relative in (TASKS_MD, PEOPLE_MD):
        assert b"[[^task-20260118-001]]" in (tmp_path / relative).read_bytes()
    assert (tmp_path / "04-Projects/Launch.md").read_bytes() == originals[
        "04-Projects/Launch.md"
    ]
    assert store.load(cs_id).state == "committed"


def test_kill_after_commit_record_repairs_missing_result_event(tmp_path: Path):
    _seed_vault(tmp_path)
    prepared = _run_child(tmp_path, "prepare")
    cs_id = prepared.stdout.strip()

    killed = _run_child(tmp_path, "commit", cs_id, tx_seam="after-commit-record")
    assert killed.returncode == 137

    store = ChangeSetStore(tmp_path)
    assert store.load(cs_id).state == "committing"

    result = _run_child(tmp_path, "resume", cs_id)
    assert result.returncode == 0, result.stderr
    assert store.load(cs_id).state == "committed"
    assert b"[[^task-20260118-001]]" in (tmp_path / TASKS_MD).read_bytes()


def test_kill_during_staging_is_abortable_then_preparable_again(tmp_path: Path):
    originals = _seed_vault(tmp_path)
    killed = _run_child(tmp_path, "prepare", seam="after-stage:0")
    assert killed.returncode == 137

    cs_id = _only_changeset(tmp_path)
    assert ChangeSetStore(tmp_path).load(cs_id).state == "preparing"

    rolled = _run_child(tmp_path, "rollback", cs_id)
    assert rolled.returncode == 0, rolled.stderr
    assert ChangeSetStore(tmp_path).load(cs_id).state == "aborted"

    for relative, data in originals.items():
        assert (tmp_path / relative).read_bytes() == data

    retry = _run_child(tmp_path, "prepare")
    assert retry.returncode == 0, retry.stderr
    new_id = retry.stdout.strip()
    assert new_id != cs_id
    committed = _run_child(tmp_path, "commit", new_id)
    assert committed.returncode == 0, committed.stderr


def test_rollback_prepared_leaves_vault_untouched(tmp_path: Path):
    originals = _seed_vault(tmp_path)
    prepared = _run_child(tmp_path, "prepare")
    cs_id = prepared.stdout.strip()

    outcome = coordinator.rollback(tmp_path, cs_id)

    assert outcome["state"] == "aborted"
    assert ChangeSetStore(tmp_path).load(cs_id).state == "aborted"
    for relative, data in originals.items():
        assert (tmp_path / relative).read_bytes() == data


def test_rollback_committed_restores_original_bytes(tmp_path: Path):
    originals = _seed_vault(tmp_path)
    prepared = _run_child(tmp_path, "prepare")
    cs_id = prepared.stdout.strip()
    assert _run_child(tmp_path, "commit", cs_id).returncode == 0

    outcome = coordinator.rollback(tmp_path, cs_id)

    assert outcome["state"] == "reverted"
    for relative, data in originals.items():
        assert (tmp_path / relative).read_bytes() == data
    assert ChangeSetStore(tmp_path).load(cs_id).state == "reverted"


def test_rollback_without_snapshot_refuses_and_writes_nothing(tmp_path: Path):
    _seed_vault(tmp_path)
    prepared = _run_child(tmp_path, "prepare")
    cs_id = prepared.stdout.strip()
    _run_child(tmp_path, "commit", cs_id)
    converted = (tmp_path / TASKS_MD).read_bytes()

    # Simulate snapshot retention having reclaimed the undo bytes.
    tx_id = ChangeSetStore(tmp_path).load(cs_id).tx_id
    shutil.rmtree(tmp_path / TX_ROOT_RELATIVE / tx_id)

    with pytest.raises(RollbackUnavailable) as error:
        coordinator.rollback(tmp_path, cs_id)
    assert error.value.reason == "rollback-snapshot-unavailable"
    assert "git" in error.value.detail["hint"]
    # Zero writes: converted bytes and the committed record both stand.
    assert (tmp_path / TASKS_MD).read_bytes() == converted
    assert ChangeSetStore(tmp_path).load(cs_id).state == "committed"
