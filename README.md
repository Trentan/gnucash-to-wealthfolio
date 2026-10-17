# GnuCash to Wealthfolio Migration Tool

A high-fidelity migration utility that imports 20+ years of personal banking, credit card, and expense transaction history from **GnuCash** (gzipped XML) into **Wealthfolio**'s local SQLite database (`app.db`), preserving cent-exact balances, custom multi-level categories, and split transactions while leaving existing investment accounts and activities intact.

---

## Features

- **Cent-Exact Reconciliation:** Verifies that every cash and credit card account balance matches GnuCash down to `$0.00` difference across tens of thousands of transactions.
- **Investment Preservation:** Keeps existing investment accounts, brokerage cash balances, and equity activities completely untouched.
- **Hybrid Category Hierarchy:** Preserves deep GnuCash category granularity (including specific business/store subcategories like `Supermarket › Woolworths` or `Gardening & Tools › Bunnings`) while conforming to Wealthfolio's 2-level UI hierarchy.
- **Split Transaction Handling:** Preserves multi-category splits (`spending_activity_splits`) and auto-assigns budget groups (`Needs`, `Wants`, `Savings`, `Personal`, etc.).
- **Automatic Rule Mining:** Generates regex/keyword categorization rules based on recurring historical payees for automated future imports.
- **Internal Transfer Pairing:** Links inter-account transfers via `source_group_id` so repayments and movements aren't double-counted in spending analytics.
- **Safe Staging & Automatic Backups:** Performs all transformations in a staging database (`output/app_migrated.db`) with automatic SQLite foreign key and integrity verification before deploying to live database. Live databases are timestamp-backed up before any replacement.

---

## Directory Structure

```text
gnucash-to-wealthfolio/
├── config/
│   └── mapping.json          # GnuCash GUID to Wealthfolio account/category mappings
├── gnucash_to_wealthfolio/
│   ├── __init__.py
│   ├── cli.py                # Command-line interface (run-all, install, sync)
│   ├── ids.py                # Deterministic UUID / GUID generation utilities
│   ├── mapper.py             # Category & account taxonomy classification logic
│   ├── migrator.py           # SQLite insertion engine and transaction migrator
│   ├── models.py             # Data models for accounts, transactions, and splits
│   ├── parser.py             # Streaming GnuCash XML/gzip parser
│   └── verifier.py           # Balance reconciliation and report generator
├── tests/                    # Unit and end-to-end reconciliation tests
├── sync-wealthfolio.bat      # Windows batch file to double-click and run sync
├── sync-wealthfolio.ps1      # PowerShell one-click migration & sync helper
├── .gitignore                # Strictly excludes personal databases and backups
└── README.md
```

---

## Requirements

- **Python 3.10+** (uses standard library modules: `sqlite3`, `xml.etree`, `gzip`, `argparse`, `decimal`, etc.)
- **Wealthfolio Desktop** (Windows)
- **PowerShell** (for `sync-wealthfolio.ps1`)

---

## Quick Start

### 1. Interactive Mapping & Backups Editor (Web UI)

To inspect your GnuCash accounts, reassign categories (especially those under "Other Expenses"), or manage backups:
- **Windows Explorer:** Double-click **`edit-mappings.bat`**
- **Command Line:** Run:
  ```bash
  python -m gnucash_to_wealthfolio.cli edit
  ```
This starts a lightweight local server at `http://127.0.0.1:8765` and opens your default browser. Any customized mappings are saved directly to `config/mapping.json` and are permanently preserved across future imports. You can also trigger the migration and verify reconciliation directly from the web interface.

---

### 2. Destructive Sync Launcher (Wipe & Re-Import)

> [!WARNING]
> **Destructive Sync Notice**: Running sync will **wipe and reinsert** all prior GnuCash-sourced accounts, transactions, splits, and auto-generated rules from Wealthfolio, replacing them with the latest mapped data. Your manual brokerage/investment portfolios remain untouched. An automatic timestamped backup is saved to `backups/` before any changes are made to the live database.

- **Windows Explorer:** Double-click **`sync-wealthfolio.bat`** to run the full sync, verify cent-exact balances, deploy to Wealthfolio, and relaunch the app.
- **PowerShell:**
  ```powershell
  # Perform a dry run (migrates to staging DB & verifies balances; does NOT touch live DB)
  .\sync-wealthfolio.ps1 -DryRun

  # Launch interactive browser editor first before syncing
  .\sync-wealthfolio.ps1 -UI

  # Full migration + live database deployment + auto-launch Wealthfolio
  .\sync-wealthfolio.ps1
  ```

---

### 3. Python CLI

The CLI provides fine-grained control over individual stages:

#### Interactive Editor (`edit`)
Launches the local browser UI to edit category mappings and prune backups:
```bash
python -m gnucash_to_wealthfolio.cli edit
```

#### Dry Run / Staging (`run-all`)
Parses GnuCash, loads/updates `config/mapping.json` (preserving user customizations), transforms records into `output/app_migrated.db`, and validates reconciliation into `output/migration_report.md`:
```bash
python -m gnucash_to_wealthfolio.cli run-all
```

#### Deploy Staged DB to Live Wealthfolio (`install`)
Backs up live `app.db` into `backups/app_pre_install_<timestamp>.db`, removes SQLite WAL/SHM sidecars, copies `output/app_migrated.db` to live `%APPDATA%\com.teymz.wealthfolio\app.db`, and optionally relaunches the application:
```bash
python -m gnucash_to_wealthfolio.cli install --launch
```

#### Full End-to-End Pipeline (`sync`)
Combines `run-all` and `install` in one transactional step. If verification fails or balance discrepancies exist, deployment is automatically aborted:
```bash
python -m gnucash_to_wealthfolio.cli sync --launch

# Or launch the interactive Web UI first:
python -m gnucash_to_wealthfolio.cli sync --ui
```

---

## CLI Options & Custom Paths

You can pass custom file paths to any command:

| Argument | Default | Description |
|---|---|---|
| `--gnucash` | `data.gnucash` | Path to source GnuCash file |
| `--mapping` | `config/mapping.json` | Path to category/account mapping JSON |
| `--source-db` | `%APPDATA%\com.teymz.wealthfolio\app.db` | Path to live Wealthfolio SQLite database |
| `--target-db` | `output/app_migrated.db` | Staging database destination |
| `--backup-dir`| `backups/` | Directory where timestamped backups are stored |
| `--report` | `output/migration_report.md` | Path to generated markdown reconciliation report |
| `--launch` | `False` | Automatically relaunch Wealthfolio after installation |

---

## Running Tests

Run the test suite using Python's built-in `unittest` runner:

```bash
python -m unittest discover tests
```

---

## Privacy & Security

Personal financial data files (such as `*.gnucash`, `backups/`, `output/`, and all `*.db` / SQLite files) are excluded via `.gitignore` to prevent committing sensitive transaction and balance records to version control.
