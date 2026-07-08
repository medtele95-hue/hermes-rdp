# Lance scripts/weekly_snapshot.py avec le PATH Python correct.
# Tache planifiee HERMES_WEEKLY_SNAPSHOT, dimanche 12:00.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$env:PATH = "C:\Tools\MinGit\cmd;$env:LOCALAPPDATA\Programs\Python\Python311;$env:PATH"
try {
    python "$PSScriptRoot\weekly_snapshot.py" 2>&1 | Tee-Object -FilePath "C:\hermes-reports\_weekly_snapshot_console.log" -Append
} catch {
    Add-Content -Path "C:\hermes-reports\_automation_health.log" -Value "$(Get-Date -Format o) [weekly_snapshot] RUNNER_EXCEPTION $_"
}
