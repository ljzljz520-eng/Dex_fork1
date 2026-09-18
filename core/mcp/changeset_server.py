#!/usr/bin/env python3
"""
Dex Change Set MCP Server (read-only).

Exposes the vault's batch-change consistency state — clean / pending /
conflict — so readers (the agent UI, Doctor, troubleshooting) can see an
unfinished or partially applied Markdown conversion instead of silently
accepting it.

This server NEVER mutates the vault: it cannot prepare, commit, resume, or
roll anything back. Those actions live behind the explicit CLI
(``python -m core.changesets ...``).

Usage:
    python changeset_server.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    print("Error: MCP SDK not installed. Run: pip install mcp", file=sys.stderr)
    sys.exit(1)

from core.changesets.consistency import consistency_scan
from core.utils.feature_status import feature_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SERVER_NAME = "dex-changeset-mcp"
FEATURE_NAME = "Vault batch-change consistency"

server = Server("dex-changeset-mcp")


def _vault_root() -> Path:
    return Path(os.environ.get("VAULT_PATH", Path.cwd())).resolve()


def _status_payload(vault_root: Path) -> dict:
    """Build the read-only envelope, degrading to a health envelope on error."""
    try:
        if not vault_root.is_dir():
            return feature_status(
                FEATURE_NAME,
                "broken",
                "The vault path is not a folder, so change consistency cannot be read.",
            )
        envelope = consistency_scan(vault_root)
        message = {
            "clean": "No unfinished batch changes and no inconsistencies were found.",
            "pending": (
                "A batch change is staged or interrupted. Your files are not "
                "damaged. Run `python -m core.changesets status` to see the one "
                "next step."
            ),
            "conflict": (
                "Inconsistent file state was detected. Automatic sync is paused; "
                "run `python -m core.changesets status` to review it."
            ),
        }[envelope["status"]]
        return feature_status(
            FEATURE_NAME,
            "ok",
            message,
            **envelope,
        )
    except Exception as error:  # the tool must never crash its host
        logger.exception("changeset status failed")
        return feature_status(
            FEATURE_NAME,
            "unknown",
            "Change consistency could not be determined right now.",
            detail=type(error).__name__,
        )


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_changeset_status",
            description=(
                "Read-only. Report vault batch-change consistency: status "
                "(clean/pending/conflict), unfinished change sets, and "
                "conflicts with the task ids and files involved. Never writes, "
                "resumes, or rolls back anything."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="list_pending_changesets",
            description=(
                "Read-only. List only unfinished change sets (staged, "
                "committing, or interrupted) with their state and the single "
                "resume/rollback command to suggest. Never changes anything."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    vault_root = _vault_root()

    if name == "get_changeset_status":
        payload = _status_payload(vault_root)
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]

    if name == "list_pending_changesets":
        envelope = _status_payload(vault_root)
        pending = [
            change
            for change in envelope.get("changesets", [])
            if not change.get("terminal")
        ]
        payload = {
            **{
                key: value
                for key, value in envelope.items()
                if key in {"feature", "feature_status", "user_message"}
            },
            "pending_changesets": pending,
            "count": len(pending),
        }
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]

    return [
        TextContent(
            type="text",
            text=json.dumps({"error": f"Unknown tool: {name}"}),
        )
    ]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
