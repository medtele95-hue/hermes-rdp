# HERMES auto-medecin — AUTOPILOT.md COUCHE 2, PORTEE DIAGNOSTIC SEUL.
#
# Decision explicite : ce script N'invoque JAMAIS --dangerously-skip-permissions.
# Il lance `claude -p` avec --permission-mode dontAsk et --allowedTools limite a
# Read,Grep,Glob (verifie empiriquement : un appel Write est refuse et le process
# se termine proprement, code retour non-zero, aucun fichier cree). L'IA ne peut
# donc ni ecrire, ni editer, ni committer, ni pousser, ni executer de commande —
# elle peut seulement lire et rendre un diagnostic texte, capture par CE script
# (qui, lui, ecrit le rapport avec des privileges normaux, hors de portee de l'IA).
#
# Toute correction reelle attend en section RESERVE SIMO du rapport, pour une
# session Claude Code normale invoquee a la main par SIMO.
#
# Tache planifiee HERMES_AUTO_MEDIC (07:00 et 19:00) + declenchable par
# scripts/bot_supervisor.ps1 en cas de crash-loop.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$reportsDir = "C:\hermes-reports"
New-Item -ItemType Directory -Force $reportsDir | Out-Null
$healthLog = Join-Path $reportsDir "_automation_health.log"
$stateFile = Join-Path $reportsDir "_auto_medic_state.json"

function Write-Health($msg) {
    Add-Content -Path $healthLog -Value "$(Get-Date -Format o) [auto_medic] $msg" -Encoding utf8
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
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" -Method Post `
            -Body @{ chat_id = $chatId; text = $text } -TimeoutSec 10 | Out-Null
    } catch { Write-Health "telegram indisponible: $_" }
}

$stamp = Get-Date -Format "yyyy-MM-ddTHHmmss"
$reportPath = Join-Path $reportsDir "auto_medic_$stamp.md"

Write-Health "run started"
$prompt = Get-Content (Join-Path $repo "AUTO_MEDIC_MISSION.md") -Raw

try {
    $raw = "" | & claude -p $prompt --permission-mode dontAsk --allowedTools "Read,Grep,Glob" --output-format text 2>&1
    $exitCode = $LASTEXITCODE
    $output = ($raw | Where-Object { $_ -notmatch "^Warning: no stdin data received" }) -join "`n"
} catch {
    $output = "ERREUR de lancement claude -p : $_"
    $exitCode = 1
}

if ($exitCode -ne 0 -and -not $output) {
    $output = "auto-medecin: claude -p a echoue (code $exitCode) sans sortie exploitable."
}

Set-Content -Path $reportPath -Value $output -Encoding utf8
Write-Health "run finished exit=$exitCode report=$reportPath"

# -- budget de securite : n'insiste pas plus de 3 fois de suite sur le meme "RAS"/probleme --
$state = if (Test-Path $stateFile) { try { Get-Content $stateFile -Raw | ConvertFrom-Json } catch { $null } } else { $null }
if (-not $state) { $state = [PSCustomObject]@{ last_summary_hash = ""; streak = 0 } }
$summaryLine = ($output -split "`n" | Select-Object -First 15) -join " "
$hash = [System.BitConverter]::ToString([System.Security.Cryptography.MD5]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($summaryLine)))
if ($hash -eq $state.last_summary_hash -and $summaryLine -notmatch "RAS") {
    $state.streak += 1
} else {
    $state.streak = 1
}
$state.last_summary_hash = $hash
$state | ConvertTo-Json | Set-Content -Path $stateFile -Encoding utf8

$isRas = $output -match "RAS\s*[-]\s*aucun probl"
if ($isRas) {
    Send-Telegram "HERMES auto-medecin : RAS"
} elseif ($state.streak -ge 3) {
    Send-Telegram "HERMES CRITIQUE : meme probleme non resolu apres 3 rondes auto-medecin - intervention SIMO/advisor requise. Voir $reportPath"
    Write-Health "ESCALATION streak=$($state.streak)"
} else {
    $firstLines = ($output -split "`n" | Select-Object -First 5) -join " | "
    Send-Telegram "HERMES auto-medecin : probleme(s) detecte(s) (round $($state.streak)/3). $firstLines"
}

Write-Output "[auto_medic] OK -> $reportPath"
