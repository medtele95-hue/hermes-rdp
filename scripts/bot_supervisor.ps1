# HERMES bot supervisor — AUTOPILOT.md COUCHE 1 ("le bot ne meurt jamais").
# Process INDEPENDANT du bot, boucle infinie (meme motif que watchdog/start_watchdog.ps1).
# Verifie toutes les 60s: (a) le bot (app.main) tourne-t-il ? (b) le watchdog a-t-il un
# heartbeat recent (gardien du gardien) ? Redemarre ce qui manque, avec anti-boucle
# (max 3 restarts/heure PAR cible) + alerte Telegram + log [AUTO_RESTART].
# Tache planifiee HERMES_BOT_SUPERVISOR, declencheur au boot (comme HERMES_WATCHDOG).
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$stateFile = Join-Path $repo "logs\supervisor_state.json"
$logFile = Join-Path $repo "logs\supervisor.log"
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
    return [PSCustomObject]@{ bot_restarts = @(); watchdog_restarts = @(); suspended = @{} }
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

function Test-BotAlive {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        if ($p.CommandLine -match "app[\\/]main\.py" -or $p.CommandLine -match "app\.main") {
            return $true
        }
    }
    return $false
}

function Start-Bot {
    # Ferme toute instance app.main encore agonisante avant de relancer -
    # deux process tenant logs/hermes.log ouvert simultanement pendant un
    # rollover l'a deja fige en silence une fois (diagnostic auto-medecin
    # 2026-07-08).
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "app[\\/]main\.py" -or $_.CommandLine -match "app\.main" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    $env:HERMES_LOG_FILE = Join-Path $repo "logs\hermes.log"
    Start-Process -FilePath "python" -ArgumentList "-m", "app.main" -WorkingDirectory $repo -WindowStyle Hidden
}

function Test-StoppedByUser {
    # mission/DASHBOARD.md (2026-07-08): dashboard's bot-stop action writes
    # this flag before killing the process — the supervisor must NOT treat
    # that as a crash and auto-restart it. Cleared by the dashboard's
    # bot-start action (app.dashboard_api.actions.action_bot_start).
    return Test-Path (Join-Path $repo "logs\stopped_by_user.flag")
}

function Test-WatchdogAlive {
    $hb = Join-Path $repo "watchdog\heartbeat.txt"
    if (-not (Test-Path $hb)) { return $false }
    try {
        $stamp = [datetime]::Parse((Get-Content $hb -Raw).Trim(), $null, [System.Globalization.DateTimeStyles]::RoundtripKind)
        return ((Get-Date).ToUniversalTime() - $stamp.ToUniversalTime()).TotalMinutes -lt 5
    } catch { return $false }
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
        Write-SupLog "[CRASH_LOOP] $label : $($recent.Count) restarts en 1h - STOP, intervention auto-medecin declenchee"
        Send-Telegram "HERMES CRITIQUE : $label en crash-loop ($($recent.Count) restarts en 1h) - intervention auto-medecin declenchee"
        try { Start-ScheduledTask -TaskName "HERMES_AUTO_MEDIC" -ErrorAction Stop } catch { Write-SupLog "auto-medecin trigger FAILED: $_" }
        $state.suspended | Add-Member -NotePropertyName $key -NotePropertyValue ($now.AddMinutes(30).ToString("o")) -Force
        Save-State $state
        return $state
    }
    & $action
    $recent += $now.ToString("o")
    $state.$key = $recent
    Write-SupLog "[AUTO_RESTART] $label redemarre a $($now.ToString('HH:mm')), raison detectee: process absent/heartbeat perime"
    Send-Telegram "HERMES : $label redemarre a $($now.ToString('HH:mm')), raison detectee : process absent/heartbeat perime"
    Save-State $state
    return $state
}

Write-SupLog "supervisor demarre"
while ($true) {
    $state = Get-State
    if (-not (Test-BotAlive)) {
        if (Test-StoppedByUser) {
            Write-SupLog "[DASHBOARD_STOP] bot arrete volontairement via dashboard, pas de redemarrage auto"
        } else {
            $state = Invoke-Restart $state "bot_restarts" "bot HERMES (app.main)" { Start-Bot }
        }
    }
    if (-not (Test-WatchdogAlive)) {
        $state = Invoke-Restart $state "watchdog_restarts" "watchdog (gardien du gardien)" {
            Start-ScheduledTask -TaskName "HERMES_WATCHDOG" -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 60
}
