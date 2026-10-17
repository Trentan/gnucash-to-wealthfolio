import os
from pathlib import Path
import shutil
import sys

DEFAULT_MAPPING_PATH: Path = Path("config/mapping.json")
DEFAULT_BACKUP_DIR: Path = Path("backups")
DEFAULT_TARGET_DB_PATH: Path = Path("output/app_migrated.db")
DEFAULT_REPORT_PATH: Path = Path("output/migration_report.md")


def _get_home_dir() -> Path:
    try:
        return Path.home()
    except Exception:
        # Fallback when os.environ is cleared in tests or HOME is not set
        return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or "~")


def get_default_wealthfolio_db_path() -> Path:
    """Auto-detect Wealthfolio's local SQLite database across platforms."""
    # 1. Allow explicit environment variable override
    env_override = os.environ.get("WEALTHFOLIO_DB_PATH")
    if env_override:
        p = Path(env_override)
        if p.exists():
            return p

    home = _get_home_dir()
    system = sys.platform

    candidates: list[Path] = []
    if system == "win32":
        appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
        localappdata = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        candidates = [
            appdata / "com.teymz.wealthfolio" / "app.db",
            localappdata / "com.teymz.wealthfolio" / "app.db",
            localappdata / "Wealthfolio" / "app.db",
        ]
    elif system == "darwin":  # macOS
        candidates = [
            home / "Library" / "Application Support" / "com.teymz.wealthfolio" / "app.db",
            home / "Library" / "Application Support" / "Wealthfolio" / "app.db",
        ]
    else:  # Linux / BSD
        xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        xdg_data = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
        candidates = [
            xdg_config / "com.teymz.wealthfolio" / "app.db",
            xdg_data / "com.teymz.wealthfolio" / "app.db",
            home / ".config" / "wealthfolio" / "app.db",
        ]

    for c in candidates:
        if c.exists():
            return c

    # Fallback to standard platform default even if not yet created
    return candidates[0] if candidates else Path("app.db")


def get_wealthfolio_executable_path() -> Path | None:
    """Find the Wealthfolio desktop executable across platforms."""
    env_override = os.environ.get("WEALTHFOLIO_EXE_PATH")
    if env_override and Path(env_override).exists():
        return Path(env_override)

    system = sys.platform
    home = _get_home_dir()

    candidates: list[Path] = []
    if system == "win32":
        localappdata = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        progfiles = Path(os.environ.get("PROGRAMFILES") or r"C:\Program Files")

        candidates = [
            localappdata / "Wealthfolio" / "Wealthfolio.exe",
            localappdata / "Programs" / "Wealthfolio" / "Wealthfolio.exe",
            progfiles / "Wealthfolio" / "Wealthfolio.exe",
        ]
    elif system == "darwin":
        candidates = [
            Path("/Applications/Wealthfolio.app/Contents/MacOS/Wealthfolio"),
            home / "Applications" / "Wealthfolio.app" / "Contents" / "MacOS" / "Wealthfolio",
        ]
    else:
        which = shutil.which("wealthfolio")
        return Path(which) if which else None

    for c in candidates:
        if c.exists():
            return c
    return None


def get_default_gnucash_file(search_dir: Path = Path(".")) -> Path | None:
    """Auto-detect the first *.gnucash file in search_dir if one exists."""
    matches = list(search_dir.glob("*.gnucash"))
    return matches[0] if matches else None
