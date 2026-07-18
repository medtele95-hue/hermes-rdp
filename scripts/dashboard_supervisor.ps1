# HERMES dashboard supervisor - mission/DASHBOARD.md.
# Process INDEPENDANT du bot ET du bot_supervisor (meme motif que
# scripts/bot_supervisor.ps1). Verifie toutes les 60s: le process
# app.dashboard_api.server tourne-t-il ? Redemarre si absent, avec
# anti-boucle (max 3 restarts/heure) + alerte Telegram + log [AUTO_RESTART].
# Tache planifiee HERMES_DASHBOARD_SUPERVISOR, declencheur au boot.
#
# Ce superviseur ne touche JAMAIS au bot lui-meme (app.main) - il ne gere
# QUE le process dashboard. Si le dashboard meurt, le bot ne le sent pas et
# continue de trader normalement (mission: "process separe").
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$stateFile = Join-Path $repo "logs\dashboard_supervisor_state.json"
$logFile = Join-Path $repo "logs\dashboard_supervisor.log"
New-Item -ItemType Directory -Force (Join-Path $repo "logs") | Out-Null

function Write-SupLog($msg) {
    $line = "$(Get-Date -Format o) $msg"
    Add-Content -Path $logFile -Value $line -Encoding utf8
    Write-Output $line
}

function Get-State {
    if (Test-Path $stateFile) {
        try { return (Get-Content $stateFile -Raw | ConvertFrom-Json) } catch { }
    }
    return [PSCustomObject]@{ dashboard_restarts = @(); suspended = @{} }
}

function Save-State($state) {
    $state | ConvertTo-Json -Depth 5 | Set-Content -Path $stateFile -Encoding utf8
}

function Send-Telegram($text) {
    $envFile = Join-Path $repo "watchdog\.env"
    if (-not (Test-Path $envFile)) { return }
    $cfg = @{}
    Get-Content $envFile | ForEach-Object {
        if ($_ -match "^\s*([A-Z_]+)\s*=\s*(.*)$") { $cfg[$matches[1]] = $matches[2].Trim() }
    }
    $token = $cfg["TELEGRAM_BOT_TOKEN"]; $chatId = $cfg["TELEGRAM_CHAT_ID"]
    if (-not $token -or -not $chatId) { return }
    try {
        $uri = "https://api.telegram.org/bot$token/sendMessage"
        Invoke-RestMethod -Uri $uri -Method Post -Body @{ chat_id = $chatId; text = $text } -TimeoutSec 10 | Out-Null
    } catch { Write-SupLog "telegram indisponible: $_" }
}

function Test-DashboardAlive {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        if ($p.CommandLine -match "app\.dashboard_api\.server" -or $p.CommandLine -match "dashboard_api[\\/]server\.py") {
            return $true
        }
    }
    return $false
}

function Start-Dashboard {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "app\.dashboard_api\.server" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    # V5.1 / action A1 (2026-07-17) - log du dashboard SEPARE de celui du bot.
    # Meme motif que scripts/bot_supervisor.ps1:66, qui fixe deja son propre
    # HERMES_LOG_FILE avant de lancer app.main.
    #
    # Pourquoi : deux process tenant logs/hermes.log ouvert empechent le rollover
    # (os.rename refuse par Windows sur un fichier ouvert) - le meme diagnostic que
    # celui note dans bot_supervisor.ps1:58-61 le 2026-07-08. Le dashboard tenait ce
    # fichier EN PERMANENCE : app/dashboard_api/server.py:28 fait `from app.logger
    # import log`, et app/logger.py:70 cree le RotatingFileHandler des l'import.
    # Resultat mesure : 176 rollovers echoues, 0 backup, 1766 Mo (176x le seuil).
    #
    # Portee : ce $env: n'affecte QUE ce process PowerShell et ses enfants (le
    # dashboard). Le bot (app.main) est lance par scripts/bot_supervisor.ps1, qui
    # fixe HERMES_LOG_FILE=logs\hermes.log de son cote : aucune fuite possible.
    # Rollback : supprimer la ligne $env:HERMES_LOG_FILE ci-dessous.
    $env:HERMES_LOG_FILE = Join-Path $repo "logs\hermes-dashboard.log"
    Start-Process -FilePath "python" -ArgumentList "-m", "app.dashboard_api.server" -WorkingDirectory $repo -WindowStyle Hidden
}

function Restart-Budget($state, $key) {
    $now = Get-Date
    $recent = @($state.$key | Where-Object { ([datetime]$_) -gt $now.AddHours(-1) })
    return ,$recent
}

function Invoke-Restart($state, $key, $label, $action) {
    $now = Get-Date
    $suspendedUntil = $state.suspended.$key
    if ($suspendedUntil -and ([datetime]$suspendedUntil) -gt $now) {
        return $state
    }
    $recent = Restart-Budget $state $key
    if ($recent.Count -ge 3) {
        Write-SupLog "[CRASH_LOOP] $label : $($recent.Count) restarts en 1h - STOP"
        Send-Telegram "HERMES : $label en crash-loop ($($recent.Count) restarts en 1h) - supervision suspendue 30min"
        $state.suspended | Add-Member -NotePropertyName $key -NotePropertyValue ($now.AddMinutes(30).ToString("o")) -Force
        Save-State $state
        return $state
    }
    & $action
    $recent += $now.ToString("o")
    $state.$key = $recent
    Write-SupLog "[AUTO_RESTART] $label redemarre a $($now.ToString('HH:mm'))"
    Send-Telegram "HERMES : $label redemarre a $($now.ToString('HH:mm')) (dashboard uniquement, le bot n'est pas affecte)"
    Save-State $state
    return $state
}

Write-SupLog "dashboard supervisor demarre"
while ($true) {
    $state = Get-State
    if (-not (Test-DashboardAlive)) {
        $state = Invoke-Restart $state "dashboard_restarts" "dashboard HERMES (app.dashboard_api.server)" { Start-Dashboard }
    }
    Start-Sleep -Seconds 60
}
