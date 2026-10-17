# sync-wealthfolio.ps1
# ---------------------------------------------------------------------------
# Re-syncs GnuCash data into Wealthfolio in one shot.
#
# Usage:
#   .\sync-wealthfolio.ps1            # migrate + deploy, relaunch Wealthfolio
#   .\sync-wealthfolio.ps1 -DryRun    # migrate + verify only, don't deploy
#
# Prerequisites:
#   - Wealthfolio must be CLOSED before running this script.
#   - Place your .gnucash file in the project root or pass via CLI arguments.
# ---------------------------------------------------------------------------

param(
    [switch]$DryRun,
    [switch]$UI
)

$ErrorActionPreference = "Stop"
$ProjectDir = $PSScriptRoot

Write-Host ""
Write-Host "=== GnuCash -> Wealthfolio Sync ===" -ForegroundColor Cyan
Write-Host ""

# Warn if Wealthfolio appears to be running
$wf = Get-Process -Name "Wealthfolio" -ErrorAction SilentlyContinue
if ($wf) {
    Write-Host "ERROR: Wealthfolio is currently running. Please close it first." -ForegroundColor Red
    exit 1
}

Set-Location $ProjectDir

if ($UI) {
    Write-Host "Launching interactive mapping and backups editor..." -ForegroundColor Cyan
    python -m gnucash_to_wealthfolio.cli sync --ui
    exit $LASTEXITCODE
}

if ($DryRun) {
    Write-Host "DRY RUN: will migrate + verify but NOT deploy to live DB." -ForegroundColor Yellow
    Write-Host ""
    python -m gnucash_to_wealthfolio.cli run-all
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Migration/verification FAILED (exit $LASTEXITCODE)." -ForegroundColor Red
        exit $LASTEXITCODE
    }
    Write-Host ""
    Write-Host "Dry run complete. Check output\migration_report.md for details." -ForegroundColor Green
} else {
    python -m gnucash_to_wealthfolio.cli sync --launch
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Sync FAILED (exit $LASTEXITCODE). Live DB was NOT modified." -ForegroundColor Red
        exit $LASTEXITCODE
    }
    Write-Host ""
    Write-Host "Sync complete! Wealthfolio has been relaunched." -ForegroundColor Green
    Write-Host "Check output\migration_report.md for the full reconciliation report." -ForegroundColor Green
}
