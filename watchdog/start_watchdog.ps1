# Lance le watchdog HERMES (boucle infinie, relance auto en cas de crash).
# Utilisé par la tâche planifiée HERMES_WATCHDOG (au boot) — peut aussi être
# lancé à la main :  powershell -ExecutionPolicy Bypass -File watchdog\start_watchdog.ps1
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
while ($true) {
    $stamp = (Get-Date).ToString("s")
    Add-Content -Path "$PSScriptRoot\watchdog_runner.log" -Value "$stamp [RUNNER] demarrage du watchdog"
    & python "$PSScriptRoot\hermes_watchdog.py" 2>&1 |
        Tee-Object -FilePath "$PSScriptRoot\watchdog_console.log" -Append | Out-Null
    $stamp = (Get-Date).ToString("s")
    Add-Content -Path "$PSScriptRoot\watchdog_runner.log" -Value "$stamp [RUNNER] watchdog termine (code $LASTEXITCODE) - relance dans 30s"
    Start-Sleep -Seconds 30
}
