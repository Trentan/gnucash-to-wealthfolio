import argparse
from datetime import datetime
from pathlib import Path
import shutil
import sys

from gnucash_to_wealthfolio.config import (
    DEFAULT_BACKUP_DIR,
    DEFAULT_MAPPING_PATH,
    DEFAULT_REPORT_PATH,
    DEFAULT_TARGET_DB_PATH,
    get_default_gnucash_file,
    get_default_wealthfolio_db_path,
    get_wealthfolio_executable_path,
)
from gnucash_to_wealthfolio.mapper import (
    build_mapping_config,
    load_mapping_config,
    save_mapping_config,
)
from gnucash_to_wealthfolio.migrator import run_migration
from gnucash_to_wealthfolio.parser import parse_gnucash_file
from gnucash_to_wealthfolio.verifier import verify_migration, write_markdown_report

DEFAULT_GNUCASH = get_default_gnucash_file() or Path("data.gnucash")
DEFAULT_MAPPING = DEFAULT_MAPPING_PATH
DEFAULT_SOURCE_DB = get_default_wealthfolio_db_path()
DEFAULT_TARGET_DB = DEFAULT_TARGET_DB_PATH
DEFAULT_REPORT = DEFAULT_REPORT_PATH
WEALTHFOLIO_EXE = get_wealthfolio_executable_path()



def _do_run_all(args) -> int:
    """Parse → map → migrate → verify. Returns 0 on success."""
    print("========================================================================")
    print("  [!] DESTRUCTIVE SYNC NOTICE:")
    print("      This operation will WIPE all prior GnuCash-sourced accounts,")
    print("      transactions, splits, and auto-generated rules from Wealthfolio.")
    print("      Existing brokerage and investment accounts remain intact.")
    print(f"      A pre-migration backup will be saved to: {args.backup_dir}")
    print("========================================================================")

    # Check existing backups advisory
    from gnucash_to_wealthfolio.backups import list_backups
    backups = list_backups(args.backup_dir)
    if len(backups) > 5:
        total_mb = sum(b.size_mb for b in backups)
        print(f"      ADVISORY: {len(backups)} backups exist ({total_mb:.1f} MB in {args.backup_dir}).")
        print(f"                Consider running 'edit-mappings.bat' to prune older backups.")

    print(f"\n[1/4] Parsing {args.gnucash}...")
    book = parse_gnucash_file(args.gnucash)
    print(f"      Parsed {len(book.accounts)} accounts and {len(book.transactions):,} transactions.")

    print(f"[2/4] Building/updating mapping configuration -> {args.mapping}...")
    existing_cfg = load_mapping_config(args.mapping) if args.mapping.exists() else None
    cfg = build_mapping_config(book, existing_config=existing_cfg)
    save_mapping_config(cfg, args.mapping)
    cfg = load_mapping_config(args.mapping)

    # Check for truly unmapped accounts (empty category_id)
    unmapped_accounts = [
        c for c in cfg.categories.values()
        if (not c.category_id or c.category_id == "cat_unmapped")
        and cfg.accounts.get(c.gnc_guid) and cfg.accounts[c.gnc_guid].tx_count > 0
    ]
    if unmapped_accounts:
        print(f"      NOTE: {len(unmapped_accounts)} active accounts are unmapped.")
        print(f"            Run 'edit-mappings.bat' or 'python -m gnucash_to_wealthfolio.cli edit' to assign them.")

    print(f"[3/4] Running SQLite migration -> {args.target_db} (backing up to {args.backup_dir})...")
    stats = run_migration(book, cfg, args.source_db, args.target_db, args.backup_dir)
    print(
        f"      Created {stats.accounts_created} accounts, {stats.categories_created} categories, "
        f"{stats.activities_created:,} activities, {stats.activity_splits_created} splits, "
        f"and {stats.rules_created} categorization rules."
    )

    print(f"[4/4] Verifying cent-exact balances and writing report -> {args.report}...")
    report = verify_migration(book, cfg, args.source_db, args.target_db)
    write_markdown_report(report, stats, cfg, args.report)

    max_diff = max(abs(r.diff) for r in report.reconciliations) if report.reconciliations else 0.0
    print(
        f"      Verification complete: max_diff=${max_diff:.2f}, "
        f"pre_existing_intact={report.pre_existing_accounts_intact}, "
        f"fk_violations={report.fk_violations}, integrity_ok={report.integrity_ok}"
    )
    ok = (
        max_diff == 0
        and report.pre_existing_accounts_intact
        and report.fk_violations == 0
        and report.integrity_ok
    )
    return 0 if ok else 1


def launch_wealthfolio(exe_path: Path | None = None) -> bool:
    """Safely launch the Wealthfolio desktop application.

    1. Terminates any running or hung Wealthfolio instances and child WebView2 processes
       to prevent PROFILE_UNAVAILABLE or WebView2 lock collisions (0x800700AA).
    2. Launches cleanly through the Windows Shell (os.startfile) on Windows so that it opens
       interactively on the user's desktop rather than being trapped in a background daemon environment.
    """
    import os
    import subprocess
    import sys
    import time

    target = exe_path or WEALTHFOLIO_EXE
    if not target or not target.exists():
        return False

    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/IM", "Wealthfolio.exe"], capture_output=True)
            time.sleep(1)
        except Exception:
            pass
        try:
            try:
                import psutil
                for p in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        cmd = " ".join(p.info["cmdline"] or []).lower()
                        if "com.teymz.wealthfolio" in cmd:
                            p.kill()
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.5)
            os.startfile(str(target))
            return True
        except Exception as e:
            print(f"Failed to startfile {target}: {e}")
            return False
    elif sys.platform == "darwin":
        try:
            subprocess.run(["pkill", "-f", "Wealthfolio"], capture_output=True)
            time.sleep(0.5)
            subprocess.run(["open", "-a", "Wealthfolio"])
            return True
        except Exception as e:
            print(f"Failed to open Wealthfolio: {e}")
            return False
    else:
        try:
            subprocess.run(["pkill", "-f", "wealthfolio"], capture_output=True)
            time.sleep(0.5)
            subprocess.Popen([str(target)], start_new_session=True)
            return True
        except Exception as e:
            print(f"Failed to spawn {target}: {e}")
            return False


def _do_install(migrated_db: Path, live_db: Path, backup_dir: Path, launch: bool) -> int:
    """Copy migrated DB over live DB (safely deleting WAL files), optionally relaunch Wealthfolio."""
    import subprocess
    import time

    # Terminate running Wealthfolio to release SQLite locks
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/IM", "Wealthfolio.exe"], capture_output=True)
        else:
            subprocess.run(["pkill", "-f", "Wealthfolio"], capture_output=True)
        time.sleep(1)
    except Exception:
        pass

    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"app_pre_install_{ts}.db"

    # Backup the live DB before overwriting
    if live_db.exists():
        shutil.copy2(live_db, backup_file)
        print(f"      Backed up live DB -> {backup_file}")

    # Remove WAL/SHM sidecars so SQLite starts clean
    for sidecar in (live_db.with_suffix(".db-wal"), live_db.with_suffix(".db-shm")):
        if sidecar.exists():
            sidecar.unlink()
            print(f"      Removed {sidecar.name}")

    shutil.copy2(migrated_db, live_db)
    print(f"      Installed {migrated_db} -> {live_db}")

    if launch:
        if launch_wealthfolio(WEALTHFOLIO_EXE):
            target_name = WEALTHFOLIO_EXE.name if WEALTHFOLIO_EXE else "Wealthfolio"
            print(f"      Launched {target_name}")
        else:
            print(f"      WARNING: Wealthfolio not found at {WEALTHFOLIO_EXE} — open it manually.")

    return 0



def _do_edit(args) -> int:
    from gnucash_to_wealthfolio.editor_server import start_editor_server
    print(f"Loading GnuCash file: {args.gnucash}...")
    book = parse_gnucash_file(args.gnucash)

    # Ensure mapping file exists or build initial config
    if not args.mapping.exists():
        print(f"Building initial mapping configuration -> {args.mapping}...")
        cfg = build_mapping_config(book)
        save_mapping_config(cfg, args.mapping)
    else:
        existing_cfg = load_mapping_config(args.mapping)
        cfg = build_mapping_config(book, existing_config=existing_cfg)
        save_mapping_config(cfg, args.mapping)

    start_editor_server(
        book=book,
        mapping_path=args.mapping,
        source_db=args.source_db,
        target_db=args.target_db,
        backup_dir=args.backup_dir,
        port=args.port,
        open_browser=not getattr(args, "no_browser", False),
        gnucash_path=args.gnucash,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GnuCash to Wealthfolio Spending Migrator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --- edit ---
    p_edit = sub.add_parser("edit", help="Launch interactive browser editor for category mappings and backups")
    p_edit.add_argument("--gnucash", type=Path, default=DEFAULT_GNUCASH)
    p_edit.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    p_edit.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    p_edit.add_argument("--target-db", type=Path, default=DEFAULT_TARGET_DB)
    p_edit.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    p_edit.add_argument("--port", type=int, default=8765)
    p_edit.add_argument("--no-browser", action="store_true", default=False)

    # --- run-all ---
    p_run = sub.add_parser("run-all", help="Parse, generate mapping, migrate to staging DB, and verify")
    p_run.add_argument("--gnucash", type=Path, default=DEFAULT_GNUCASH)
    p_run.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    p_run.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    p_run.add_argument("--target-db", type=Path, default=DEFAULT_TARGET_DB)
    p_run.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    p_run.add_argument("--report", type=Path, default=DEFAULT_REPORT)

    # --- install ---
    p_inst = sub.add_parser("install", help="Promote verified output/app_migrated.db to live Wealthfolio app.db")
    p_inst.add_argument("--migrated-db", type=Path, default=DEFAULT_TARGET_DB)
    p_inst.add_argument("--live-db", type=Path, default=DEFAULT_SOURCE_DB)
    p_inst.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    p_inst.add_argument(
        "--launch", action="store_true", default=False,
        help="Relaunch Wealthfolio after install",
    )

    # --- sync (run-all + install in one shot) ---
    p_sync = sub.add_parser(  # noqa: F841
        "sync",
        help="Full sync: parse GnuCash, migrate, verify, then deploy to live Wealthfolio DB",
    )
    p_sync.add_argument("--gnucash", type=Path, default=DEFAULT_GNUCASH)
    p_sync.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    p_sync.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    p_sync.add_argument("--target-db", type=Path, default=DEFAULT_TARGET_DB)
    p_sync.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    p_sync.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    p_sync.add_argument(
        "--launch", action="store_true", default=False,
        help="Relaunch Wealthfolio after deploy",
    )
    p_sync.add_argument(
        "--ui", action="store_true", default=False,
        help="Launch interactive browser editor instead of direct sync",
    )
    p_sync.add_argument("--port", type=int, default=8765)
    p_sync.add_argument("--no-browser", action="store_true", default=False)

    args = parser.parse_args(argv)

    if args.cmd == "edit":
        return _do_edit(args)

    if args.cmd == "run-all":
        return _do_run_all(args)

    if args.cmd == "install":
        print("[install] Deploying migrated DB to Wealthfolio...")
        return _do_install(args.migrated_db, args.live_db, args.backup_dir, args.launch)

    if args.cmd == "sync":
        if args.ui:
            return _do_edit(args)

        print("=== SYNC: Step 1/2 — run-all ===")
        rc = _do_run_all(args)
        if rc != 0:
            print("ERROR: Migration or verification failed — aborting deploy. Live DB unchanged.")
            return rc
        print("\n=== SYNC: Step 2/2 — install ===")
        return _do_install(args.target_db, args.source_db, args.backup_dir, args.launch)

    return 1


if __name__ == "__main__":
    sys.exit(main())
