# Lance le watchdog HERMES (boucle infinie, relance auto en cas de crash).
# Utilisé par la tâche planifiée HERMES_WATCHDOG (au boot) — peut aussi être
# lancé à la main :  powershell -ExecutionPolicy Bypass -File watchdog\start_watchdog.ps1
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
while ($true) {
    $stamp = (Get-Date).ToString("s")
    Add-Content -Path "$PSScriptRoot\watchdog_runner.log" -Value "$stamp [RUNNER] demarrage du watchdog"
    # V5.1 / correctif C1 (2026-07-17) - log du watchdog SEPARE de celui du bot.
    # Meme motif que scripts/bot_supervisor.ps1:66 et scripts/dashboard_supervisor.ps1 (A1).
    #
    # Pourquoi : hermes_watchdog.py:72 importe app.mt5.demo_router (pour 3 constantes),
    # qui importe app.logger:17, dont la ligne 70 (`log = configure_logging()`) cree un
    # RotatingFileHandler sur logs/hermes.log DES L'IMPORT. Le watchdog n'ecrit jamais
    # dans ce log, mais il en tenait un handle PERMANENT (audit A1.1 : 7/7 echantillons),
    # ce qui faisait echouer os.rename() cote bot (WinError 32) : 0 rotation en 9,45 j,
    # 1770 Mo = 177x le seuil de 10 Mo.
    #
    # Portee : ce $env: n'affecte QUE ce process PowerShell et son enfant python
    # (hermes_watchdog.py). Le bot et le dashboard ont chacun leur propre lanceur qui
    # fixe leur propre HERMES_LOG_FILE : aucune fuite possible.
    # La surveillance de hermes.log par le watchdog continue de fonctionner : elle
    # utilise f.stat().st_mtime (metadonnee), pas une ouverture de fichier.
    # Rollback : supprimer la ligne $env:HERMES_LOG_FILE ci-dessous.
    $env:HERMES_LOG_FILE = Join-Path $repo "logs\hermes-watchdog.log"
    & python "$PSScriptRoot\hermes_watchdog.py" 2>&1 |
        Tee-Object -FilePath "$PSScriptRoot\watchdog_console.log" -Append | Out-Null
    $stamp = (Get-Date).ToString("s")
    Add-Content -Path "$PSScriptRoot\watchdog_runner.log" -Value "$stamp [RUNNER] watchdog termine (code $LASTEXITCODE) - relance dans 30s"
    Start-Sleep -Seconds 30
}
