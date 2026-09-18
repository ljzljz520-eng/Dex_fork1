#!/usr/bin/env python3
"""
Migrate existing Dex vault to Obsidian wiki link format
Zero AI tokens - pure regex pattern matching
"""
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.portable_contract import ContractViolation, resolve

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

BASE_DIR = Path(os.environ.get('VAULT_PATH', Path.cwd()))


def _entity_files(directory: Path):
    """Yield real entity pages, never the shipped folder placeholder."""
    if not directory.exists():
        return
    for entity_file in directory.rglob('*.md'):
        if entity_file.name.casefold() != 'readme.md':
            yield entity_file


def _is_migratable_markdown(path: Path) -> bool:
    """Only user-owned or seeded vault content may be rewritten."""
    if path.is_symlink():
        return False
    try:
        relative = path.relative_to(BASE_DIR).as_posix()
        resolution = resolve(relative)
    except (ContractViolation, ValueError):
        return False
    return not resolution.denied and resolution.ownership in {'seed', 'vault'}

# Build indices for smart conversion
def build_person_index() -> dict:
    """Build index of all person filenames"""
    people_dir = BASE_DIR / '05-Areas' / 'People'
    index = {}
    
    for person_file in _entity_files(people_dir):
        name = person_file.stem  # e.g., John_Doe
        rel_path = person_file.relative_to(BASE_DIR)
        index[name] = str(rel_path)
    
    return index

def build_project_index() -> dict:
    """Build index of all projects"""
    projects_dir = BASE_DIR / '04-Projects'
    index = {}
    
    for proj_file in _entity_files(projects_dir):
        name = proj_file.stem
        rel_path = proj_file.relative_to(BASE_DIR)
        index[name] = str(rel_path)
    
    return index

def build_company_index() -> dict:
    """Build index of all companies"""
    companies_dir = BASE_DIR / '05-Areas' / 'Companies'
    index = {}
    
    for comp_file in _entity_files(companies_dir):
        name = comp_file.stem
        rel_path = comp_file.relative_to(BASE_DIR)
        index[name] = str(rel_path)
    
    return index

def convert_references_in_file(content: str, person_idx: dict, 
                               project_idx: dict, company_idx: dict) -> Tuple[str, int]:
    """Convert plain text references to wiki links. Returns (new_content, num_changes)"""
    changes = 0
    
    # Skip fenced blocks and inline code spans.
    code_spans = []
    def save_code_span(match):
        code_spans.append(match.group(0))
        return f"__CODE_SPAN_{len(code_spans)-1}__"
    
    content = re.sub(r'```.*?```', save_code_span, content, flags=re.DOTALL)
    content = re.sub(
        r'(?P<ticks>`+).*?(?P=ticks)',
        save_code_span,
        content,
        flags=re.DOTALL,
    )
    
    # Convert person references (Firstname_Lastname pattern)
    for person_name, person_path in person_idx.items():
        # Only convert if not already a wiki link
        pattern = rf'(?<!\[\[)\b({re.escape(person_name)})\b(?!\]\])'
        matches = len(re.findall(pattern, content))
        if matches > 0:
            content = re.sub(pattern, r'[[\1]]', content)
            changes += matches
    
    # Convert project references (04-Projects/Project_Name)
    for project_name, project_path in project_idx.items():
        pattern = rf'(?<!\[\[)\b({re.escape(project_path)})\b(?!\]\])'
        matches = len(re.findall(pattern, content))
        if matches > 0:
            content = re.sub(pattern, r'[[\1]]', content)
            changes += matches
    
    # Convert company references
    for company_name, company_path in company_idx.items():
        pattern = rf'(?<!\[\[)\b({re.escape(company_name)})\b(?!\]\])'
        matches = len(re.findall(pattern, content))
        if matches > 0:
            content = re.sub(pattern, r'[[\1]]', content)
            changes += matches
    
    # Convert task ID references (^task-YYYYMMDD-XXX)
    pattern = r'(?<!\[\[)\^(task-\d{8}-\d{3,})(?!\]\])'
    matches = len(re.findall(pattern, content))
    if matches > 0:
        content = re.sub(pattern, r'[[^\1]]', content)
        changes += matches
    
    # Restore code blocks and inline spans.
    for i, span in enumerate(code_spans):
        content = content.replace(f"__CODE_SPAN_{i}__", span)
    
    return content, changes

def estimate_migration(files: List[Path]) -> str:
    """Estimate migration time"""
    num_files = len(files)
    est_seconds = num_files / 30  # ~30 files/sec
    
    if est_seconds < 60:
        return f"~{int(est_seconds)} seconds"
    else:
        minutes = int(est_seconds / 60)
        return f"~{minutes} minute{'s' if minutes > 1 else ''}"


def create_git_backup() -> tuple[str, bool]:
    """Capture the exact pre-migration state and return its commit plus creation flag."""
    add = subprocess.run(
        ['git', 'add', '-A'],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        raise RuntimeError(add.stderr.strip() or 'git could not stage the vault')

    staged = subprocess.run(
        ['git', 'diff', '--cached', '--quiet', '--'],
        cwd=BASE_DIR,
    )
    if staged.returncode not in (0, 1):
        raise RuntimeError('git could not verify the staged backup state')

    created = staged.returncode == 1
    if created:
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        commit = subprocess.run(
            ['git', 'commit', '-m', f'Backup before Obsidian migration - {timestamp}'],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            raise RuntimeError(commit.stderr.strip() or commit.stdout.strip() or 'git could not create the backup commit')

    baseline = subprocess.run(
        ['git', 'rev-parse', 'HEAD'],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
    )
    if baseline.returncode != 0:
        raise RuntimeError(baseline.stderr.strip() or 'git could not identify the backup commit')
    return baseline.stdout.strip(), created

def _bytes_converter(person_idx: dict, project_idx: dict, company_idx: dict):
    """Adapt the pure text conversion to the changeset bytes->bytes contract."""

    def convert(data: bytes) -> bytes:
        text = data.decode('utf-8')
        new_text, _changes = convert_references_in_file(
            text, person_idx, project_idx, company_idx
        )
        return new_text.encode('utf-8')

    return convert


def _preview(files: List[Path], person_idx: dict,
             project_idx: dict, company_idx: dict) -> Tuple[int, int]:
    """Read-only dry run: (files that would change, total conversions)."""
    files_modified = 0
    total_changes = 0
    for md_file in tqdm(files, desc='Checking') if HAS_TQDM else files:
        content = md_file.read_text(encoding='utf-8')
        _new_content, changes = convert_references_in_file(
            content, person_idx, project_idx, company_idx
        )
        if changes > 0:
            files_modified += 1
            total_changes += changes
    return files_modified, total_changes


def migrate_vault(dry_run: bool = False) -> int:
    """Main migration function: index -> prepare -> confirm -> atomic commit."""
    from core.changesets import coordinator
    from core.changesets.cli import pending_guidance, prepare_rejection_message

    print("Dex Obsidian Migration\n" + "="*50)

    # Build indices
    print("Building indices...")
    person_idx = build_person_index()
    project_idx = build_project_index()
    company_idx = build_company_index()
    print(f"  Found {len(person_idx)} people")
    print(f"  Found {len(project_idx)} projects")
    print(f"  Found {len(company_idx)} companies")

    # Find all markdown files
    print("\nScanning vault...")
    md_files = [
        path for path in BASE_DIR.rglob('*.md')
        if _is_migratable_markdown(path)
    ]
    relatives = sorted(
        path.relative_to(BASE_DIR).as_posix() for path in md_files
    )
    print(f"  Found {len(md_files)} markdown files")
    print(f"  Estimated time: {estimate_migration(md_files)}")

    if dry_run:
        print("\n[DRY RUN MODE] - No files will be modified")

    # Never open a second conversion on top of an unfinished one.
    guidance = pending_guidance(BASE_DIR)
    if guidance is not None:
        print("\n" + guidance)
        return 0

    input("\nPress Enter to continue...")

    if dry_run:
        files_modified, total_changes = _preview(
            md_files, person_idx, project_idx, company_idx
        )
        print("\n" + "="*50)
        print("Dry run complete")
        print(f"  Files scanned: {len(md_files)}")
        print(f"  Files that would change: {files_modified}")
        print(f"  Total conversions: {total_changes}")
        print(
            "\n[DRY RUN] No files were modified. Run without --dry-run to "
            "apply changes."
        )
        return 0

    # Capture the exact pre-migration state BEFORE staging anything, so the
    # backup commit reflects only the user's untouched vault.
    print("\nCreating backup...")
    try:
        backup_ref, backup_created = create_git_backup()
    except RuntimeError as error:
        print(f"Backup failed; no files were changed: {error}")
        return 1
    if backup_created:
        print(f"  Backup commit created: {backup_ref}")
    else:
        print(f"  No new commit needed; the vault was already captured at {backup_ref}")

    # Phase 1: convert and stage every file, verify fingerprints and the
    # task-id invariant. Vault content is still untouched here.
    print("\nPreparing all files (nothing is changed yet)...")
    outcome = coordinator.prepare(
        BASE_DIR,
        relatives,
        _bytes_converter(person_idx, project_idx, company_idx),
    )

    if outcome.status == coordinator.REJECTED:
        print(
            "\nMigration stopped before any file was changed:\n  "
            + prepare_rejection_message(outcome.reason)
        )
        if outcome.detail:
            print(f"  details: {outcome.detail}")
        return 1

    if outcome.status == coordinator.NOOP:
        print("\nNothing to convert — every reference is already a wiki link.")
        return 0

    print(f"  Prepared {outcome.files_changed} of {outcome.files_scanned} files.")

    # Phase 2: one library lock, one atomic group replacement.
    print("\nApplying the whole batch atomically...")
    try:
        coordinator.commit(BASE_DIR, outcome.changeset_id)
    except coordinator.ChangeSetConflict as error:
        print(
            "\nThe batch could not be applied safely and your files were not "
            "changed. Your prepared copy is safely stored."
        )
        print(f"  reason: {error.reason}")
        print(
            f"\nRun `python -m core.changesets status` to see the one next step."
        )
        return 1

    # Summary
    print("\n" + "="*50)
    print("Migration Complete!")
    print(f"  Files scanned: {outcome.files_scanned}")
    print(f"  Files modified: {outcome.files_changed}")
    print(f"  Change set: {outcome.changeset_id}")
    print(f"\nBackup baseline: {backup_ref}")
    print(
        "To revert these changes without rewriting history: "
        f"git restore --source={backup_ref} --worktree -- ."
    )

    # macOS notification
    subprocess.run([
        'osascript', '-e',
        f'display notification "{outcome.files_changed} files converted with wiki links" '
        f'with title "Dex Obsidian Migration Complete" sound name "Glass"'
    ])

    # Sound
    subprocess.run(['afplay', '/System/Library/Sounds/Glass.aiff'])

    return 0

if __name__ == '__main__':
    dry_run = '--dry-run' in sys.argv
    raise SystemExit(migrate_vault(dry_run=dry_run))
