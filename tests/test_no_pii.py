import os
import re
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).parent.parent

# Case-insensitive forbidden tokens to prevent personal PII regression
FORBIDDEN_PATTERNS = [
    re.compile(r"healey\.gnucash", re.IGNORECASE),
    re.compile(r"trentan", re.IGNORECASE),
    re.compile(r"\bjanet\b", re.IGNORECASE),
    re.compile(r"\bcharlie\b", re.IGNORECASE),
    re.compile(r"\bphoebe\b", re.IGNORECASE),
    re.compile(r"\brebecca\b", re.IGNORECASE),
    re.compile(r"\bjoyner\b", re.IGNORECASE),
    re.compile(r"\bforsyth\b", re.IGNORECASE),
    re.compile(r"\bcharnwood\b", re.IGNORECASE),
    re.compile(r"\btodds\b", re.IGNORECASE),
    re.compile(r"\bpikki\b", re.IGNORECASE),
    re.compile(r"\bsanctum\b", re.IGNORECASE),
    re.compile(r"\bjaecoo\b", re.IGNORECASE),
]

# File patterns and directories to scan
TRACKED_EXTENSIONS = {".py", ".json", ".html", ".js", ".css", ".md", ".bat", ".ps1", ".sh"}
EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", ".pytest_cache", "__pycache__", "backups", "build", "dist"}
# Note: pyproject.toml has { name = "Trentan Healey" } which is the project author metadata.
# We exempt pyproject.toml from the author first-name scan.
EXEMPT_FILES = {"pyproject.toml", "test_no_pii.py"}


def test_zero_pii_across_repo():
    """Verify that zero PII or private ledger addresses exist in git-tracked repo files."""
    import subprocess
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True
    )
    tracked_files = result.stdout.splitlines()
    violations = []

    for rel_path in tracked_files:
        path = REPO_ROOT / rel_path
        if path.name in EXEMPT_FILES:
            continue
        if path.suffix.lower() not in TRACKED_EXTENSIONS:
            continue
        if not path.is_file():
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for pattern in FORBIDDEN_PATTERNS:
            matches = pattern.findall(content)
            if matches:
                violations.append(f"{rel_path}: Matched forbidden pattern '{pattern.pattern}' ({len(matches)} occurrences)")

    assert not violations, "PII or hardcoded ledger data detected in git-tracked files:\n" + "\n".join(violations)
