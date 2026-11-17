import json
import os
from pathlib import Path
import shutil
import sys

DEFAULT_MAPPING_PATH: Path = Path("config/mapping.json")
DEFAULT_BACKUP_DIR: Path = Path("backups")
DEFAULT_TARGET_DB_PATH: Path = Path("output/app_migrated.db")
DEFAULT_REPORT_PATH: Path = Path("output/migration_report.md")
DEFAULT_SETTINGS_PATH: Path = Path("config/app_settings.json")


def load_app_settings(settings_path: Path = DEFAULT_SETTINGS_PATH) -> dict:
    """Load persistent app settings from JSON, returning empty dict if missing or invalid."""
    if not settings_path.exists():
        return {}
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_app_settings(settings: dict, settings_path: Path = DEFAULT_SETTINGS_PATH) -> None:
    """Save persistent app settings to JSON, creating parent directories if needed."""
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        existing = load_app_settings(settings_path)
        existing.update(settings)
        settings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"Warning: Failed to save app settings to {settings_path}: {e}", file=sys.stderr)


def get_active_backup_dir(cli_override: Path | None = None) -> Path:
    """Resolve active backup root directory: CLI arg -> app_settings.json -> default."""
    if cli_override and cli_override != DEFAULT_BACKUP_DIR:
        return cli_override
    settings = load_app_settings()
    if "backup_dir" in settings and settings["backup_dir"]:
        return Path(settings["backup_dir"])
    return cli_override or DEFAULT_BACKUP_DIR


def get_active_mapping_path(cli_override: Path | None = None) -> Path:
    """Resolve active mapping path: CLI arg -> app_settings.json -> default."""
    if cli_override and cli_override != DEFAULT_MAPPING_PATH:
        return cli_override
    settings = load_app_settings()
    if "mapping_path" in settings and settings["mapping_path"]:
        return Path(settings["mapping_path"])
    return cli_override or DEFAULT_MAPPING_PATH


def get_active_gnucash_path(cli_override: Path | None = None) -> Path | None:
    """Resolve active GnuCash file path: CLI arg -> app_settings.json -> auto-detect default."""
    settings = load_app_settings()
    stored_gnucash = Path(settings["gnucash_path"]) if "gnucash_path" in settings and settings["gnucash_path"] else None

    # If a specific CLI override was provided (not equal to the auto-detect default), use it
    local_default = get_default_gnucash_file() or Path("data.gnucash")
    if cli_override and cli_override != local_default:
        return cli_override

    # Otherwise prefer the stored path from app_settings.json if it exists
    if stored_gnucash and stored_gnucash.exists():
        return stored_gnucash

    return cli_override or stored_gnucash or local_default


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
