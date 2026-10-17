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


def list_backups(backup_dir: Path) -> list[BackupInfo]:
    """List all SQLite database backup files in backup_dir, sorted newest first."""
    if not backup_dir.exists():
        return []

    results: list[BackupInfo] = []
    for item in backup_dir.iterdir():
        if item.is_file() and item.suffix.lower() == ".db":
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
    """Safely delete a specific backup file in backup_dir.
    Returns True if deleted, False otherwise.
    """
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return False

    if not filename.lower().endswith(".db"):
        return False

    target = (backup_dir / filename).resolve()
    # Path traversal protection
    try:
        resolved_dir = backup_dir.resolve()
        target.relative_to(resolved_dir)
    except ValueError:
        return False

    if not target.exists() or not target.is_file():
        return False

    try:
        target.unlink()
        for sidecar in (target.with_suffix(".db-wal"), target.with_suffix(".db-shm")):
            if sidecar.exists():
                sidecar.unlink()
        return True
    except OSError:
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

