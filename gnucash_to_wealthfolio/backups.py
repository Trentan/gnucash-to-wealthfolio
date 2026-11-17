from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class BackupInfo:
    filename: str
    path: Path
    size_bytes: int
    size_mb: float
    created_at: str
    created_ts: float


def get_wealthfolio_backup_dir(backup_dir: Path) -> Path:
    """Return the designated subdirectory for Wealthfolio database backups."""
    return backup_dir / "wealthfolio"


def get_mapping_backup_dir(backup_dir: Path) -> Path:
    """Return the designated subdirectory for Mapping JSON backups."""
    return backup_dir / "mappings"


def list_backups(backup_dir: Path) -> list[BackupInfo]:
    """List all SQLite database backup files, sorted newest first.
    Checks <backup_dir>/wealthfolio/ as well as <backup_dir> for backwards compatibility.
    """
    if not backup_dir.exists():
        return []

    # Directories to inspect: subfolder first, then root backup_dir
    wf_sub = get_wealthfolio_backup_dir(backup_dir)
    search_dirs = [wf_sub] if wf_sub.exists() else []
    search_dirs.append(backup_dir)

    results: list[BackupInfo] = []
    seen_filenames = set()

    for s_dir in search_dirs:
        for item in s_dir.iterdir():
            if item.is_file() and item.suffix.lower() == ".db" and item.name not in seen_filenames:
                try:
                    stat = item.stat()
                    created_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    results.append(
                        BackupInfo(
                            filename=item.name,
                            path=item,
                            size_bytes=stat.st_size,
                            size_mb=round(stat.st_size / (1024 * 1024), 2),
                            created_at=created_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                            created_ts=stat.st_mtime,
                        )
                    )
                    seen_filenames.add(item.name)
                except OSError:
                    continue

    results.sort(key=lambda b: b.created_ts, reverse=True)
    return results


def prune_backups(backup_dir: Path, keep_latest: int = 3) -> list[str]:
    """Delete older backup files, keeping the latest keep_latest backups.
    Returns list of deleted filenames.
    """
    backups = list_backups(backup_dir)
    if len(backups) <= keep_latest:
        return []

    to_delete = backups[keep_latest:]
    deleted_names: list[str] = []

    for b in to_delete:
        try:
            if b.path.exists():
                b.path.unlink()
                deleted_names.append(b.filename)
                # Also clean up any lingering wal/shm sidecars for this backup
                for sidecar in (b.path.with_suffix(".db-wal"), b.path.with_suffix(".db-shm")):
                    if sidecar.exists():
                        sidecar.unlink()
        except OSError:
            pass

    return deleted_names


def delete_backup(backup_dir: Path, filename: str) -> bool:
    """Safely delete a specific backup file in backup_dir or its wealthfolio subfolder.
    Returns True if deleted, False otherwise.
    """
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return False

    if not filename.lower().endswith(".db"):
        return False

    # Check both <backup_dir>/wealthfolio/ and <backup_dir>
    wf_sub = get_wealthfolio_backup_dir(backup_dir)
    possible_targets = [wf_sub / filename, backup_dir / filename]

    for candidate in possible_targets:
        target = candidate.resolve()
        # Path traversal protection
        try:
            resolved_dir = backup_dir.resolve()
            target.relative_to(resolved_dir)
        except ValueError:
            continue

        if target.exists() and target.is_file():
            try:
                target.unlink()
                for sidecar in (target.with_suffix(".db-wal"), target.with_suffix(".db-shm")):
                    if sidecar.exists():
                        sidecar.unlink()
                return True
            except OSError:
                return False

    return False


def list_mapping_backups(mapping_backup_dir: Path) -> list[BackupInfo]:
    """List all JSON mapping backup files in mapping_backup_dir, sorted newest first."""
    if not mapping_backup_dir.exists():
        return []

    results: list[BackupInfo] = []
    for item in mapping_backup_dir.iterdir():
        if item.is_file() and item.suffix.lower() == ".json":
            try:
                stat = item.stat()
                created_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                results.append(
                    BackupInfo(
                        filename=item.name,
                        path=item,
                        size_bytes=stat.st_size,
                        size_mb=round(stat.st_size / (1024 * 1024), 2),
                        created_at=created_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                        created_ts=stat.st_mtime,
                    )
                )
            except OSError:
                continue

    results.sort(key=lambda b: b.created_ts, reverse=True)
    return results


def backup_mapping_file(mapping_path: Path, mapping_backup_dir: Path) -> Path | None:
    """Create a timestamped backup of the mapping file in mapping_backup_dir.
    Returns the Path to the created backup file, or None if mapping_path does not exist.
    """
    if not mapping_path.exists():
        return None

    mapping_backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = mapping_backup_dir / f"mapping_{ts}.json"
    import shutil
    shutil.copy2(mapping_path, backup_file)
    prune_mapping_backups(mapping_backup_dir, keep_latest=20)
    return backup_file


def prune_mapping_backups(mapping_backup_dir: Path, keep_latest: int = 20) -> list[str]:
    """Delete older mapping backup JSON files, keeping the latest keep_latest backups.
    Returns list of deleted filenames.
    """
    backups = list_mapping_backups(mapping_backup_dir)
    if len(backups) <= keep_latest:
        return []

    to_delete = backups[keep_latest:]
    deleted_names: list[str] = []

    for b in to_delete:
        try:
            if b.path.exists():
                b.path.unlink()
                deleted_names.append(b.filename)
        except OSError:
            pass

    return deleted_names

