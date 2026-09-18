"""Read-only dex-changeset-mcp: stdio boot, tool list, envelope parity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from core.changesets.consistency import (
    STATUS_CLEAN,
    STATUS_CONFLICT,
    STATUS_PENDING,
    consistency_scan,
)
from core.changesets.coordinator import prepare
from core.utils import preflight
from core.utils.mcp_handshake import mcp_stdio_handshake

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_NAME = "dex-changeset-mcp"


def _seed_clean(root: Path) -> None:
    tasks = root / "03-Tasks/Tasks.md"
    people = root / "05-Areas/People/Jane_Doe.md"
    tasks.parent.mkdir(parents=True)
    people.parent.mkdir(parents=True)
    tasks.write_text("- [ ] Call ^task-20260118-001\n", encoding="utf-8")
    people.write_text("Linked ^task-20260118-001\n", encoding="utf-8")


def _rpc_client(vault: Path):
    env = {
        "HOME": str(vault),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
        "VAULT_PATH": str(vault),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "core.mcp.changeset_server"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    def send(payload: dict) -> None:
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    def read() -> dict:
        return json.loads(process.stdout.readline())

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            }
        )
        init = read()
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
        )
        yield send, read, init
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def test_stdio_boots_and_lists_tools(tmp_path: Path) -> None:
    _seed_clean(tmp_path)
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
        "VAULT_PATH": str(tmp_path),
    }
    result = mcp_stdio_handshake(
        [sys.executable, "-m", "core.mcp.changeset_server"],
        cwd=REPO_ROOT,
        env=env,
        timeout=30,
    )

    assert result.ok, f"{result.error}\nstderr:\n{result.stderr}"
    assert result.response["result"]["serverInfo"]["name"] == SERVER_NAME

    gen = _rpc_client(tmp_path)
    send, read, init = next(gen)
    assert init["result"]["serverInfo"]["name"] == SERVER_NAME

    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    listed = read()
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert names == {"get_changeset_status", "list_pending_changesets"}


def test_status_tool_matches_library_envelope(tmp_path: Path) -> None:
    _seed_clean(tmp_path)

    gen = _rpc_client(tmp_path)
    send, read, _init = next(gen)
    send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_changeset_status", "arguments": {}},
        }
    )
    clean = json.loads(read()["result"]["content"][0]["text"])
    assert clean["feature_status"] == "ok"
    assert clean["status"] == STATUS_CLEAN
    assert clean["status"] == consistency_scan(tmp_path)["status"]


def test_status_tool_reports_conflict_and_never_writes(tmp_path: Path) -> None:
    _seed_clean(tmp_path)
    # Mixed checkbox state: visible partial fan-out.
    (tmp_path / "03-Tasks/Tasks.md").write_text(
        "- [x] Call ^task-20260118-001\n", encoding="utf-8"
    )

    gen = _rpc_client(tmp_path)
    send, read, _init = next(gen)
    send(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "get_changeset_status", "arguments": {}},
        }
    )
    payload = json.loads(read()["result"]["content"][0]["text"])
    assert payload["status"] == STATUS_CONFLICT
    kinds = {conflict["kind"] for conflict in payload["conflicts"]}
    assert "mixed-task-state" in kinds

    send(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "list_pending_changesets", "arguments": {}},
        }
    )
    pending_payload = json.loads(read()["result"]["content"][0]["text"])
    assert pending_payload["count"] == 0


def test_pending_changesets_listed(tmp_path: Path) -> None:
    import re

    _seed_clean(tmp_path)

    def conv(data: bytes) -> bytes:
        return re.sub(
            rb"(?<!\[\[)\^(task-\d{8}-\d{3,})(?!\]\])",
            rb"[[^\1]]",
            data,
        )

    outcome = prepare(
        tmp_path,
        ["03-Tasks/Tasks.md", "05-Areas/People/Jane_Doe.md"],
        conv,
    )
    assert outcome.status == "prepared"

    gen = _rpc_client(tmp_path)
    send, read, _init = next(gen)
    send(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "list_pending_changesets", "arguments": {}},
        }
    )
    payload = json.loads(read()["result"]["content"][0]["text"])
    assert payload["count"] == 1
    assert payload["pending_changesets"][0]["id"] == outcome.changeset_id
    assert consistency_scan(tmp_path)["status"] == STATUS_PENDING


def test_server_is_registered_for_preflight_and_provisioning() -> None:
    assert preflight.SERVER_MODULES[SERVER_NAME] == "changeset_server.py"
    example = (REPO_ROOT / "System" / ".mcp.json.example").read_text(
        encoding="utf-8"
    )
    assert SERVER_NAME in example
    assert "core/mcp/changeset_server.py" in example
