# Lance scripts/daily_report.py avec le PATH Python correct.
# Tache planifiee HERMES_DAILY_REPORT, chaque jour a 21:30.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$env:PATH = "C:\Tools\MinGit\cmd;$env:LOCALAPPDATA\Programs\Python\Python311;$env:PATH"
try {
    python "$PSScriptRoot\daily_report.py" 2>&1 | Tee-Object -FilePath "C:\hermes-reports\_daily_report_console.log" -Append
} catch {
    Add-Content -Path "C:\hermes-reports\_automation_health.log" -Value "$(Get-Date -Format o) [daily_report] RUNNER_EXCEPTION $_"
}
