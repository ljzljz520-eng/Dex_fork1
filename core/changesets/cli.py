"""Command-line entry point for batch change sets.

``python -m core.changesets status|resume|rollback``

The human-facing wording is part of the product: after an interruption a
non-technical user must learn (1) what happened, (2) that their files are not
damaged, and (3) the single command to run next (NFR-4). Outputs contain
vault-relative paths and ids only — never file contents.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from core.changesets.consistency import (
    GATED_CONFLICT_KINDS,
    STATUS_CLEAN,
    STATUS_CONFLICT,
    STATUS_PENDING,
    consistency_scan,
)
from core.changesets.coordinator import (
    GIT_RECOVERY_HINT,
    ChangeSetConflict,
    ChangeSetCoordinatorError,
    RollbackUnavailable,
    resume,
    rollback,
)
from core.changesets.storage import ChangeSetNotFound, ChangeSetStore

PROGRAM = "python -m core.changesets"

EXIT_CLEAN = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PENDING = 3
EXIT_CONFLICT = 4


def _vault_root(value: str | None) -> Path:
    return Path(value or os.environ.get("VAULT_PATH") or Path.cwd()).resolve()


PREPARE_REJECTION_TEXT = {
    "pending-changeset-exists": (
        "Another batch change is already waiting to be finished or undone."
    ),
    "unsafe-path": "One target path is outside the vault.",
    "not-markdown": "Only Markdown (.md) files can be converted.",
    "symlink-target": "One target is a symlink and was skipped for safety.",
    "not-regular-file": "One target is not a regular file.",
    "not-utf-8": "One file is not valid UTF-8 text.",
    "contract-forbids": (
        "One target is a protected product file that the ownership contract "
        "does not allow this migration to rewrite."
    ),
    "converter-failed": "The conversion failed while reading one file.",
    "live-fingerprint-drift": (
        "A file changed during preparation; the batch was cancelled for safety."
    ),
    "staged-content-damaged": (
        "The prepared copy could not be verified; the batch was cancelled."
    ),
    "task-occurrence-invariant": (
        "The conversion would have moved or duplicated a task id, so the "
        "batch was cancelled to protect task links."
    ),
}


def prepare_rejection_message(reason: str | None) -> str:
    return PREPARE_REJECTION_TEXT.get(
        reason or "unknown", f"The change could not be prepared ({reason})."
    )


# ---------------------------------------------------------------------------
# Plain-language rendering
# ---------------------------------------------------------------------------

def _resume_command(cs_id: str) -> str:
    return f"{PROGRAM} resume --id {cs_id}"


def _rollback_command(cs_id: str) -> str:
    return f"{PROGRAM} rollback --id {cs_id}"


def _describe_state(summary: dict) -> str:
    state = summary.get("state")
    interrupted = summary.get("interrupted")
    if state == "prepared":
        return (
            "Your file changes were fully prepared and double-checked, but "
            "they have not been applied yet. Your files are exactly as you "
            "left them; nothing is damaged."
        )
    if state == "committing" or interrupted:
        return (
            "Applying these file changes was interrupted partway. Dex kept a "
            "complete record, so no files are damaged, but the job is not "
            "finished yet."
        )
    if state == "preparing":
        return (
            "Preparing these file changes was interrupted before any file "
            "was touched. Your files are unchanged and nothing is damaged."
        )
    return (
        "A batch change is not finished. Your files are protected; Dex kept a "
        "complete record of what was happening."
    )


def _recommended_action(summary: dict) -> tuple[str, str]:
    """Return (the one next step, the command) for a non-terminal group."""
    state = summary.get("state")
    cs_id = summary["id"]
    if state == "preparing":
        return (
            "Nothing was applied, so the clean action is to discard the "
            "preparation. Run this one command:",
            _rollback_command(cs_id),
        )
    return (
        "Pick up exactly where it stopped and finish the job. Run this one "
        "command:",
        _resume_command(cs_id),
    )


def _render_group_block(summary: dict, *, under_conflict: bool = False) -> list[str]:
    lines = [f"Change set: {summary['id']}"]
    if under_conflict and summary.get("state") == "prepared":
        # The vault is not byte-identical to preparation anymore, so the
        # "files exactly as you left them" reassurance would be untrue.
        lines.append(
            "This batch was fully prepared and double-checked, but Dex also "
            "found files that no longer match the prepared version. Review "
            "the conflict details below before finishing or undoing it."
        )
    else:
        lines.append(_describe_state(summary))
    step, command = _recommended_action(summary)
    lines.append(step)
    lines.append(f"  {command}")
    lines.append(
        "If you would rather undo it instead, run "
        f"{_rollback_command(summary['id'])}."
    )
    return lines


def _render_conflict(conflict: dict) -> list[str]:
    kind = conflict["kind"]
    locations = ", ".join(conflict.get("locations", [])) or "(recorded change set)"
    cs_id = conflict.get("changeset_id")
    lead = {
        "interrupted-changeset": "A change record could not be read",
        "fingerprint-drift": "Some files no longer match the recorded change",
        "task-occurrence-set-drift": "A task id moved to a different file",
        "task-occurrence-count-drift": "A task id appears a different number of times",
        "mixed-task-state": "A task is marked done in one file and open in another",
    }.get(kind, kind)
    lines = [f"- {lead} ({kind})"]
    if locations:
        lines.append(f"  files: {locations}")
    if conflict.get("task_id"):
        lines.append(f"  task: {conflict['task_id']}")
    if cs_id:
        lines.append(f"  change set: {cs_id}")
        if kind == "fingerprint-drift":
            # Resume refuses drifted files by design; the clean move is to
            # discard the prepared batch (pre-commit) or restore via rollback.
            lines.append(f"  next command: {_rollback_command(cs_id)}")
        elif kind == "interrupted-changeset":
            lines.append(
                "  next command: run the status command and follow the "
                "recovery instructions shown for this change set"
            )
        else:
            lines.append(f"  next command: {_resume_command(cs_id)}")
    return lines


def render_human(envelope: dict, *, only_id: str | None = None) -> str:
    blocks: list[str] = []
    pending = [c for c in envelope["changesets"] if not c.get("terminal")]
    if only_id is not None:
        pending = [c for c in pending if c["id"] == only_id]

    if envelope["status"] == STATUS_CLEAN:
        blocks.append(
            "Everything looks good. No unfinished batch changes and no "
            "inconsistencies were found."
        )

    if envelope["status"] == STATUS_PENDING and not envelope["conflicts"]:
        blocks.append(
            "A batch change is waiting. Your files are safe; here is what to do."
        )

    for summary in pending:
        blocks.append(
            "\n".join(
                _render_group_block(
                    summary, under_conflict=bool(envelope["conflicts"])
                )
            )
        )

    if envelope["conflicts"]:
        blocks.append(
            "Dex found inconsistent file state. Automatic syncing is paused "
            "so nothing gets half-written, but no files are damaged. Review "
            "these details:"
        )
        for conflict in envelope["conflicts"]:
            blocks.append("\n".join(_render_conflict(conflict)))

    if envelope["warnings"]:
        blocks.append(
            "Some files could not be read during the scan:"
        )
        for warning in envelope["warnings"]:
            files = ", ".join(warning.get("locations", []))
            blocks.append(f"- {warning['kind']}: {files}")

    blocks.append(
        f"Scanned {envelope['files_scanned']} file(s) at {envelope['scanned_at']}."
    )
    return "\n\n".join(blocks)


def pending_guidance(vault_root: Path | str) -> str | None:
    """Human guidance when a rerun must be blocked, else None.

    Used by the migration script so reruns point at the existing group instead
    of opening a second one. A bare ``mixed-task-state`` conflict (one mirror
    page ticked moments before the sync daemon converges it) with no recorded
    change set is the ordinary pre-sync state — it does NOT block a new
    migration and must not be described as an unfinished previous migration.
    Only non-terminal groups and conflicts attributable to a recorded group
    block the rerun.
    """
    envelope = consistency_scan(vault_root)
    pending = [c for c in envelope["changesets"] if not c.get("terminal")]
    attributable = [
        conflict
        for conflict in envelope["conflicts"]
        if conflict.get("changeset_id")
        and conflict["kind"] in GATED_CONFLICT_KINDS
    ]
    if not pending and not attributable:
        return None

    sections: list[str] = []
    if pending:
        sections.append(
            "A previous Obsidian migration did not finish. Dex did not start a "
            "new one — starting again could leave files half-converted."
        )
        for summary in pending:
            sections.append("\n".join(_render_group_block(summary)))
    else:
        # The group cannot be summarized (e.g. its event log is unreadable) or
        # live bytes drifted away from a recorded group after it vanished from
        # the readable ledger.
        sections.append(
            "An earlier batch change left unresolved file state, so Dex did "
            "not start a new migration — running another one could mix the "
            "two problems together. Here is what was found:"
        )
        for conflict in attributable:
            sections.append("\n".join(_render_conflict(conflict)))

    sections.append(f"See the full picture with:\n  {PROGRAM} status")
    if pending:
        sections.append(
            "Your notes are not damaged: prepared changes are kept safely "
            "aside until you choose to finish or undo them."
        )
    else:
        sections.append("Your notes are not damaged; nothing was written by this attempt.")
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    root = _vault_root(args.vault)
    envelope = consistency_scan(root)

    if args.id:
        store = ChangeSetStore(root)
        try:
            store.load(args.id)
        except ChangeSetNotFound:
            if args.json:
                print(json.dumps({"success": False, "error": "not-found",
                                  "changeset_id": args.id}))
            else:
                print(f"No change set named {args.id} was found.")
            return EXIT_FAILED

    if args.json:
        print(json.dumps(envelope, indent=2))
    else:
        print(render_human(envelope, only_id=args.id))

    if envelope["status"] == STATUS_CONFLICT:
        return EXIT_CONFLICT
    if envelope["status"] == STATUS_PENDING:
        return EXIT_PENDING
    return EXIT_CLEAN


def cmd_resume(args: argparse.Namespace) -> int:
    root = _vault_root(args.vault)
    try:
        result = resume(root, args.id)
    except ChangeSetConflict as error:
        if args.json:
            print(json.dumps(
                {"success": False, "error": error.reason, "detail": error.detail},
                indent=2,
            ))
        else:
            print(
                "Dex could not safely finish this change set, because the "
                "files changed since it was prepared. Nothing was written, so "
                "your files are not damaged."
            )
            print(f"  problem: {error.reason}")
            print(
                "If you want to discard the prepared change instead, run:\n"
                f"  {_rollback_command(args.id)}"
            )
        return EXIT_FAILED
    except (ChangeSetCoordinatorError, ChangeSetNotFound) as error:
        if args.json:
            print(json.dumps({"success": False, "error": str(error)}))
        else:
            print(f"Nothing to resume: {error}")
        return EXIT_FAILED

    if args.json:
        print(json.dumps({"success": True, **result}, indent=2))
    else:
        print(
            "Done. Every file in the batch was updated together and the job "
            "is fully finished — nothing is left half-applied."
        )
    return EXIT_CLEAN


def cmd_rollback(args: argparse.Namespace) -> int:
    root = _vault_root(args.vault)
    store = ChangeSetStore(root)
    try:
        change = store.load(args.id)
    except ChangeSetNotFound as error:
        if args.json:
            print(json.dumps({"success": False, "error": str(error)}))
        else:
            print(f"No change set named {args.id} was found.")
        return EXIT_FAILED

    is_committed = change.state == "committed"
    if is_committed and not args.force and sys.stdin.isatty():
        try:
            answer = input(
                "This will undo an already-applied change and restore the "
                "original files. Type 'yes' to continue: "
            )
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("Cancelled; nothing was changed.")
            return EXIT_CLEAN

    try:
        result = rollback(root, args.id)
    except RollbackUnavailable as error:
        if args.json:
            print(json.dumps(
                {"success": False, "error": error.reason, "detail": error.detail},
                indent=2,
            ))
        else:
            print(
                "Dex cannot undo this one automatically because the safety "
                "snapshot has already been cleaned up. Nothing was changed."
            )
            print(GIT_RECOVERY_HINT)
        return EXIT_FAILED
    except (ChangeSetConflict, ChangeSetCoordinatorError) as error:
        reason = getattr(error, "reason", str(error))
        if args.json:
            print(json.dumps({"success": False, "error": reason}))
        else:
            print(
                "Dex could not safely undo this change because the files "
                f"changed since the batch ran ({reason}). Nothing was written."
            )
        return EXIT_FAILED

    if args.json:
        print(json.dumps({"success": True, **result}, indent=2))
    else:
        if result.get("vault_changed") is False:
            print(
                "Cancelled. The prepared changes were discarded and your "
                "files were never touched."
            )
        else:
            print(
                "Undone. Every file was restored to its original text in one "
                "atomic step, so nothing is left half-reverted."
            )
    return EXIT_CLEAN


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.changesets",
        description=(
            "Inspect and recover batch Markdown changes (two-phase change "
            "sets). Read-only status; explicit resume/rollback only."
        ),
    )
    parser.add_argument(
        "--vault",
        help="Vault root (defaults to $VAULT_PATH, then the current folder).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status",
        help="Show clean/pending/conflict state and the one next step.",
    )
    status.add_argument("--id", help="Focus on one change set id.")
    status.add_argument("--json", action="store_true", help="Raw envelope JSON.")
    status.set_defaults(handler=cmd_status)

    resume_p = sub.add_parser("resume", help="Finish an interrupted change set.")
    resume_p.add_argument("--id", required=True)
    resume_p.add_argument("--json", action="store_true")
    resume_p.set_defaults(handler=cmd_resume)

    revert = sub.add_parser("rollback", help="Cancel or undo a change set.")
    revert.add_argument("--id", required=True)
    revert.add_argument(
        "--force",
        action="store_true",
        help="Skip the confirmation for undoing an already-applied change.",
    )
    revert.add_argument("--json", action="store_true")
    revert.set_defaults(handler=cmd_rollback)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
